package main

import (
	"errors"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/store"
)

// The failure this whole change exists for: a harness that exits seconds into a
// 90-minute lease. The envelope must carry the container's own output, because
// "exit_code=1 oom_killed=false" is not a diagnosis and left four validators,
// the miner, and an operator with nothing to read.
func TestFinishSandboxRunAttachesContainerLogTail(t *testing.T) {
	backend := &diagnosticSandbox{
		diagnostics: sandbox.RuntimeDiagnostics{StateKnown: true, ExitCode: 1},
		logs:        "thread 'main' panicked at src/main.rs:42: cannot open /data/index",
	}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-dark", "sandbox", store.StatusRunning, 7, 283)
	s.store.Fail("run-dark", "harness never became healthy: harness exited before health: exit_code=1 oom_killed=false")

	s.finishSandboxRun("run-dark", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-dark")
	if job.Failure == nil {
		t.Fatal("failure envelope missing")
	}
	got, _ := job.Failure.Diagnostics["container_log_tail"].(string)
	if !strings.Contains(got, "cannot open /data/index") {
		t.Fatalf("container log tail missing from the envelope: %+v", job.Failure.Diagnostics)
	}
	if backend.logsAfter {
		t.Fatal("logs were read after Stop; docker cannot read a removed container")
	}
	if !backend.stopped {
		t.Fatal("sandbox must still be stopped")
	}
	// The log tail must not displace the machine-readable failure fields: the
	// message becomes ditto-subnet's DittobenchError text, which failure_detail
	// truncates to 200 chars on the way to the ticket.
	if strings.Contains(job.Error, "panicked") {
		t.Fatalf("log tail leaked into the 200-char failure_detail path: %q", job.Error)
	}
}

// A container that produced nothing must not produce a placeholder key. An
// operator has to be able to tell "the harness said nothing" apart from "we
// captured an empty string", which is the same distinction ditto-screener makes
// by leaving its detail untouched when no section had content.
func TestFinishSandboxRunOmitsAnEmptyContainerLogTail(t *testing.T) {
	backend := &diagnosticSandbox{
		diagnostics: sandbox.RuntimeDiagnostics{StateKnown: true, ExitCode: 1},
		logs:        "",
	}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-silent", "sandbox", store.StatusRunning, 7, 283)
	s.store.Fail("run-silent", "harness never became healthy")

	s.finishSandboxRun("run-silent", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-silent")
	if _, present := job.Failure.Diagnostics["container_log_tail"]; present {
		t.Fatalf("empty logs must be absent, not empty: %+v", job.Failure.Diagnostics)
	}
}

// Capturing logs must not alter classification. This is the guard that keeps the
// diagnostics change from quietly becoming a retry-policy change.
func TestContainerLogTailDoesNotChangeClassification(t *testing.T) {
	backend := &diagnosticSandbox{
		diagnostics: sandbox.RuntimeDiagnostics{StateKnown: true, ExitCode: 1},
		logs:        "harness crashed on boot",
	}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-crash", "sandbox", store.StatusRunning, 7, 283)
	s.store.Fail("run-crash", "harness never became healthy")

	s.finishSandboxRun("run-crash", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-crash")
	if job.Failure.Kind != "sandbox_failure" || job.Failure.Code != "sandbox_runtime" {
		t.Fatalf("an unrecognised harness crash must stay an agent fault: %+v", job.Failure)
	}
	if job.Failure.Retryable {
		t.Fatal("capturing evidence must never make a run no-fault retryable")
	}
}

// A scored-path verdict already on the job keeps its classification AND still
// gains the container evidence.
func TestContainerLogTailSurvivesThePriorVerdictMerge(t *testing.T) {
	backend := &diagnosticSandbox{
		diagnostics: sandbox.RuntimeDiagnostics{Running: true},
		logs:        "upstream 502 from inference gateway",
	}
	s := &server{store: store.New(), sandbox: backend}
	s.store.Create("run-relay", "sandbox", store.StatusRunning, 7, 283)
	s.failRelayUnavailable("run-relay", errors.New("relay preflight failed"))

	s.finishSandboxRun("run-relay", &sandbox.Handle{ContainerID: "private"})

	job, _ := s.store.Get("run-relay")
	if job.Failure.Code != "model_relay_unavailable" || !job.Failure.Retryable {
		t.Fatalf("prior verdict was clobbered: %+v", job.Failure)
	}
	got, _ := job.Failure.Diagnostics["container_log_tail"].(string)
	if !strings.Contains(got, "upstream 502") {
		t.Fatalf("evidence lost through the merge: %+v", job.Failure.Diagnostics)
	}
}
