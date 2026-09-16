package probe

// Resource agent (B5 PR5).
//
// The agent runs on the host as the rootless daemon user, with the pinned
// Docker socket, for the root collector's resource and cleanup collection. It
// starts workloads only through the production launch paths:
//
//   - executor_authoring: codingexecutor.PhaseFactory.Authoring with the
//     approved execution profile's resource policy, then Executor.Execute;
//   - executor_grading: PhaseFactory.HostedGrading with the manifest the
//     approved grading profile converts to for each pinned language image,
//     then Executor.RunEnforcementWorkload (build mode; the hosted Build and
//     Test paths admit only recorded commands);
//   - harness: sandbox.NewHostedHarnessDocker with the execution profile's
//     policy, then LocalDocker.RunEnforcementWorkload and StopRetainingImage.
//
// It reports only the production receipt (return code, timeout, retained
// output length) and the identifiers the collector needs to find the container
// from outside. It never reads a cgroup file, decides a match or writes a
// record: the collector measures every limit itself.

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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
	"github.com/ditto-assistant/dittobench-api/internal/codinglaunchjournal"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// ResourceAgentSchema names the agent's line protocol; it is not a record.
const ResourceAgentSchema = "dittobench-coding-native-resource-agent-v1"

// Container classes, as the catalog names them.
const (
	ClassHarness           = "harness"
	ClassExecutorAuthoring = "executor_authoring"
	ClassExecutorGrading   = "executor_grading"
)

// WorkloadRunnerName is the runner's file name in an executor probe workspace.
const WorkloadRunnerName = "dittobench-coding-enforcement-probe"

const (
	maxResourceRuns        = 512
	maxConcurrentRuns      = 4
	maxResourceTimeoutMS   = 3_600_000
	resourceShutdownBound  = 90 * time.Second
	harnessPollInterval    = 100 * time.Millisecond
	harnessEgressNetwork   = "ditto-native-enforcement"
	enforcementCommandIDRE = `^native-enforcement-[a-z-]{1,24}$`
)

var (
	repositoryPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._/:-]{0,254}$`)
	runIDPattern      = regexp.MustCompile(`^r[0-9]{1,3}$`)
	commandIDPattern  = regexp.MustCompile(enforcementCommandIDRE)
)

// ResourceRequest is one agent request. Keys are closed.
type ResourceRequest struct {
	Op         string   `json:"op"`
	Run        string   `json:"run,omitempty"`
	Class      string   `json:"class,omitempty"`
	Language   string   `json:"language,omitempty"`
	Repository string   `json:"repository,omitempty"`
	Workload   []string `json:"workload,omitempty"`
	TimeoutMS  int      `json:"timeout_ms,omitempty"`
	TestGroup  string   `json:"test_group,omitempty"`
	FailStart  bool     `json:"fail_start,omitempty"`
}

// ResourceResponse is one agent response.
type ResourceResponse struct {
	Schema                  string            `json:"schema"`
	Op                      string            `json:"op"`
	Run                     string            `json:"run,omitempty"`
	ExecutorInstance        string            `json:"executor_instance,omitempty"`
	ContainerName           string            `json:"container_name,omitempty"`
	CommandTimeoutMS        int               `json:"command_timeout_ms,omitempty"`
	Done                    bool              `json:"done,omitempty"`
	RunFailed               bool              `json:"run_failed,omitempty"`
	ReturnCode              *int              `json:"return_code,omitempty"`
	TimedOut                bool              `json:"timed_out,omitempty"`
	Completed               bool              `json:"completed,omitempty"`
	RetainedOutputBytes     *int              `json:"retained_output_bytes,omitempty"`
	PID                     int               `json:"pid,omitempty"`
	UID                     *int              `json:"uid,omitempty"`
	GID                     *int              `json:"gid,omitempty"`
	ProbeRunnerBinarySHA256 string            `json:"probe_runner_binary_sha256,omitempty"`
	Inputs                  map[string]string `json:"inputs,omitempty"`
	Error                   string            `json:"error,omitempty"`
}

// WorkloadReceipt is the production receipt for one finished workload.
type WorkloadReceipt struct {
	ReturnCode          int
	TimedOut            bool
	Completed           bool
	RetainedOutputBytes int
}

// StartedWorkload is a launched workload: how the collector finds its
// container, and how to wait for its production receipt and cleanup.
type StartedWorkload struct {
	ExecutorInstance string
	ContainerName    string
	Wait             func() (WorkloadReceipt, error)
}

// WorkloadSpec is one resolved launch: approved image, profiles and command.
type WorkloadSpec struct {
	Class           string
	Language        string
	Repository      string
	Image           catalog.EnforcementImage
	Command         codingrunner.CommandSpec
	Workspace       string
	Runner          string
	Execution       codinghostedinput.Profile
	Grading         codinghostedworker.GradingProfile
	SeccompProfile  string
	AppArmorProfile string
	Now             func() time.Time
	// FailStart launches the harness from a pinned digest that is never a
	// released image, so the production start fails after it created the job
	// network: the cleanup_recovery partial-start scenario.
	FailStart bool
	// LaunchIntent is the hosted runtime's launch journal hook, when the agent
	// runs with a journal (cleanup_recovery).
	LaunchIntent func(ctx context.Context, run string, containers, networks []string) error
}

// ResourceBackend launches workloads. Production uses the executor and sandbox
// packages; tests replace it.
type ResourceBackend interface {
	Start(ctx context.Context, spec WorkloadSpec) (StartedWorkload, error)
}

// ResourceAgentConfig is the agent's startup authority.
type ResourceAgentConfig struct {
	ExecutionProfile  []byte
	GradingProfile    []byte
	EnforcementImages []byte
	Runner            string
	WorkDirectory     string
	SeccompProfile    string
	AppArmorProfile   string
	Backend           ResourceBackend
	// RunnerSource is copied into executor probe workspaces; /proc/self/exe
	// when empty, so the copy is the running binary.
	RunnerSource string
	RunnerSHA256 func() (string, error)
	FileSHA256   func(string) (string, error)
	Now          func() time.Time
	// LaunchJournal, when set, makes every run one journaled attempt as the
	// hosted runtime does it: the journal is reconciled before the agent
	// accepts requests; each start journals and creates a sentinel network and
	// launches only through the journal hook, one run at a time; each finished
	// run is reconciled, which removes the sentinel.
	LaunchJournal *codinglaunchjournal.Journal
	JournalDocker codinglaunchjournal.Docker
}

// ResourceAgent holds the runs of one collector session.
type ResourceAgent struct {
	config    ResourceAgentConfig
	execution codinghostedinput.Profile
	grading   codinghostedworker.GradingProfile
	images    catalog.EnforcementImages
	inputs    map[string]string
	runnerSHA string

	ctx    context.Context
	cancel context.CancelFunc
	mu     sync.Mutex
	next   int
	runs   map[string]*resourceRun
	group  sync.WaitGroup
}

type resourceRun struct {
	done    chan struct{}
	receipt WorkloadReceipt
	err     error
}

// NewResourceAgent validates every approved input before any launch.
func NewResourceAgent(ctx context.Context, config ResourceAgentConfig) (*ResourceAgent, error) {
	if ctx == nil || config.Backend == nil {
		return nil, errors.New("probe: resource agent needs a context and a backend")
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
	if config.RunnerSource == "" {
		config.RunnerSource = runningExecutable
	}
	runnerSHA, err := config.RunnerSHA256()
	if err != nil || !sha256Hex.MatchString(runnerSHA) {
		return nil, errors.New("probe: the running probe binary could not be measured")
	}
	if !cleanAbsolute(config.Runner) || !cleanAbsolute(config.WorkDirectory) {
		return nil, errors.New("probe: runner and work directory must be clean absolute paths")
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
	if config.LaunchJournal != nil {
		if config.JournalDocker == nil {
			return nil, errors.New("probe: a launch journal needs a Docker client")
		}
		if _, err := config.LaunchJournal.Reconcile(ctx, config.JournalDocker); err != nil {
			return nil, errors.New("probe: the launch journal could not be reconciled")
		}
	}
	child, cancel := context.WithCancel(ctx)
	return &ResourceAgent{
		config: config, execution: execution, grading: grading, images: images, runnerSHA: runnerSHA,
		inputs: map[string]string{
			"enforcement_images_sha256": images.SHA256,
			"execution_profile_sha256":  digestHex(config.ExecutionProfile),
			"grading_profile_sha256":    digestHex(config.GradingProfile),
		},
		ctx: child, cancel: cancel, runs: map[string]*resourceRun{},
	}, nil
}

func cleanAbsolute(path string) bool {
	return filepath.IsAbs(path) && filepath.Clean(path) == path && !strings.ContainsAny(path, ",=\x00\n")
}

func digestHex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// DecodeResourceRequest parses one request line strictly.
func DecodeResourceRequest(line []byte) (ResourceRequest, error) {
	var request ResourceRequest
	decoder := json.NewDecoder(strings.NewReader(string(line)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || decoder.More() {
		return ResourceRequest{}, errors.New("probe: malformed resource agent request")
	}
	return request, nil
}

// Handle performs one request.
func (a *ResourceAgent) Handle(request ResourceRequest) ResourceResponse {
	response := ResourceResponse{Schema: ResourceAgentSchema, Op: request.Op}
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
		response.Error = "probe: unknown resource agent op"
	}
	return response
}

func (a *ResourceAgent) spec(request ResourceRequest) (WorkloadSpec, int, error) {
	spec := WorkloadSpec{
		Class: request.Class, Language: request.Language, Repository: request.Repository, Runner: a.config.Runner,
		Execution: a.execution, Grading: a.grading, SeccompProfile: a.config.SeccompProfile,
		AppArmorProfile: a.config.AppArmorProfile, Now: a.config.Now, FailStart: request.FailStart,
	}
	if request.FailStart && request.Class != ClassHarness {
		return spec, 0, errors.New("probe: only a harness start can be made to fail")
	}
	if !slices.Contains([]string{ClassHarness, ClassExecutorAuthoring, ClassExecutorGrading}, request.Class) {
		return spec, 0, errors.New("probe: container class is unknown")
	}
	image, ok := a.images.Images[request.Language]
	if !ok {
		return spec, 0, errors.New("probe: language is not in the pinned image set")
	}
	spec.Image = image
	if !repositoryPattern.MatchString(request.Repository) || strings.Contains(request.Repository, "..") || strings.Contains(request.Repository, "@") {
		return spec, 0, errors.New("probe: image repository is malformed")
	}
	options, err := ParseWorkloadArgs(request.Workload)
	if err != nil {
		return spec, 0, err
	}
	timeout := time.Duration(request.TimeoutMS) * time.Millisecond
	if request.TestGroup != "" {
		// The group's approved command timeout, never the request's.
		if request.Class != ClassExecutorGrading || request.TimeoutMS != 0 {
			return spec, 0, errors.New("probe: only grading workloads take a test group timeout")
		}
		index := slices.IndexFunc(a.grading.TestGroups, func(group codinggrader.TestGroupSpec) bool { return group.Group == request.TestGroup })
		if index < 0 {
			return spec, 0, errors.New("probe: test group is not in the approved grading profile")
		}
		timeout = a.grading.TestGroups[index].Command.Timeout
	} else if request.TimeoutMS < 1 || request.TimeoutMS > maxResourceTimeoutMS {
		return spec, 0, errors.New("probe: workload timeout is out of bounds")
	}
	argv := WorkloadArgs(options)
	commandID := "native-enforcement-" + options.Mode
	if !commandIDPattern.MatchString(commandID) {
		return spec, 0, errors.New("probe: workload command id is malformed")
	}
	if request.Class == ClassHarness {
		spec.Command = codingrunner.CommandSpec{ID: commandID, Argv: argv, Timeout: timeout}
	} else {
		spec.Command = codingrunner.CommandSpec{
			ID: commandID, Argv: append(slices.Clone(codingexecutor.EnforcementWorkloadPrefix), argv[1:]...), Timeout: timeout,
		}
	}
	return spec, int(timeout / time.Millisecond), nil
}

func (a *ResourceAgent) start(response ResourceResponse, request ResourceRequest) ResourceResponse {
	spec, timeoutMS, err := a.spec(request)
	if err != nil {
		response.Error = err.Error()
		return response
	}
	a.mu.Lock()
	active := 0
	for _, run := range a.runs {
		select {
		case <-run.done:
		default:
			active++
		}
	}
	limit := maxConcurrentRuns
	if a.config.LaunchJournal != nil {
		limit = 1
	}
	if a.next >= maxResourceRuns || active >= limit || a.ctx.Err() != nil {
		a.mu.Unlock()
		response.Error = "probe: resource agent run bound reached"
		return response
	}
	id := fmt.Sprintf("r%d", a.next)
	a.next++
	a.mu.Unlock()
	if spec.Class != ClassHarness {
		workspace, err := a.workspace(id)
		if err != nil {
			response.Error = err.Error()
			return response
		}
		spec.Workspace = workspace
	}
	journal := a.config.LaunchJournal
	if journal != nil {
		if _, err := journal.Sentinel(a.ctx, a.config.JournalDocker); err != nil {
			a.removeWorkspace(spec.Workspace)
			a.reconcile()
			response.Error = "probe: the attempt sentinel network was not created"
			return response
		}
		spec.LaunchIntent = journal.Intent
	}
	started, err := a.config.Backend.Start(a.ctx, spec)
	if err != nil {
		a.removeWorkspace(spec.Workspace)
		a.reconcile()
		response.Error = err.Error()
		return response
	}
	run := &resourceRun{done: make(chan struct{})}
	a.mu.Lock()
	a.runs[id] = run
	a.mu.Unlock()
	a.group.Add(1)
	go func() {
		defer a.group.Done()
		defer close(run.done)
		run.receipt, run.err = started.Wait()
		a.removeWorkspace(spec.Workspace)
		a.reconcile()
	}()
	response.Run, response.ExecutorInstance, response.ContainerName = id, started.ExecutorInstance, started.ContainerName
	response.CommandTimeoutMS = timeoutMS
	return response
}

// reconcile ends one journaled attempt after its production cleanup. A failure
// leaves the journal pending and its objects in place, where the collector's
// outside counts see them.
func (a *ResourceAgent) reconcile() {
	if a.config.LaunchJournal == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	_, _ = a.config.LaunchJournal.Reconcile(ctx, a.config.JournalDocker)
}

// workspace is a fresh probe workspace holding only a copy of the running
// runner, readable and executable by the candidate identity.
func (a *ResourceAgent) workspace(id string) (string, error) {
	directory := filepath.Join(a.config.WorkDirectory, "workspace-"+id)
	if err := os.Mkdir(directory, 0o755); err != nil {
		return "", errors.New("probe: probe workspace could not be created")
	}
	if err := os.Chmod(directory, 0o755); err != nil {
		return "", err
	}
	source, err := os.Open(a.config.RunnerSource)
	if err != nil {
		return "", err
	}
	defer source.Close()
	target := filepath.Join(directory, WorkloadRunnerName)
	destination, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o555)
	if err != nil {
		return "", err
	}
	_, copyErr := io.Copy(destination, io.LimitReader(source, maxRunnerBytes+1))
	closeErr := destination.Close()
	if copyErr != nil || closeErr != nil || os.Chmod(target, 0o555) != nil {
		return "", errors.New("probe: runner copy failed")
	}
	if copied, err := a.config.FileSHA256(target); err != nil || copied != a.runnerSHA {
		return "", errors.New("probe: runner copy differs from the running binary")
	}
	return directory, nil
}

func (a *ResourceAgent) removeWorkspace(directory string) {
	if directory != "" {
		_ = os.RemoveAll(directory)
	}
}

func (a *ResourceAgent) wait(response ResourceResponse, request ResourceRequest) ResourceResponse {
	response.Run = request.Run
	if !runIDPattern.MatchString(request.Run) || request.TimeoutMS < 0 || request.TimeoutMS > maxResourceTimeoutMS {
		response.Error = "probe: wait request is malformed"
		return response
	}
	a.mu.Lock()
	run, ok := a.runs[request.Run]
	a.mu.Unlock()
	if !ok {
		response.Error = "probe: run is unknown"
		return response
	}
	select {
	case <-run.done:
	case <-time.After(time.Duration(request.TimeoutMS) * time.Millisecond):
		return response
	}
	response.Done = true
	if run.err != nil {
		response.RunFailed = true
		return response
	}
	code, retained := run.receipt.ReturnCode, run.receipt.RetainedOutputBytes
	response.ReturnCode, response.RetainedOutputBytes = &code, &retained
	response.TimedOut, response.Completed = run.receipt.TimedOut, run.receipt.Completed
	return response
}

// Close cancels every run and waits, bounded, for production cleanup.
func (a *ResourceAgent) Close() {
	a.cancel()
	finished := make(chan struct{})
	go func() {
		a.group.Wait()
		close(finished)
	}()
	select {
	case <-finished:
	case <-time.After(resourceShutdownBound):
	}
}

// ServeResourceAgent runs the line protocol until exit, end of input,
// cancellation (SIGTERM) or the request bound. Cancellation cancels every run
// and waits for its production cleanup before returning.
func ServeResourceAgent(ctx context.Context, agent *ResourceAgent, input io.Reader, output io.Writer) error {
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
				return fmt.Errorf("probe: resource agent input: %w", err)
			}
			return nil
		case line = <-lines:
		}
		if count >= maxAgentRequests {
			return errors.New("probe: agent request bound reached")
		}
		request, err := DecodeResourceRequest(line)
		var response ResourceResponse
		if err != nil {
			response = ResourceResponse{Schema: ResourceAgentSchema, Op: "invalid", Error: err.Error()}
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

// absentImageDigest is never a released image digest.
const absentImageDigest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

// ProductionResourceBackend launches workloads through the production executor
// and sandbox packages.
type ProductionResourceBackend struct {
	Docker DockerCLI
}

// Start implements ResourceBackend.
func (b ProductionResourceBackend) Start(ctx context.Context, spec WorkloadSpec) (StartedWorkload, error) {
	reference := spec.Repository + "@" + spec.Image.ImageDigest
	switch spec.Class {
	case ClassHarness:
		policy := spec.Execution.ResourcePolicy
		docker := sandbox.NewHostedHarnessDocker(sandbox.HostedHarnessConfig{
			MemoryLimitBytes: policy.MemoryLimitBytes, ScratchLimitBytes: policy.ScratchLimitBytes,
			CPUQuotaMillis: policy.CPUQuotaMillis, PidsLimit: policy.PidsLimit, EgressNetwork: harnessEgressNetwork,
			SeccompProfile: spec.SeccompProfile, AppArmorProfile: spec.AppArmorProfile, LaunchIntent: spec.LaunchIntent,
		})
		if spec.FailStart {
			reference = spec.Repository + "@" + absentImageDigest
		} else if _, err := ResolveApprovedImage(ctx, b.Docker, reference); err != nil {
			return StartedWorkload{}, err
		}
		handle, err := docker.RunEnforcementWorkload(ctx, reference, spec.Runner, spec.Command.Argv)
		if handle == nil {
			return StartedWorkload{}, err
		}
		name := handle.ContainerID
		wait := func() (WorkloadReceipt, error) {
			receipt, waitErr := b.waitHarness(ctx, name, spec.Command.Timeout)
			stopErr := docker.StopRetainingImage(context.Background(), handle)
			return receipt, errors.Join(err, waitErr, stopErr)
		}
		return StartedWorkload{ContainerName: name, Wait: wait}, nil
	case ClassExecutorAuthoring, ClassExecutorGrading:
		factory, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
			ImageRepository: spec.Repository, CandidateUID: 10001, CandidateGID: 10001,
			RequireRootless: true, RequireIsolatedDaemon: true,
			SeccompProfile: spec.SeccompProfile, AppArmorProfile: spec.AppArmorProfile, Now: spec.Now,
			LaunchIntent: spec.LaunchIntent,
		})
		if err != nil {
			return StartedWorkload{}, err
		}
		if spec.Class == ClassExecutorAuthoring {
			created, err := factory.Authoring(ctx, spec.Image.ImageDigest, spec.Execution.ResourcePolicy)
			if err != nil {
				return StartedWorkload{}, err
			}
			executor, ok := created.(*codingexecutor.Executor)
			if !ok {
				return StartedWorkload{}, errors.New("probe: authoring executor is not the production executor")
			}
			return StartedWorkload{ExecutorInstance: executor.InstanceID(), Wait: func() (WorkloadReceipt, error) {
				result, err := executor.Execute(ctx, spec.Workspace, spec.Command)
				return WorkloadReceipt{
					ReturnCode: result.ReturnCode, TimedOut: result.TimedOut, Completed: !result.TimedOut,
					RetainedOutputBytes: len(result.Stdout) + len(result.Stderr),
				}, err
			}}, nil
		}
		deadline := spec.Now().Add(max(30*time.Minute, spec.Command.Timeout+10*time.Minute))
		manifest, err := spec.Grading.EnforcementProbeManifest(codinghostedworker.EnforcementProbeImage{
			ImageDigest: spec.Image.ImageDigest, BuildArgv: spec.Image.BuildArgv, TestArgv: spec.Image.TestArgv,
		}, deadline)
		if err != nil {
			return StartedWorkload{}, err
		}
		created, err := factory.HostedGrading(ctx, manifest)
		if err != nil {
			return StartedWorkload{}, err
		}
		executor, ok := created.(*codingexecutor.Executor)
		if !ok {
			return StartedWorkload{}, errors.New("probe: grading executor is not the production executor")
		}
		return StartedWorkload{ExecutorInstance: executor.InstanceID(), Wait: func() (WorkloadReceipt, error) {
			result, err := executor.RunEnforcementWorkload(ctx, spec.Workspace, spec.Command)
			return WorkloadReceipt{
				ReturnCode: result.ReturnCode, TimedOut: result.TimedOut, Completed: result.Completed,
				RetainedOutputBytes: result.RetainedOutputBytes,
			}, err
		}}, nil
	}
	return StartedWorkload{}, errors.New("probe: container class is unknown")
}

// waitHarness waits for the harness workload container to exit, or its
// timeout, whichever is first; the harness has no supervisor of its own. Every
// inspect runs under a context bounded by the caller and the workload timeout,
// so a hung daemon cannot hold the wait past its deadline.
func (b ProductionResourceBackend) waitHarness(ctx context.Context, name string, timeout time.Duration) (WorkloadReceipt, error) {
	waitCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	for {
		raw, err := b.Docker.Output(waitCtx, "container", "inspect", "--format", "{{json .State}}", name)
		if waitCtx.Err() != nil {
			return WorkloadReceipt{TimedOut: true}, ctx.Err()
		}
		if err != nil {
			return WorkloadReceipt{}, errors.New("probe: harness workload container could not be inspected")
		}
		var state struct {
			Running  bool `json:"Running"`
			ExitCode int  `json:"ExitCode"`
		}
		if json.Unmarshal(raw, &state) != nil {
			return WorkloadReceipt{}, errors.New("probe: harness workload state is malformed")
		}
		if !state.Running {
			return WorkloadReceipt{ReturnCode: state.ExitCode, Completed: true}, nil
		}
		timer := time.NewTimer(harnessPollInterval)
		select {
		case <-waitCtx.Done():
			timer.Stop()
			return WorkloadReceipt{TimedOut: true}, ctx.Err()
		case <-timer.C:
		}
	}
}
