package probe

// Pre-exec agent (B5 PR6).
//
// The agent runs on the host as the rootless daemon user, with the pinned
// Docker socket, for the root collector's preexec_confinement collection. One
// request is one public fixture run through the production hosted grading
// launch: codingexecutor.PhaseFactory.HostedGrading with the manifest the
// approved grading profile converts to for the language's pinned image, then
// Executor.Build and Executor.Test with that language's own recorded test
// command.
//
// Every fixture is a subject compiled against the same hidden suite. A hostile
// subject panics when its action is not confined and observes EPERM when it is,
// so the suite's own pass/fail is the observation. The agent reports only the
// production receipt and the digests of what it staged. It never decides an
// outcome, never names an image or a command, and never writes a record: the
// collector maps every receipt to a catalog outcome itself.

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedworker"
)

// PreexecAgentSchema names the agent's line protocol; it is not a record.
const PreexecAgentSchema = "dittobench-coding-native-preexec-agent-v1"

const (
	maxPreexecRuns     = 256
	preexecRunIDPrefix = "p"
	// Only one fixture runs at a time: the collector reads the hang subject's
	// /proc from outside and must know which candidate it is looking at.
	maxConcurrentPreexecRuns = 1
)

var preexecRunIDPattern = regexp.MustCompile(`^p[0-9]{1,3}$`)

// PreexecRequest is one agent request. Keys are closed.
type PreexecRequest struct {
	Op  string `json:"op"`
	Run string `json:"run,omitempty"`
	// Language selects the pinned image and its fixture entry.
	Language string `json:"language,omitempty"`
	// Repository is the approved image repository; the digest comes from the
	// pinned image set, never from this field.
	Repository string `json:"repository,omitempty"`
	// Fixture is "control.<name>" or "hostile.<name>", exactly as the catalog
	// names its probes.
	Fixture string `json:"fixture,omitempty"`
	// TimeoutMS bounds one wait, so the collector keeps its own deadline
	// rather than blocking on a run that never finishes.
	TimeoutMS int `json:"timeout_ms,omitempty"`
}

// PreexecResponse is one agent response. It carries observations and staged
// digests only; no field states an outcome or a match.
type PreexecResponse struct {
	Schema                  string            `json:"schema"`
	Op                      string            `json:"op"`
	Run                     string            `json:"run,omitempty"`
	ExecutorInstance        string            `json:"executor_instance,omitempty"`
	CommandTimeoutMS        int               `json:"command_timeout_ms,omitempty"`
	Done                    bool              `json:"done,omitempty"`
	RunFailed               bool              `json:"run_failed,omitempty"`
	BuildFailed             bool              `json:"build_failed,omitempty"`
	ReturnCode              *int              `json:"return_code,omitempty"`
	TimedOut                bool              `json:"timed_out,omitempty"`
	Completed               bool              `json:"completed,omitempty"`
	Passed                  *int              `json:"passed,omitempty"`
	Total                   *int              `json:"total,omitempty"`
	SuiteSHA256             string            `json:"suite_sha256,omitempty"`
	SubjectSHA256           string            `json:"subject_sha256,omitempty"`
	PID                     int               `json:"pid,omitempty"`
	UID                     *int              `json:"uid,omitempty"`
	GID                     *int              `json:"gid,omitempty"`
	ProbeRunnerBinarySHA256 string            `json:"probe_runner_binary_sha256,omitempty"`
	Inputs                  map[string]string `json:"inputs,omitempty"`
	Error                   string            `json:"error,omitempty"`
}

// PreexecReceipt is the production receipt for one finished fixture run.
type PreexecReceipt struct {
	ReturnCode int
	TimedOut   bool
	Completed  bool
	Passed     uint32
	Total      uint32
	// BuildFailed reports a build that never reached the test command. A
	// hostile subject that refuses to build is not a confinement observation.
	BuildFailed bool
}

// StartedPreexec is a launched fixture run: how the collector finds its
// executor from outside, and how to wait for the production receipt.
type StartedPreexec struct {
	ExecutorInstance string
	Wait             func() (PreexecReceipt, error)
}

// PreexecSpec is one resolved fixture launch.
type PreexecSpec struct {
	Language        string
	Repository      string
	Image           catalog.EnforcementImage
	Group           string
	ExpectedTotal   uint32
	Workspace       string
	ProtectedGrader string
	Execution       codinghostedinput.Profile
	Grading         codinghostedworker.GradingProfile
	SeccompProfile  string
	AppArmorProfile string
	Now             func() time.Time
}

// PreexecBackend launches fixture runs. Production uses the executor package;
// tests replace it.
type PreexecBackend interface {
	Start(ctx context.Context, spec PreexecSpec) (StartedPreexec, error)
}

// PreexecAgentConfig is the agent's startup authority.
type PreexecAgentConfig struct {
	ExecutionProfile  []byte
	GradingProfile    []byte
	EnforcementImages []byte
	PreexecFixtures   []byte
	// Checkout is the reviewed checkout the pinned fixture paths are read
	// from; every staged file is verified against the manifest digest.
	Checkout        string
	Runner          string
	WorkDirectory   string
	SeccompProfile  string
	AppArmorProfile string
	Backend         PreexecBackend
	RunnerSHA256    func() (string, error)
	FileSHA256      func(string) (string, error)
	Now             func() time.Time
}

// PreexecAgent holds the runs of one collector session.
type PreexecAgent struct {
	config    PreexecAgentConfig
	execution codinghostedinput.Profile
	grading   codinghostedworker.GradingProfile
	images    catalog.EnforcementImages
	fixtures  catalog.PreexecFixtures
	inputs    map[string]string
	runnerSHA string

	ctx    context.Context
	cancel context.CancelFunc
	mu     sync.Mutex
	next   int
	runs   map[string]*preexecRun
	group  sync.WaitGroup
}

type preexecRun struct {
	done      chan struct{}
	receipt   PreexecReceipt
	err       error
	workspace string
	protected string
	subject   string
	suite     string
}

// NewPreexecAgent validates every approved input before any launch, and reads
// every pinned fixture file from the reviewed checkout, refusing any whose
// bytes differ from the manifest.
func NewPreexecAgent(ctx context.Context, config PreexecAgentConfig) (*PreexecAgent, error) {
	if ctx == nil || config.Backend == nil {
		return nil, errors.New("probe: preexec agent needs a context and a backend")
	}
	if config.RunnerSHA256 == nil {
		config.RunnerSHA256 = RunningExecutableSHA256
	}
	if config.FileSHA256 == nil {
		config.FileSHA256 = fileSHA256
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	runnerSHA, err := config.RunnerSHA256()
	if err != nil || !sha256Hex.MatchString(runnerSHA) {
		return nil, errors.New("probe: the running probe binary could not be measured")
	}
	if !cleanAbsolute(config.Runner) || !cleanAbsolute(config.WorkDirectory) || !cleanAbsolute(config.Checkout) {
		return nil, errors.New("probe: runner, work directory and checkout must be clean absolute paths")
	}
	if mounted, err := config.FileSHA256(config.Runner); err != nil || mounted != runnerSHA {
		return nil, errors.New("probe: the runner path is not the running probe binary")
	}
	var execution codinghostedinput.Profile
	if codingcontract.ValidateJSONDocument(config.ExecutionProfile, 64<<10) != nil ||
		codingcontract.RequireExactCanonicalJSON(config.ExecutionProfile) != nil ||
		json.Unmarshal(config.ExecutionProfile, &execution) != nil {
		return nil, errors.New("probe: execution profile is not exact canonical JSON")
	}
	if digest, err := codinghostedinput.ProfileDigest(execution); err != nil || digest != digestHex(config.ExecutionProfile) {
		return nil, errors.New("probe: execution profile is invalid")
	}
	grading, err := parseGradingProfile(config.GradingProfile)
	if err != nil {
		return nil, err
	}
	images, err := catalog.ParseEnforcementImages(config.EnforcementImages)
	if err != nil {
		return nil, fmt.Errorf("probe: %w", err)
	}
	if images.GradingProfileSHA256 != digestHex(config.GradingProfile) {
		return nil, errors.New("probe: enforcement images name another grading profile")
	}
	fixtures, err := catalog.ParsePreexecFixtures(config.PreexecFixtures)
	if err != nil {
		return nil, fmt.Errorf("probe: %w", err)
	}
	if err := verifyFixtureFiles(config, fixtures); err != nil {
		return nil, err
	}
	// The fixture suite is not the benchmark's suite, so the fixture command is
	// the one that runs it; the image's recorded command stays the approved
	// grading command. What must agree is Rust's pinned authority, exactly as
	// the offline verifier binds it.
	for _, language := range catalog.Languages {
		entry := fixtures.Languages[language]
		image, ok := images.Images[language]
		if !ok {
			return nil, errors.New("probe: a fixture language is not in the pinned image set")
		}
		if language != "rust" {
			continue
		}
		if entry.Authority == nil || rustAuthorityOf(image.TestArgv[entry.TestGroup]) != entry.Authority.SHA256 {
			return nil, errors.New("probe: the rust fixture authority is not the recorded rust authority")
		}
	}
	child, cancel := context.WithCancel(ctx)
	return &PreexecAgent{
		config: config, execution: execution, grading: grading, images: images,
		fixtures: fixtures, runnerSHA: runnerSHA,
		inputs: map[string]string{
			"enforcement_images_sha256": images.SHA256,
			"execution_profile_sha256":  digestHex(config.ExecutionProfile),
			"grading_profile_sha256":    digestHex(config.GradingProfile),
			"preexec_fixtures_sha256":   fixtures.SHA256,
		},
		ctx: child, cancel: cancel, runs: map[string]*preexecRun{},
	}, nil
}

// verifyFixtureFiles reads every pinned file once, before any launch, so a
// checkout that differs from the approval is refused at startup rather than
// after a container has already run.
func verifyFixtureFiles(config PreexecAgentConfig, fixtures catalog.PreexecFixtures) error {
	for _, ref := range fixtures.Files() {
		path := filepath.Join(config.Checkout, filepath.FromSlash(ref.Path))
		digest, err := config.FileSHA256(path)
		if err != nil || digest != ref.SHA256 {
			return fmt.Errorf("probe: fixture %s differs from the reviewed checkout", ref.Path)
		}
	}
	return nil
}

// DecodePreexecRequest parses one request line strictly.
func DecodePreexecRequest(line []byte) (PreexecRequest, error) {
	var request PreexecRequest
	decoder := json.NewDecoder(strings.NewReader(string(line)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || decoder.More() {
		return PreexecRequest{}, errors.New("probe: malformed preexec agent request")
	}
	return request, nil
}

// Handle performs one request.
func (a *PreexecAgent) Handle(request PreexecRequest) PreexecResponse {
	response := PreexecResponse{Schema: PreexecAgentSchema, Op: request.Op}
	switch request.Op {
	case "hello":
		uid, gid := os.Getuid(), os.Getgid()
		response.PID, response.UID, response.GID = os.Getpid(), &uid, &gid
		response.ProbeRunnerBinarySHA256 = a.runnerSHA
		response.Inputs = a.inputs
	case "start":
		return a.start(response, request)
	case "wait":
		return a.wait(response, request)
	case "exit":
		a.Close()
	default:
		response.Error = "probe: unknown preexec agent op"
	}
	return response
}

// subject resolves one catalog fixture id to the recorded file, refusing any id
// the pinned manifest does not carry.
func (a *PreexecAgent) subject(language, fixture string) (catalog.PreexecFixtureRef, error) {
	entry, ok := a.fixtures.Languages[language]
	if !ok {
		return catalog.PreexecFixtureRef{}, errors.New("probe: language is not in the pinned fixture set")
	}
	phase, name, found := strings.Cut(fixture, ".")
	if !found {
		return catalog.PreexecFixtureRef{}, errors.New("probe: fixture id is malformed")
	}
	switch phase {
	case "control":
		ref, ok := entry.Controls[name]
		if !ok {
			return catalog.PreexecFixtureRef{}, errors.New("probe: control is not in the pinned fixture set")
		}
		return ref, nil
	case "hostile":
		hostile, ok := entry.Hostile[name]
		if !ok {
			return catalog.PreexecFixtureRef{}, errors.New("probe: hostile fixture is not in the pinned fixture set")
		}
		return hostile.Subject, nil
	}
	return catalog.PreexecFixtureRef{}, errors.New("probe: fixture phase is unknown")
}

func (a *PreexecAgent) spec(request PreexecRequest, run *preexecRun) (PreexecSpec, int, error) {
	spec := PreexecSpec{
		Language: request.Language, Repository: request.Repository,
		Execution: a.execution, Grading: a.grading, SeccompProfile: a.config.SeccompProfile,
		AppArmorProfile: a.config.AppArmorProfile, Now: a.config.Now,
	}
	image, ok := a.images.Images[request.Language]
	if !ok {
		return spec, 0, errors.New("probe: language is not in the pinned image set")
	}
	if !repositoryPattern.MatchString(request.Repository) ||
		strings.Contains(request.Repository, "..") || strings.Contains(request.Repository, "@") {
		return spec, 0, errors.New("probe: image repository is malformed")
	}
	entry := a.fixtures.Languages[request.Language]
	ref, err := a.subject(request.Language, request.Fixture)
	if err != nil {
		return spec, 0, err
	}
	// One fixture run executes the fixture suite's own recorded command for the
	// approved group; every other group keeps the image's recorded command.
	image.TestArgv = maps.Clone(image.TestArgv)
	image.TestArgv[entry.TestGroup] = slices.Clone(entry.TestArgv)
	spec.Image, spec.Group, spec.ExpectedTotal = image, entry.TestGroup, entry.ExpectedTotal
	index := slices.IndexFunc(a.grading.TestGroups, func(group codinggrader.TestGroupSpec) bool {
		return group.Group == entry.TestGroup
	})
	if index < 0 {
		return spec, 0, errors.New("probe: fixture test group is not in the approved grading profile")
	}
	run.subject, run.suite = ref.SHA256, entry.ControlsSuiteSHA256
	// The group's approved command timeout, never a request's.
	return spec, int(a.grading.TestGroups[index].Command.Timeout / time.Millisecond), nil
}

func (a *PreexecAgent) start(response PreexecResponse, request PreexecRequest) PreexecResponse {
	run := &preexecRun{done: make(chan struct{})}
	spec, timeoutMS, err := a.spec(request, run)
	if err != nil {
		response.Error = err.Error()
		return response
	}
	a.mu.Lock()
	active := 0
	for _, existing := range a.runs {
		select {
		case <-existing.done:
		default:
			active++
		}
	}
	if a.next >= maxPreexecRuns || active >= maxConcurrentPreexecRuns || a.ctx.Err() != nil {
		a.mu.Unlock()
		response.Error = "probe: preexec agent run bound reached"
		return response
	}
	id := fmt.Sprintf("%s%d", preexecRunIDPrefix, a.next)
	a.next++
	a.mu.Unlock()
	if err := a.stage(id, request, run, &spec); err != nil {
		a.removeRun(run)
		response.Error = err.Error()
		return response
	}
	started, err := a.config.Backend.Start(a.ctx, spec)
	if err != nil {
		a.removeRun(run)
		response.Error = err.Error()
		return response
	}
	a.mu.Lock()
	a.runs[id] = run
	a.mu.Unlock()
	a.group.Add(1)
	go func() {
		defer a.group.Done()
		defer close(run.done)
		run.receipt, run.err = started.Wait()
		a.removeRun(run)
	}()
	response.Run, response.ExecutorInstance = id, started.ExecutorInstance
	response.CommandTimeoutMS = timeoutMS
	response.SubjectSHA256, response.SuiteSHA256 = run.subject, run.suite
	return response
}

// stage builds the two directories one fixture run needs: a workspace holding
// only that subject and its module file, and a protected grader directory
// holding only the hidden suite. Both are written from the reviewed checkout
// and re-verified against the pinned digests.
func (a *PreexecAgent) stage(id string, request PreexecRequest, run *preexecRun, spec *PreexecSpec) error {
	entry := a.fixtures.Languages[request.Language]
	ref, err := a.subject(request.Language, request.Fixture)
	if err != nil {
		return err
	}
	workspace := filepath.Join(a.config.WorkDirectory, "preexec-workspace-"+id)
	protected := filepath.Join(a.config.WorkDirectory, "preexec-grader-"+id)
	for _, directory := range []string{workspace, protected} {
		if err := os.Mkdir(directory, 0o755); err != nil {
			return errors.New("probe: preexec directories could not be created")
		}
	}
	run.workspace, run.protected = workspace, protected
	staged := []struct {
		ref       catalog.PreexecFixtureRef
		directory string
	}{
		{ref, workspace},
		{entry.ControlsSuite, protected},
	}
	if entry.Module != nil {
		staged = append(staged, struct {
			ref       catalog.PreexecFixtureRef
			directory string
		}{*entry.Module, workspace})
	}
	if entry.Authority != nil {
		staged = append(staged, struct {
			ref       catalog.PreexecFixtureRef
			directory string
		}{*entry.Authority, protected})
	}
	for _, item := range staged {
		if err := a.copyFixture(item.ref, item.directory); err != nil {
			return err
		}
	}
	spec.Workspace, spec.ProtectedGrader = workspace, protected
	return nil
}

func (a *PreexecAgent) copyFixture(ref catalog.PreexecFixtureRef, directory string) error {
	source := filepath.Join(a.config.Checkout, filepath.FromSlash(ref.Path))
	digest, err := a.config.FileSHA256(source)
	if err != nil || digest != ref.SHA256 {
		return fmt.Errorf("probe: fixture %s differs from the reviewed checkout", ref.Path)
	}
	raw, err := os.ReadFile(source)
	if err != nil {
		return fmt.Errorf("probe: fixture %s could not be read", ref.Path)
	}
	target := filepath.Join(directory, filepath.Base(filepath.FromSlash(ref.Path)))
	handle, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return fmt.Errorf("probe: fixture %s could not be staged", ref.Path)
	}
	_, writeErr := handle.Write(raw)
	closeErr := handle.Close()
	if writeErr != nil || closeErr != nil {
		return fmt.Errorf("probe: fixture %s could not be staged", ref.Path)
	}
	return nil
}

func (a *PreexecAgent) removeRun(run *preexecRun) {
	for _, directory := range []string{run.workspace, run.protected} {
		if directory != "" {
			_ = os.RemoveAll(directory)
		}
	}
}

func (a *PreexecAgent) wait(response PreexecResponse, request PreexecRequest) PreexecResponse {
	response.Run = request.Run
	if !preexecRunIDPattern.MatchString(request.Run) ||
		request.TimeoutMS < 0 || request.TimeoutMS > maxResourceTimeoutMS {
		response.Error = "probe: wait request is malformed"
		return response
	}
	a.mu.Lock()
	run, ok := a.runs[request.Run]
	a.mu.Unlock()
	if !ok {
		response.Error = "probe: preexec run is unknown"
		return response
	}
	select {
	case <-run.done:
	case <-a.ctx.Done():
		response.Error = "probe: preexec agent is shutting down"
		return response
	case <-time.After(time.Duration(request.TimeoutMS) * time.Millisecond):
		return response
	}
	response.Done = true
	response.SubjectSHA256, response.SuiteSHA256 = run.subject, run.suite
	if run.err != nil {
		response.RunFailed = true
		response.Error = "probe: preexec run failed"
		return response
	}
	receipt := run.receipt
	code, passed, total := receipt.ReturnCode, int(receipt.Passed), int(receipt.Total)
	response.ReturnCode, response.Passed, response.Total = &code, &passed, &total
	response.TimedOut, response.Completed, response.BuildFailed = receipt.TimedOut, receipt.Completed, receipt.BuildFailed
	return response
}

// Close cancels every run and waits for the production cleanup of each.
func (a *PreexecAgent) Close() {
	a.cancel()
	done := make(chan struct{})
	go func() {
		a.group.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(resourceShutdownBound):
	}
	a.mu.Lock()
	runs := a.runs
	a.runs = map[string]*preexecRun{}
	a.mu.Unlock()
	for _, run := range runs {
		a.removeRun(run)
	}
}

// ProductionPreexecBackend launches through the production hosted grading path.
type ProductionPreexecBackend struct {
	Docker DockerCLI
}

// Start implements PreexecBackend. The manifest comes from the approved
// grading profile with the fixture suite's own expected total; the image
// digest and both commands come from the pinned image set.
func (b ProductionPreexecBackend) Start(ctx context.Context, spec PreexecSpec) (StartedPreexec, error) {
	factory, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
		ImageRepository: spec.Repository, CandidateUID: 10001, CandidateGID: 10001,
		RequireRootless: true, RequireIsolatedDaemon: true,
		SeccompProfile: spec.SeccompProfile, AppArmorProfile: spec.AppArmorProfile, Now: spec.Now,
	})
	if err != nil {
		return StartedPreexec{}, err
	}
	group := slices.IndexFunc(spec.Grading.TestGroups, func(item codinggrader.TestGroupSpec) bool {
		return item.Group == spec.Group
	})
	if group < 0 {
		return StartedPreexec{}, errors.New("probe: fixture test group is not in the approved grading profile")
	}
	timeout := spec.Grading.TestGroups[group].Command.Timeout
	deadline := spec.Now().Add(max(30*time.Minute, timeout+10*time.Minute))
	manifest, err := spec.Grading.PreexecProbeManifest(codinghostedworker.EnforcementProbeImage{
		ImageDigest: spec.Image.ImageDigest, BuildArgv: spec.Image.BuildArgv, TestArgv: spec.Image.TestArgv,
	}, deadline, spec.Group, spec.ExpectedTotal)
	if err != nil {
		return StartedPreexec{}, err
	}
	created, err := factory.HostedGrading(ctx, manifest)
	if err != nil {
		return StartedPreexec{}, err
	}
	executor, ok := created.(*codingexecutor.Executor)
	if !ok {
		return StartedPreexec{}, errors.New("probe: grading executor is not the production executor")
	}
	return StartedPreexec{ExecutorInstance: executor.InstanceID(), Wait: func() (PreexecReceipt, error) {
		build, err := executor.Build(ctx, spec.Workspace, manifest.Build.Command)
		if err != nil {
			return PreexecReceipt{}, err
		}
		// A subject that does not build produced no confinement observation.
		// The collector sees the flag and refuses rather than reading a
		// receipt the suite never wrote.
		if build.TimedOut || !build.Completed || build.ReturnCode != 0 {
			return PreexecReceipt{
				ReturnCode: build.ReturnCode, TimedOut: build.TimedOut,
				Completed: build.Completed, BuildFailed: true,
			}, nil
		}
		run, err := executor.Test(ctx, spec.Workspace, spec.ProtectedGrader, manifest.TestGroups[group])
		return PreexecReceipt{
			ReturnCode: run.ReturnCode, TimedOut: run.TimedOut, Completed: run.Completed,
			Passed: run.Passed, Total: run.Total,
		}, err
	}}, nil
}

// ServePreexecAgent serves the pre-exec line protocol on the collector's pipe,
// one request at a time, with the same bounds as the resource agent.
func ServePreexecAgent(ctx context.Context, agent *PreexecAgent, input io.Reader, output io.Writer) error {
	defer agent.Close()
	lines := make(chan []byte)
	failed := make(chan error, 1)
	go func() {
		scanner := bufio.NewScanner(input)
		scanner.Buffer(make([]byte, maxAgentLine), maxAgentLine)
		for scanner.Scan() {
			lines <- append([]byte(nil), scanner.Bytes()...)
		}
		failed <- scanner.Err()
	}()
	encoder := json.NewEncoder(output)
	for count := 0; ; count++ {
		var line []byte
		select {
		case <-ctx.Done():
			return nil
		case err := <-failed:
			if err != nil {
				return fmt.Errorf("probe: preexec agent input: %w", err)
			}
			return nil
		case line = <-lines:
		}
		if count >= maxAgentRequests {
			return errors.New("probe: agent request bound reached")
		}
		request, err := DecodePreexecRequest(line)
		var response PreexecResponse
		if err != nil {
			response = PreexecResponse{Schema: PreexecAgentSchema, Op: "invalid", Error: err.Error()}
		} else {
			response = agent.Handle(request)
		}
		if err := encoder.Encode(response); err != nil {
			return err
		}
		if err == nil && request.Op == "exit" {
			return nil
		}
	}
}

// rustAuthorityOf reads the pinned authority digest out of a recorded Rust test
// command, the same way the offline verifier does.
func rustAuthorityOf(argv []string) string {
	for index := 1; index+1 < len(argv); index += 2 {
		if argv[index] == "--authority-sha256" {
			return argv[index+1]
		}
	}
	return ""
}
