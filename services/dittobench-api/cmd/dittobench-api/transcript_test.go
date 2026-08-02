package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func sampleTranscript(order []int) transcriptArtifact {
	cases := []transcriptCase{
		{CaseID: "web_search-a-0001", Kind: protocol.KindTool,
			Response:  protocol.RunResponse{FinalText: "the index reached 3418", ToolCalls: []protocol.ObservedToolCall{{Name: "search_web", Args: json.RawMessage(`{"query":"veltrix"}`)}}},
			Observed:  []protocol.ObservedToolCall{{Name: "search_web", Args: json.RawMessage(`{"query":"veltrix"}`)}},
			Execution: runner.CaseExecution{Attempts: []runner.AttemptTelemetry{{Attempt: 1, DurationMs: 20, Outcome: "success", HTTPStatus: 200}}, TotalDurationMs: 20, TerminalOutcome: "success"}},
		{CaseID: "memory-b-0002", Kind: protocol.KindMemory, UserID: "miner",
			Response:  protocol.RunResponse{FinalText: "blue", Answer: "blue"},
			Execution: runner.CaseExecution{Attempts: []runner.AttemptTelemetry{{Attempt: 1, DurationMs: 30, Outcome: "server_error", HTTPStatus: 503}, {Attempt: 2, DurationMs: 40, Outcome: "success", HTTPStatus: 200}}, TotalDurationMs: 320, TerminalOutcome: "success"}},
		{CaseID: "memory-c-0003", Kind: protocol.KindMemory, UserID: "miner",
			Response:  protocol.RunResponse{FinalText: "not in memory", Abstain: true},
			Execution: runner.CaseExecution{Attempts: []runner.AttemptTelemetry{{Attempt: 1, DurationMs: 120000, Outcome: "timeout"}}, TotalDurationMs: 120000, TimedOut: true, TerminalOutcome: "timeout"}},
	}
	ordered := make([]transcriptCase, len(order))
	for i, idx := range order {
		ordered[i] = cases[idx]
	}
	return transcriptArtifact{
		RunID: "run-t", Seed: 7, BenchVersion: protocol.BenchVersion, DatasetSHA256: "abc", Cases: ordered,
		ModelRelay: relayExecutionSummary{Requests: 3, Successes: 2, CallerCancellations: 1, UpstreamAttempts: 4, Retries: 1},
	}
}

// The canonical bytes must not depend on per-case completion order, or the
// digest would differ between validators running the same cases concurrently.
func TestTranscriptCanonicalOrderIndependent(t *testing.T) {
	shaA, bodyA, err := sampleTranscript([]int{0, 1, 2}).canonicalBytes()
	if err != nil {
		t.Fatal(err)
	}
	shaB, bodyB, err := sampleTranscript([]int{2, 0, 1}).canonicalBytes()
	if err != nil {
		t.Fatal(err)
	}
	if shaA != shaB {
		t.Fatalf("digest depends on case order: %s vs %s", shaA, shaB)
	}
	if string(bodyA) != string(bodyB) {
		t.Fatal("canonical bytes depend on case order")
	}
	if len(shaA) != 64 {
		t.Fatalf("digest is not sha256 hex: %q", shaA)
	}
}

func TestHandleGetTranscript(t *testing.T) {
	s := &server{store: store.New()}
	runID := "run-transcript"
	s.store.Create(runID, "run_size", store.StatusRunning, 7, 3)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/runs/{id}/transcript", s.handleGetTranscript)

	// Before the transcript exists: 404, not an empty body.
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/runs/"+runID+"/transcript", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("pre-transcript status = %d, want 404", rec.Code)
	}

	sha, body, err := sampleTranscript([]int{0, 1, 2}).canonicalBytes()
	if err != nil {
		t.Fatal(err)
	}
	s.store.SetTranscript(runID, sha, body)

	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/runs/"+runID+"/transcript", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("X-Transcript-SHA256"); got != sha {
		t.Fatalf("digest header = %q, want %q", got, sha)
	}
	if rec.Body.String() != string(body) {
		t.Fatal("served bytes differ from canonical transcript")
	}
	var round transcriptArtifact
	if err := json.Unmarshal(rec.Body.Bytes(), &round); err != nil {
		t.Fatalf("served transcript is not valid JSON: %v", err)
	}
	if len(round.Cases) != 3 || round.RunID != "run-t" {
		t.Fatalf("round-trip mismatch: %+v", round)
	}
	if round.Execution.Cases != 3 || round.Execution.Succeeded != 2 || round.Execution.TimedOut != 1 || round.Execution.Retried != 1 || round.Execution.TotalAttempts != 4 {
		t.Fatalf("execution summary mismatch: %+v", round.Execution)
	}
	if round.Execution.MedianDurationMs != 320 || round.Execution.P95DurationMs != 120000 || round.Execution.MaxDurationMs != 120000 {
		t.Fatalf("execution duration summary mismatch: %+v", round.Execution)
	}
	if round.ModelRelay.Requests != 3 || round.ModelRelay.Successes != 2 || round.ModelRelay.CallerCancellations != 1 || round.ModelRelay.Retries != 1 {
		t.Fatalf("model relay summary mismatch: %+v", round.ModelRelay)
	}
}
