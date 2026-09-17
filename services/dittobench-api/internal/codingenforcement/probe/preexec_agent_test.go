package probe

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// checkoutRoot is the reviewed checkout the pinned fixture paths are read from.
func checkoutRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("../../../../..")
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func fixturesManifest(t *testing.T) []byte {
	t.Helper()
	return mustRead(t, "../fixtures/preexec/fixtures.json")
}

// preexecEnforcementImages is the CI image set with Rust's recorded authority
// digest set to the fixture authority, which is the one binding the agent
// requires between the two documents. Every other recorded command stays the
// image's own: the fixture suite runs the fixture's command, not the
// benchmark's.
func preexecEnforcementImages(t *testing.T) []byte {
	t.Helper()
	images, err := ciEnforcementImages("sha256:" + strings.Repeat("c", 64))
	if err != nil {
		t.Fatal(err)
	}
	fixtures, err := catalog.ParsePreexecFixtures(fixturesManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	rust := fixtures.Languages["rust"]
	decoder := json.NewDecoder(bytes.NewReader(images))
	decoder.UseNumber()
	var doc map[string]any
	if err := decoder.Decode(&doc); err != nil {
		t.Fatal(err)
	}
	image := doc["images"].(map[string]any)["rust"].(map[string]any)
	argv := image["test_argv"].(map[string]any)[rust.TestGroup].([]any)
	for index := 1; index+1 < len(argv); index += 2 {
		if argv[index] == "--authority-sha256" {
			argv[index+1] = rust.Authority.SHA256
		}
	}
	raw, err := catalog.Canonical(doc)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

type fakePreexecBackend struct {
	specs       []PreexecSpec
	stagedTrees []map[string][]string
	receipt     PreexecReceipt
	err         error
	startErr    error
	release     chan struct{}
}

func (b *fakePreexecBackend) Start(_ context.Context, spec PreexecSpec) (StartedPreexec, error) {
	if b.startErr != nil {
		return StartedPreexec{}, b.startErr
	}
	b.specs = append(b.specs, spec)
	// The staged trees are read here, while the run is live, because the agent
	// removes them once it finishes.
	staged := map[string][]string{}
	for _, directory := range []string{spec.Workspace, spec.ProtectedGrader} {
		entries, err := os.ReadDir(directory)
		if err != nil {
			return StartedPreexec{}, err
		}
		for _, entry := range entries {
			staged[directory] = append(staged[directory], entry.Name())
		}
	}
	b.stagedTrees = append(b.stagedTrees, staged)
	return StartedPreexec{ExecutorInstance: "instance-1", Wait: func() (PreexecReceipt, error) {
		if b.release != nil {
			<-b.release
		}
		return b.receipt, b.err
	}}, nil
}

func preexecConfig(t *testing.T, backend PreexecBackend) PreexecAgentConfig {
	t.Helper()
	return PreexecAgentConfig{
		ExecutionProfile: executionProfileBytes(t),
		GradingProfile:   mustRead(t, "testdata/ci-grading-profile.json"),
		// The images must name the same grading profile the agent is given.
		EnforcementImages: preexecEnforcementImages(t),
		PreexecFixtures:   fixturesManifest(t),
		Checkout:          checkoutRoot(t),
		Runner:            "/opt/ditto-coding-hosted/rev/bin/dittobench-coding-enforcement-probe",
		WorkDirectory:     t.TempDir(),
		Backend:           backend,
		RunnerSHA256:      func() (string, error) { return fakeRunnerSHA256, nil },
		FileSHA256:        preexecFileSHA256(t),
	}
}

// preexecFileSHA256 answers the runner digest for the runner path and the real
// digest for everything else, so fixture verification is exercised.
func preexecFileSHA256(t *testing.T) func(string) (string, error) {
	t.Helper()
	return func(path string) (string, error) {
		if strings.HasSuffix(path, "dittobench-coding-enforcement-probe") {
			return fakeRunnerSHA256, nil
		}
		return fileSHA256(path)
	}
}

func TestPreexecAgentStagesOnlyTheRecordedFixtureAndSuite(t *testing.T) {
	backend := &fakePreexecBackend{receipt: PreexecReceipt{Completed: true, Passed: 2, Total: 2}}
	agent, err := NewPreexecAgent(t.Context(), preexecConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	hello := agent.Handle(PreexecRequest{Op: "hello"})
	if hello.ProbeRunnerBinarySHA256 != fakeRunnerSHA256 || len(hello.Inputs) != 4 ||
		hello.Inputs["preexec_fixtures_sha256"] == "" {
		t.Fatalf("hello=%#v", hello)
	}
	fixtures, err := catalog.ParsePreexecFixtures(fixturesManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	entry := fixtures.Languages["go"]
	response := agent.Handle(PreexecRequest{
		Op: "start", Language: "go", Repository: "coding-runtime.invalid/go/runtime", Fixture: "hostile.ptrace",
	})
	if response.Error != "" || response.Run == "" || response.ExecutorInstance != "instance-1" {
		t.Fatalf("start=%#v", response)
	}
	if response.SubjectSHA256 != entry.Hostile["ptrace"].Subject.SHA256 ||
		response.SuiteSHA256 != entry.ControlsSuiteSHA256 {
		t.Fatalf("staged digests=%#v", response)
	}
	spec := backend.specs[0]
	if spec.Group != entry.TestGroup {
		t.Fatalf("spec group = %s", spec.Group)
	}
	if !slices.Equal(spec.Image.TestArgv[spec.Group], entry.TestArgv) {
		t.Fatalf("spec runs %v, not the recorded fixture command", spec.Image.TestArgv[spec.Group])
	}
	if slices.Equal(spec.Image.TestArgv["visible"], entry.TestArgv) {
		t.Fatal("the fixture command replaced another group's recorded command")
	}
	if spec.ExpectedTotal != entry.ExpectedTotal {
		t.Fatalf("spec expected total = %d", spec.ExpectedTotal)
	}
	workspace := backend.stagedTrees[0][spec.Workspace]
	protected := backend.stagedTrees[0][spec.ProtectedGrader]
	slices.Sort(workspace)
	if !slices.Equal(workspace, []string{"go.mod", "subject.go"}) {
		t.Fatalf("workspace = %v", workspace)
	}
	if !slices.Equal(protected, []string{"hidden_test.go"}) {
		t.Fatalf("protected grader = %v", protected)
	}
	done := agent.Handle(PreexecRequest{Op: "wait", Run: response.Run, TimeoutMS: 30000})
	if !done.Done || done.Passed == nil || *done.Passed != 2 || done.Total == nil || *done.Total != 2 {
		t.Fatalf("wait=%#v", done)
	}
	if _, err := os.Stat(spec.Workspace); !os.IsNotExist(err) {
		t.Fatal("the workspace outlived its run")
	}
}

// The suite is protected material: it is never staged where the candidate's
// own workspace can read it.
func TestPreexecAgentNeverStagesTheSuiteInTheWorkspace(t *testing.T) {
	backend := &fakePreexecBackend{receipt: PreexecReceipt{Completed: true, Passed: 2, Total: 2}}
	agent, err := NewPreexecAgent(t.Context(), preexecConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	for _, language := range catalog.Languages {
		response := agent.Handle(PreexecRequest{
			Op: "start", Language: language, Repository: "coding-runtime.invalid/runtime", Fixture: "control.pass",
		})
		if response.Error != "" {
			t.Fatalf("%s start: %s", language, response.Error)
		}
		spec := backend.specs[len(backend.specs)-1]
		suite := filepath.Base(fixtureSuitePath(t, language))
		for _, name := range backend.stagedTrees[len(backend.stagedTrees)-1][spec.Workspace] {
			if name == suite {
				t.Fatalf("%s staged the hidden suite in the workspace", language)
			}
		}
		agent.Handle(PreexecRequest{Op: "wait", Run: response.Run, TimeoutMS: 30000})
	}
}

func fixtureSuitePath(t *testing.T, language string) string {
	t.Helper()
	fixtures, err := catalog.ParsePreexecFixtures(fixturesManifest(t))
	if err != nil {
		t.Fatal(err)
	}
	return fixtures.Languages[language].ControlsSuite.Path
}

func TestPreexecAgentRefusesRequestsOutsideThePinnedSet(t *testing.T) {
	backend := &fakePreexecBackend{receipt: PreexecReceipt{Completed: true, Passed: 2, Total: 2}}
	agent, err := NewPreexecAgent(t.Context(), preexecConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	for _, testCase := range []struct {
		name    string
		request PreexecRequest
	}{
		{"unknown language", PreexecRequest{Op: "start", Language: "ruby", Repository: "r.invalid/x", Fixture: "control.pass"}},
		{"unknown control", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x", Fixture: "control.other"}},
		{"unknown hostile", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x", Fixture: "hostile.rootkit"}},
		{"unknown phase", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x", Fixture: "identity.candidate"}},
		{"malformed fixture id", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x", Fixture: "control"}},
		{"digest in the repository", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x@sha256:0", Fixture: "control.pass"}},
		{"traversing repository", PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/../x", Fixture: "control.pass"}},
		{"unknown op", PreexecRequest{Op: "collect"}},
		{"unknown run", PreexecRequest{Op: "wait", Run: "p99", TimeoutMS: 1}},
		{"malformed run id", PreexecRequest{Op: "wait", Run: "r0", TimeoutMS: 1}},
		{"wait beyond the bound", PreexecRequest{Op: "wait", Run: "p0", TimeoutMS: 1 << 30}},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if response := agent.Handle(testCase.request); response.Error == "" {
				t.Fatalf("the request was accepted: %#v", response)
			}
		})
	}
	if len(backend.specs) != 0 {
		t.Fatal("a refused request reached the backend")
	}
}

// A build that never reached the test command is reported as such: the suite
// wrote no receipt, so there is no confinement observation to read.
func TestPreexecAgentReportsABuildThatNeverRanTheSuite(t *testing.T) {
	backend := &fakePreexecBackend{receipt: PreexecReceipt{ReturnCode: 2, Completed: true, BuildFailed: true}}
	agent, err := NewPreexecAgent(t.Context(), preexecConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	start := agent.Handle(PreexecRequest{Op: "start", Language: "go", Repository: "r.invalid/x", Fixture: "hostile.mount"})
	if start.Error != "" {
		t.Fatal(start.Error)
	}
	done := agent.Handle(PreexecRequest{Op: "wait", Run: start.Run, TimeoutMS: 30000})
	if !done.BuildFailed || done.Passed == nil || *done.Passed != 0 {
		t.Fatalf("wait=%#v", done)
	}
}

func TestPreexecAgentStartupRefusesInconsistentAuthority(t *testing.T) {
	backend := &fakePreexecBackend{}
	for _, testCase := range []struct {
		name string
		turn func(*PreexecAgentConfig)
	}{
		{"rust authority differs from the recorded rust command", func(config *PreexecAgentConfig) {
			images, err := ciEnforcementImages("sha256:" + strings.Repeat("c", 64))
			if err != nil {
				t.Fatal(err)
			}
			config.EnforcementImages = images
		}},
		{"malformed fixture manifest", func(config *PreexecAgentConfig) {
			config.PreexecFixtures = []byte(`{"schema":"other"}`)
		}},
		{"checkout without the recorded fixtures", func(config *PreexecAgentConfig) {
			config.Checkout = t.TempDir()
		}},
		{"relative checkout", func(config *PreexecAgentConfig) { config.Checkout = "." }},
		{"no backend", func(config *PreexecAgentConfig) { config.Backend = nil }},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			config := preexecConfig(t, backend)
			testCase.turn(&config)
			if _, err := NewPreexecAgent(t.Context(), config); err == nil {
				t.Fatal("the agent started")
			}
		})
	}
}

func TestDecodePreexecRequestRefusesUnknownFields(t *testing.T) {
	if _, err := DecodePreexecRequest([]byte(`{"op":"start","class":"harness"}`)); err == nil {
		t.Fatal("an unknown field was accepted")
	}
	request, err := DecodePreexecRequest([]byte(`{"op":"start","language":"go","fixture":"control.pass"}`))
	if err != nil || request.Fixture != "control.pass" {
		t.Fatalf("request=%#v err=%v", request, err)
	}
}
