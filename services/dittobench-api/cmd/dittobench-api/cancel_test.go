package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestCancelRunStopsActiveJob(t *testing.T) {
	s := &server{store: store.New(), runCancels: make(map[string]context.CancelFunc)}
	s.store.Create("run-1", "run_size", store.StatusRunning, 1, 114)
	ctx, cancel := context.WithCancel(context.Background())
	s.registerRunCancel("run-1", cancel)

	mux := http.NewServeMux()
	mux.HandleFunc("DELETE /v1/runs/{id}", s.handleCancelRun)
	req := httptest.NewRequest(http.MethodDelete, "/v1/runs/run-1", nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusAccepted {
		t.Fatalf("status = %d, want %d: %s", rec.Code, http.StatusAccepted, rec.Body.String())
	}
	select {
	case <-ctx.Done():
	default:
		t.Fatal("run context was not cancelled")
	}
	job, ok := s.store.Get("run-1")
	if !ok || job.Status != store.StatusFailed || job.Error != "run cancelled by client" {
		t.Fatalf("unexpected cancelled job: %+v", job)
	}

	// The worker can still be unwinding when DELETE returns. Its late terminal
	// writes must not revive the run or blame validator infrastructure.
	s.store.FailWith("run-1", "late relay failure", &store.Failure{
		Kind: "validator_infrastructure",
		Code: "model_relay_unavailable",
	})
	s.store.Finish("run-1", protocol.ScoreReport{RunID: "run-1", Composite: 1})
	job, _ = s.store.Get("run-1")
	if job.Status != store.StatusFailed || job.Error != "run cancelled by client" || job.Failure != nil || job.Report != nil {
		t.Fatalf("late worker write replaced cancellation: %+v", job)
	}
}

func TestCancelRunIsIdempotentAfterTerminalState(t *testing.T) {
	s := &server{store: store.New(), runCancels: make(map[string]context.CancelFunc)}
	s.store.Create("run-1", "run_size", store.StatusFailed, 1, 114)

	mux := http.NewServeMux()
	mux.HandleFunc("DELETE /v1/runs/{id}", s.handleCancelRun)
	req := httptest.NewRequest(http.MethodDelete, "/v1/runs/run-1", nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d: %s", rec.Code, http.StatusOK, rec.Body.String())
	}
}
