package probe

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
)

const fakeRunnerSHA256 = "1111111111111111111111111111111111111111111111111111111111111111"

type fakeBackend struct {
	mu      sync.Mutex
	specs   []WorkloadSpec
	release chan struct{}
	err     error
	receipt WorkloadReceipt
	waitErr error
}

func (b *fakeBackend) Start(ctx context.Context, spec WorkloadSpec) (StartedWorkload, error) {
	b.mu.Lock()
	b.specs = append(b.specs, spec)
	b.mu.Unlock()
	if b.err != nil {
		return StartedWorkload{}, b.err
	}
	if spec.Workspace != "" {
		if _, err := os.Stat(filepath.Join(spec.Workspace, WorkloadRunnerName)); err != nil {
			return StartedWorkload{}, errors.New("workspace lacks the runner copy")
		}
	}
	return StartedWorkload{ExecutorInstance: "coding-executor-" + strings.Repeat("a", 32), Wait: func() (WorkloadReceipt, error) {
		select {
		case <-b.release:
		case <-ctx.Done():
			return WorkloadReceipt{}, ctx.Err()
		}
		return b.receipt, b.waitErr
	}}, nil
}

func executionProfileBytes(t *testing.T) []byte {
	t.Helper()
	grading, err := parseGradingProfile(mustRead(t, "testdata/ci-grading-profile.json"))
	if err != nil {
		t.Fatal(err)
	}
	policy := grading.ResourcePolicy
	profile := codinghostedinput.Profile{
		Schema: "dittobench-coding-hosted-authoring-profile-v2", ImageDigest: "sha256:" + strings.Repeat("a", 64),
		ResourcePolicy: policy, Budgets: codingcontract.Budgets{
			ModelInputTokens: 10000, ModelOutputTokens: 1000, WorkspaceToolCalls: policy.CandidateLimits.MaxToolCalls, WallTimeSeconds: 600,
		},
	}
	body, err := json.Marshal(profile)
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value map[string]any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	body, err = json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func agentConfig(t *testing.T, backend ResourceBackend) ResourceAgentConfig {
	t.Helper()
	images, err := ciEnforcementImages("sha256:" + strings.Repeat("c", 64))
	if err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(t.TempDir(), "runner")
	if err := os.WriteFile(source, []byte("measured runner"), 0o555); err != nil {
		t.Fatal(err)
	}
	return ResourceAgentConfig{
		ExecutionProfile: executionProfileBytes(t), GradingProfile: mustRead(t, "testdata/ci-grading-profile.json"),
		EnforcementImages: images, Runner: "/opt/ditto-coding-hosted/rev/bin/dittobench-coding-enforcement-probe",
		WorkDirectory: t.TempDir(), Backend: backend, RunnerSource: source,
		RunnerSHA256: func() (string, error) { return fakeRunnerSHA256, nil },
		FileSHA256:   func(string) (string, error) { return fakeRunnerSHA256, nil },
	}
}

func startRequest(class, mode string, extra ...string) ResourceRequest {
	return ResourceRequest{
		Op: "start", Class: class, Language: "go", Repository: "coding-runtime.invalid/go/runtime",
		Workload: append([]string{mode, "--nonce", "0123456789abcdef"}, extra...), TimeoutMS: 5000,
	}
}

func TestResourceAgentLaunchesOnlyTheWorkloadThroughEachClass(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{}), receipt: WorkloadReceipt{ReturnCode: 0, Completed: true, RetainedOutputBytes: 24576}}
	agent, err := NewResourceAgent(t.Context(), agentConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	hello := agent.Handle(ResourceRequest{Op: "hello"})
	if hello.ProbeRunnerBinarySHA256 != fakeRunnerSHA256 || hello.PID != os.Getpid() || len(hello.Inputs) != 3 {
		t.Fatalf("hello=%#v", hello)
	}
	for _, class := range []string{ClassHarness, ClassExecutorAuthoring, ClassExecutorGrading} {
		response := agent.Handle(startRequest(class, "hold", "--hold-ms", "10"))
		if response.Error != "" || response.Run == "" || response.CommandTimeoutMS != 5000 {
			t.Fatalf("%s start=%#v", class, response)
		}
	}
	harness, authoring := backend.specs[0], backend.specs[1]
	if !slices.Equal(harness.Command.Argv, []string{"workload", "hold", "--nonce", "0123456789abcdef", "--hold-ms", "10"}) || harness.Workspace != "" {
		t.Fatalf("harness spec=%#v", harness)
	}
	wantExecutor := append(slices.Clone(codingexecutor.EnforcementWorkloadPrefix), "hold", "--nonce", "0123456789abcdef", "--hold-ms", "10")
	if !slices.Equal(authoring.Command.Argv, wantExecutor) || authoring.Workspace == "" || authoring.Image.ImageDigest == "" {
		t.Fatalf("authoring spec=%#v", authoring)
	}
	if waited := agent.Handle(ResourceRequest{Op: "wait", Run: "r0", TimeoutMS: 10}); waited.Done || waited.Error != "" {
		t.Fatalf("an unfinished run reported done: %#v", waited)
	}
	close(backend.release)
	waited := agent.Handle(ResourceRequest{Op: "wait", Run: "r1", TimeoutMS: 2000})
	if !waited.Done || waited.RunFailed || *waited.ReturnCode != 0 || *waited.RetainedOutputBytes != 24576 || !waited.Completed {
		t.Fatalf("wait=%#v", waited)
	}
	// The workspace and its runner copy are gone once the run finished.
	if _, err := os.Stat(authoring.Workspace); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("probe workspace was not removed")
	}
}

func TestResourceAgentTakesTheApprovedGroupTimeout(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{})}
	close(backend.release)
	agent, err := NewResourceAgent(t.Context(), agentConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	request := startRequest(ClassExecutorGrading, "hang", "--seconds", "700")
	request.TimeoutMS, request.TestGroup = 0, "visible"
	response := agent.Handle(request)
	if response.Error != "" || backend.specs[0].Command.Timeout != agent.grading.TestGroups[1].Command.Timeout ||
		response.CommandTimeoutMS != int(agent.grading.TestGroups[1].Command.Timeout/time.Millisecond) {
		t.Fatalf("response=%#v spec=%#v", response, backend.specs)
	}
	for _, bad := range []func(*ResourceRequest){
		func(r *ResourceRequest) { r.TimeoutMS = 1000 },
		func(r *ResourceRequest) { r.TestGroup = "private" },
		func(r *ResourceRequest) { r.Class = ClassExecutorAuthoring },
	} {
		candidate := request
		bad(&candidate)
		if response := agent.Handle(candidate); response.Error == "" {
			t.Fatalf("accepted %#v", candidate)
		}
	}
}

func TestResourceAgentRefusesMalformedRequestsAndInputs(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{})}
	agent, err := NewResourceAgent(t.Context(), agentConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*ResourceRequest){
		func(r *ResourceRequest) { r.Class = "sibling" },
		func(r *ResourceRequest) { r.Language = "java" },
		func(r *ResourceRequest) { r.Repository = "registry/../other" },
		func(r *ResourceRequest) { r.Repository = "registry/go@sha256:" + strings.Repeat("a", 64) },
		func(r *ResourceRequest) { r.Workload = []string{"net-agent"} },
		func(r *ResourceRequest) { r.Workload = []string{"hold", "--nonce", "zz"} },
		func(r *ResourceRequest) { r.Workload = []string{"hold", "--nonce", "0123456789abcdef", "--bytes", "5"} },
		func(r *ResourceRequest) { r.TimeoutMS = 0 },
		func(r *ResourceRequest) { r.FailStart = true; r.Class = ClassExecutorAuthoring },
	} {
		request := startRequest(ClassHarness, "hold")
		change(&request)
		if response := agent.Handle(request); response.Error == "" {
			t.Fatalf("accepted %#v", request)
		}
	}
	if len(backend.specs) != 0 {
		t.Fatal("a refused request reached the backend")
	}
	if _, err := DecodeResourceRequest([]byte(`{"op":"start","image":"x"}`)); err == nil {
		t.Fatal("unknown request key accepted")
	}

	config := agentConfig(t, backend)
	config.FileSHA256 = func(string) (string, error) { return strings.Repeat("2", 64), nil }
	if _, err := NewResourceAgent(t.Context(), config); err == nil {
		t.Fatal("a runner path that is not the running binary was accepted")
	}
	config = agentConfig(t, backend)
	config.ExecutionProfile = append([]byte(" "), config.ExecutionProfile...)
	if _, err := NewResourceAgent(t.Context(), config); err == nil {
		t.Fatal("non-canonical execution profile accepted")
	}
	config = agentConfig(t, backend)
	config.EnforcementImages, _ = ciEnforcementImages("sha256:" + strings.Repeat("d", 64))
	config.GradingProfile = append(bytes.TrimSuffix(config.GradingProfile, []byte("\n")), ' ', '\n')
	if _, err := NewResourceAgent(t.Context(), config); err == nil {
		t.Fatal("a grading profile the image set does not name was accepted")
	}
}

func TestServeResourceAgentCancelsRunsOnTermination(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{})}
	ctx, cancel := context.WithCancel(t.Context())
	agent, err := NewResourceAgent(ctx, agentConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	reader, writer := ioPipe()
	var output syncBuffer
	done := make(chan error, 1)
	go func() { done <- ServeResourceAgent(ctx, agent, reader, &output) }()
	line, _ := json.Marshal(startRequest(ClassExecutorAuthoring, "hang", "--seconds", "600"))
	_, _ = writer.Write(append(line, '\n'))
	deadline := time.Now().Add(2 * time.Second)
	for !strings.Contains(output.String(), `"run":"r0"`) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("termination did not end the session")
	}
	agent.mu.Lock()
	run := agent.runs["r0"]
	agent.mu.Unlock()
	select {
	case <-run.done:
	default:
		t.Fatal("the run was not finished before the agent returned")
	}
	if !errors.Is(run.err, context.Canceled) {
		t.Fatalf("run err=%v", run.err)
	}
}

func TestResourceAgentPassesOnlyAHarnessFailedStart(t *testing.T) {
	backend := &fakeBackend{release: make(chan struct{})}
	close(backend.release)
	agent, err := NewResourceAgent(t.Context(), agentConfig(t, backend))
	if err != nil {
		t.Fatal(err)
	}
	request := startRequest(ClassHarness, "hold", "--hold-ms", "10")
	request.FailStart = true
	if response := agent.Handle(request); response.Error != "" || len(backend.specs) != 1 || !backend.specs[0].FailStart {
		t.Fatalf("response=%#v specs=%#v", response, backend.specs)
	}
	if response := agent.Handle(startRequest(ClassHarness, "hold", "--hold-ms", "10")); response.Error != "" || backend.specs[1].FailStart {
		t.Fatalf("an ordinary start was marked to fail: %#v", response)
	}
}

// hangingDocker blocks every call until its context ends, as a wedged daemon
// would, and records whether each call carried a deadline.
type hangingDocker struct {
	mu        sync.Mutex
	deadlines []bool
}

func (h *hangingDocker) Output(ctx context.Context, _ ...string) ([]byte, error) {
	_, bounded := ctx.Deadline()
	h.mu.Lock()
	h.deadlines = append(h.deadlines, bounded)
	h.mu.Unlock()
	<-ctx.Done()
	return nil, ctx.Err()
}

func TestHarnessWaitBoundsAHungInspectByTheWorkloadTimeout(t *testing.T) {
	docker := &hangingDocker{}
	backend := ProductionResourceBackend{Docker: docker}
	started := time.Now()
	receipt, err := backend.waitHarness(context.Background(), "harness", 50*time.Millisecond)
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("a hung inspect held the wait for %s", elapsed)
	}
	if err != nil || !receipt.TimedOut || receipt.Completed {
		t.Fatalf("a hung inspect must time the workload out: %+v %v", receipt, err)
	}
	if len(docker.deadlines) != 1 || !docker.deadlines[0] {
		t.Fatalf("inspect did not carry the workload deadline: %v", docker.deadlines)
	}
}

func TestHarnessWaitPropagatesCallerCancellationIntoInspect(t *testing.T) {
	docker := &hangingDocker{}
	backend := ProductionResourceBackend{Docker: docker}
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(20*time.Millisecond, cancel)
	receipt, err := backend.waitHarness(ctx, "harness", time.Hour)
	if !errors.Is(err, context.Canceled) || !receipt.TimedOut {
		t.Fatalf("caller cancellation must end a hung inspect: %+v %v", receipt, err)
	}
}

func TestHarnessWaitReturnsTheExitedContainerCode(t *testing.T) {
	docker := &fakeDocker{responses: map[string][]byte{
		"container inspect --format {{json .State}} harness": []byte(`{"Running":false,"ExitCode":3}`),
	}}
	receipt, err := ProductionResourceBackend{Docker: docker}.waitHarness(context.Background(), "harness", time.Minute)
	if err != nil || !receipt.Completed || receipt.ReturnCode != 3 {
		t.Fatalf("exited harness receipt: %+v %v", receipt, err)
	}
}
