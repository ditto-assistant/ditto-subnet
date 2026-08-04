package store

import (
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestCreateFinishGet(t *testing.T) {
	s := New()
	if _, ok := s.Get("nope"); ok {
		t.Fatalf("expected miss for unknown id")
	}

	s.Create("r1", "direct", StatusRunning, 42, 10)
	got, ok := s.Get("r1")
	if !ok || got.Status != StatusRunning || got.Seed != 42 {
		t.Fatalf("expected running job seed 42, got ok=%v %+v", ok, got)
	}

	s.Finish("r1", protocol.ScoreReport{RunID: "r1", Composite: 0.5})
	got, _ = s.Get("r1")
	if got.Status != StatusDone {
		t.Fatalf("expected done, got %q", got.Status)
	}
	if got.Report == nil || got.Report.Composite != 0.5 {
		t.Fatalf("expected report composite 0.5, got %+v", got.Report)
	}
}

func TestFail(t *testing.T) {
	s := New()
	s.Create("r2", "sandbox", StatusBuilding, 1, 5)
	s.Fail("r2", "boom")
	got, _ := s.Get("r2")
	if got.Status != StatusFailed || got.Error != "boom" {
		t.Fatalf("expected failed/boom, got %q/%q", got.Status, got.Error)
	}
}

func TestCancelIfActiveMakesCancellationSticky(t *testing.T) {
	s := New()
	s.Create("cancelled", "run_size", StatusRunning, 1, 5)

	job, found, transitioned := s.CancelIfActive("cancelled", "run cancelled by client")
	if !found || !transitioned || job.Status != StatusFailed {
		t.Fatalf("cancel transition = found %t transitioned %t job %+v", found, transitioned, job)
	}

	s.SetStatus("cancelled", StatusScoring)
	s.SetStage("cancelled", StatusScoring, 5, 5)
	s.SetRelayWaiting("cancelled", true)
	s.AppendPartial("cancelled", protocol.CaseScore{CaseID: "late"})
	s.FailWith("cancelled", "late infrastructure failure", &Failure{
		Kind: "validator_infrastructure",
		Code: "model_relay_unavailable",
	})
	s.SetTranscript("cancelled", "late-sha", []byte("late transcript"))
	s.Finish("cancelled", protocol.ScoreReport{RunID: "cancelled", Composite: 1})

	got, _ := s.Get("cancelled")
	if got.Status != StatusFailed || got.Error != "run cancelled by client" || got.Failure != nil {
		t.Fatalf("late writes replaced cancellation: %+v", got)
	}
	if got.Report != nil || len(got.Partial) != 0 || got.TranscriptSHA256 != "" {
		t.Fatalf("late writes mutated cancelled job: %+v", got)
	}

	again, found, transitioned := s.CancelIfActive("cancelled", "replacement")
	if !found || transitioned || again.Error != "run cancelled by client" {
		t.Fatalf("repeat cancel was not idempotent: found %t transitioned %t job %+v", found, transitioned, again)
	}
}

func TestFailIfActiveKeepsClassifiedFailureSticky(t *testing.T) {
	s := New()
	s.Create("allowance", "run_size", StatusRunning, 1, 5)
	failure := &Failure{
		Kind:      "sandbox_failure",
		Code:      "inference_allowance_exhausted",
		Retryable: false,
	}

	job, found, transitioned := s.FailIfActive(
		"allowance", "harness exhausted its inference allowance", failure,
	)
	if !found || !transitioned || job.Status != StatusFailed || job.Failure != failure {
		t.Fatalf("failure transition = found %t transitioned %t job %+v", found, transitioned, job)
	}

	s.SetStage("allowance", StatusScoring, 5, 5)
	s.AppendPartial("allowance", protocol.CaseScore{CaseID: "late"})
	s.Fail("allowance", "late worker failure")
	s.Finish("allowance", protocol.ScoreReport{RunID: "allowance", Composite: 1})

	got, _ := s.Get("allowance")
	if got.Status != StatusFailed || got.Error != "harness exhausted its inference allowance" || got.Failure != failure {
		t.Fatalf("late writes replaced classified failure: %+v", got)
	}
	if got.Report != nil || len(got.Partial) != 0 {
		t.Fatalf("late writes mutated terminal job: %+v", got)
	}
}

func TestFailWithSanitizedInfrastructureClassification(t *testing.T) {
	s := New()
	s.Create("r-infra", "sandbox", StatusRunning, 1, 5)
	failure := &Failure{
		Kind:      "validator_infrastructure",
		Code:      "sandbox_oom",
		Retryable: true,
		Diagnostics: map[string]any{
			"oom_killed":        true,
			"memory_peak_bytes": uint64(3 << 30),
		},
	}
	s.FailWith("r-infra", "validator sandbox resource envelope exhausted", failure)
	got, _ := s.Get("r-infra")
	if got.Failure == nil || got.Failure.Kind != "validator_infrastructure" || !got.Failure.Retryable {
		t.Fatalf("sanitized failure missing: %+v", got)
	}
}

func TestGetReturnsCopy(t *testing.T) {
	s := New()
	s.Create("r3", "direct", StatusQueued, 0, 0)
	got, _ := s.Get("r3")
	got.Status = StatusDone // mutate the copy
	again, _ := s.Get("r3")
	if again.Status != StatusQueued {
		t.Fatalf("Get must return a copy; stored job was mutated to %q", again.Status)
	}
}

func TestRunSizeProgressAndPartial(t *testing.T) {
	s := New()
	s.Create("r", "run_size", StatusQueued, 5, 12)
	s.SetRunSize("r", "small")
	s.SetBenchVersion("r", 3)
	s.SetStage("r", StatusGenerating, 0, 12)

	got, _ := s.Get("r")
	if got.RunSize != "small" {
		t.Fatalf("expected run_size small, got %q", got.RunSize)
	}
	if got.BenchVersion != 3 {
		t.Fatalf("expected bench_version 3, got %d", got.BenchVersion)
	}
	if got.Status != StatusGenerating || got.Progress.Stage != string(StatusGenerating) || got.Progress.Total != 12 {
		t.Fatalf("stage not set: %+v", got)
	}

	s.AppendPartial("r", protocol.CaseScore{CaseID: "c1", Score: 1.0})
	s.AppendPartial("r", protocol.CaseScore{CaseID: "c2", Score: 0.0})
	got, _ = s.Get("r")
	if len(got.Partial) != 2 {
		t.Fatalf("expected 2 partials, got %d", len(got.Partial))
	}
	if got.Progress.Done != 2 {
		t.Fatalf("expected progress done 2, got %d", got.Progress.Done)
	}
}

func TestRelayWaitPreservesAndRestoresRunningProgress(t *testing.T) {
	s := New()
	s.Create("relay", "run_size", StatusQueued, 5, 12)
	s.SetStage("relay", StatusRunning, 4, 12)

	s.SetRelayWaiting("relay", true)
	waiting, _ := s.Get("relay")
	if waiting.Status != StatusWaitingRelay || waiting.Progress.Stage != string(StatusWaitingRelay) {
		t.Fatalf("relay wait not surfaced: %+v", waiting)
	}
	if waiting.Progress.Done != 4 || waiting.Progress.Total != 12 {
		t.Fatalf("relay wait lost progress: %+v", waiting.Progress)
	}
	s.SetStage("relay", StatusScoring, 12, 12)
	stillWaiting, _ := s.Get("relay")
	if stillWaiting.Status != StatusWaitingRelay || stillWaiting.Progress.Stage != string(StatusWaitingRelay) {
		t.Fatalf("stage update hid active relay wait: %+v", stillWaiting)
	}

	s.SetRelayWaiting("relay", false)
	resumed, _ := s.Get("relay")
	if resumed.Status != StatusScoring || resumed.Progress.Stage != string(StatusScoring) {
		t.Fatalf("relay wait did not restore latest stage: %+v", resumed)
	}
	if resumed.Progress.Done != 12 || resumed.Progress.Total != 12 {
		t.Fatalf("relay resume lost progress: %+v", resumed.Progress)
	}
}

func TestConcurrentAccess(t *testing.T) {
	s := New()
	s.Create("r", "direct", StatusRunning, 0, 0)
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			s.Update("r", func(j *Job) { j.N = n })
			s.Get("r")
		}(i)
	}
	wg.Wait()
}
