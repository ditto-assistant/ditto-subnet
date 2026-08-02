package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type diagnosticSandbox struct {
	diagnostics    sandbox.RuntimeDiagnostics
	logs           string
	logsAfter      bool // true once Stop ran, proving Logs was NOT read before teardown
	stopped        bool
	v8IsolationErr error
}

func (*diagnosticSandbox) Available(context.Context) error { return nil }
func (s *diagnosticSandbox) V8IsolationReady(context.Context) error {
	return s.v8IsolationErr
}
func (*diagnosticSandbox) Build(context.Context, sandbox.Source) (string, string, *protocol.CodeFingerprint, error) {
	return "", "", nil, nil
}
func (*diagnosticSandbox) Run(context.Context, string, map[string]string) (*sandbox.Handle, error) {
	return nil, nil
}
func (*diagnosticSandbox) Release(context.Context, string) {}
func (s *diagnosticSandbox) Stop(context.Context, *sandbox.Handle) {
	s.stopped = true
}
func (s *diagnosticSandbox) Diagnostics(context.Context, *sandbox.Handle) sandbox.RuntimeDiagnostics {
	return s.diagnostics
}
func (s *diagnosticSandbox) Logs(context.Context, *sandbox.Handle) string {
	if s.stopped {
		s.logsAfter = true
	}
	return s.logs
}

func TestFullRunConcurrencyIsOnePerScorer(t *testing.T) {
	s := &server{
		store:    store.New(),
		runSlots: make(chan struct{}, maxConcurrentRuns),
	}
	if cap(s.runSlots) != 1 {
		t.Fatalf("full miner sandbox concurrency = %d, want 1", cap(s.runSlots))
	}
	s.runSlots <- struct{}{}

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/v1/submit", nil)
	s.submitRunSize(recorder, request, submitRequest{
		HarnessURL: "http://127.0.0.1:8080",
		RunSize:    "full",
	})
	if recorder.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429: %s", recorder.Code, recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), "validator at capacity") {
		t.Fatalf("capacity response missing retry guidance: %s", recorder.Body.String())
	}
}

func TestFinishSandboxRunClassifiesOOMBeforeStop(t *testing.T) {
	backend := &diagnosticSandbox{diagnostics: sandbox.RuntimeDiagnostics{
		OOMKilled:       true,
		ExitCode:        137,
		MemoryEvents:    map[string]uint64{"oom": 1, "oom_kill": 1},
		MemoryPeakBytes: pointer(uint64(3 << 30)),
	}}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-oom", "sandbox", store.StatusRunning, 1, 114)
	s.store.Fail("run-oom", "private miner output and container id")

	s.finishSandboxRun("run-oom", &sandbox.Handle{ContainerID: "private"})
	job, _ := s.store.Get("run-oom")
	if !backend.stopped {
		t.Fatal("sandbox must be stopped after diagnostics")
	}
	if job.Error != "validator sandbox resource envelope exhausted" {
		t.Fatalf("private error was not replaced: %q", job.Error)
	}
	if job.Failure == nil || job.Failure.Code != "sandbox_oom" || !job.Failure.Retryable {
		t.Fatalf("infrastructure classifier missing: %+v", job.Failure)
	}
}

func pointer[T any](value T) *T { return &value }

// A run the scored path already classified as validator infrastructure must
// keep that classification through the deferred sandbox teardown. Overwriting
// it with the generic sandbox_runtime envelope is what billed platform-side
// lease revocations to the miner: ditto-subnet only honours a failure whose
// kind is "validator_infrastructure" AND whose retryable is true, so a
// downgraded run became fail_job("scoring_error") and spent a real attempt.
func TestFinishSandboxRunPreservesScoredPathInfrastructureVerdict(t *testing.T) {
	backend := &diagnosticSandbox{diagnostics: sandbox.RuntimeDiagnostics{
		Running:      true,
		ExitCode:     0,
		MemoryEvents: map[string]uint64{},
	}}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-denied", "sandbox", store.StatusRunning, 7, 283)
	s.failRelayUnavailable(
		"run-denied",
		errors.New("platform declined this run's inference grant 469 time(s) during benchmark"),
	)

	s.finishSandboxRun("run-denied", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-denied")
	if job.Failure == nil {
		t.Fatal("failure envelope missing")
	}
	if job.Failure.Kind != "validator_infrastructure" {
		t.Fatalf("kind = %q, want validator_infrastructure", job.Failure.Kind)
	}
	if job.Failure.Code != "model_relay_unavailable" {
		t.Fatalf("code = %q, want model_relay_unavailable", job.Failure.Code)
	}
	if !job.Failure.Retryable {
		t.Fatal("a platform lease denial must stay retryable, or the miner is charged")
	}
	if !strings.Contains(job.Error, "locked model relay unavailable") {
		t.Fatalf("scored-path message lost: %q", job.Error)
	}
	// The diagnostics this path exists to collect are still folded in.
	if _, ok := job.Failure.Diagnostics["exit_code"]; !ok {
		t.Fatalf("sandbox diagnostics not attached: %+v", job.Failure.Diagnostics)
	}
}

// With no prior verdict the generic sandbox envelope is still the right answer.
func TestFinishSandboxRunStillClassifiesUnattributedFailures(t *testing.T) {
	backend := &diagnosticSandbox{diagnostics: sandbox.RuntimeDiagnostics{
		ExitCode:     1,
		MemoryEvents: map[string]uint64{},
	}}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-plain", "sandbox", store.StatusRunning, 7, 283)
	s.store.Fail("run-plain", "harness exited non-zero")

	s.finishSandboxRun("run-plain", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-plain")
	if job.Failure == nil || job.Failure.Kind != "sandbox_failure" || job.Failure.Retryable {
		t.Fatalf("unattributed failure must stay agent-billed: %+v", job.Failure)
	}
}
