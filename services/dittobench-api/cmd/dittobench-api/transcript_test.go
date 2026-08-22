package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/store"
	"github.com/ditto-assistant/dittobench-datagen/gen"
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
		ModelRelay: relayExecutionSummary{Requests: 3, Successes: 2, CallerCancellations: 1, UpstreamAttempts: 4, Retries: 1, RouteProbeAttempts: 2, RouteProbeRouted: 1},
	}
}

func TestV9PublicTranscriptCommitsToButNeverPublishesProjectionSecrets(t *testing.T) {
	manifest := gen.HarnessProjectionManifest{
		BlindingKey: "1111111111111111111111111111111111111111111111111111111111111111",
		Users:       []gen.IDProjection{{Internal: "miner", Wire: "8ec86f06-e794-4d1c-a920-97d3fcf5ce8b"}},
		Cases:       []gen.IDProjection{{Internal: "memory-canary-0001", Wire: "f0e310c2-8c21-42e1-9e85-17d34ca9d51a"}},
		Pairs:       []gen.ScopedIDProjection{{UserID: "miner", Internal: "project-04-origin", Wire: "f493ee76-36e6-49da-b842-03378db9d35c"}},
		Sessions:    []gen.ScopedIDProjection{{UserID: "miner", Internal: "project-04", Wire: "4d86aa61-8bde-444e-88a0-6e4346ee8fb2"}},
		Subjects:    []gen.ScopedIDProjection{{UserID: "miner", Internal: "project-subject", Wire: "14f16420-b235-4552-b15e-e3940fac9001"}},
	}
	projectionSHA, privateBody, err := projectionReplayArtifact(manifest)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(privateBody), manifest.BlindingKey) {
		t.Fatal("private replay artifact did not retain required key material")
	}
	public := transcriptArtifact{
		RunID: "run-v9", Seed: 537, BenchVersion: protocol.BenchVersionV9,
		DatasetSHA256: "dataset", ProjectionSHA256: projectionSHA,
		Cases: []transcriptCase{{CaseID: "memory-canary-0001", Kind: protocol.KindMemory, Response: protocol.RunResponse{FinalText: "canonical answer", Answer: "canonical answer"}}},
	}
	sha, body, err := public.canonicalBytes()
	if err != nil {
		t.Fatal(err)
	}

	s := &server{store: store.New()}
	s.store.Create("run-v9", "run_size", store.StatusRunning, 537, 1)
	s.store.SetTranscript("run-v9", sha, body)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/runs/{id}/transcript", s.handleGetTranscript)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/runs/run-v9/transcript", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	served := rec.Body.String()
	for _, forbidden := range []string{
		manifest.BlindingKey, "8ec86f06-e794-4d1c-a920-97d3fcf5ce8b",
		"f0e310c2-8c21-42e1-9e85-17d34ca9d51a", "f493ee76-36e6-49da-b842-03378db9d35c",
		"4d86aa61-8bde-444e-88a0-6e4346ee8fb2", "14f16420-b235-4552-b15e-e3940fac9001",
		"harness_projection", "blinding_key", "tool_case_order", `"user_id":"miner"`, `"user_id":"colleague"`,
	} {
		if strings.Contains(served, forbidden) {
			t.Errorf("public transcript leaked %q: %s", forbidden, served)
		}
	}
	if !strings.Contains(served, `"projection_sha256":"`+projectionSHA+`"`) {
		t.Errorf("public transcript omitted projection commitment: %s", served)
	}
	if !strings.Contains(served, `"case_id":"memory-canary-0001"`) || !strings.Contains(served, "canonical answer") {
		t.Errorf("public transcript lost canonical offline-regrading inputs: %s", served)
	}
}

func TestV9PrivateProjectionArtifactIsSeparateAndOwnerOnly(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "private-projections")
	runID := "7e4283a1-92f8-4f41-914e-b508aca0ec5e"
	body := []byte(`{"blinding_key":"private","cases":[]}`)
	if err := writePrivateProjectionArtifact(dir, runID, body); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, runID+".projection.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(body) {
		t.Fatalf("private artifact body = %s", got)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if gotMode := info.Mode().Perm(); gotMode != 0o600 {
		t.Fatalf("private artifact mode = %o, want 600", gotMode)
	}
	dirInfo, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	if gotMode := dirInfo.Mode().Perm(); gotMode != 0o700 {
		t.Fatalf("private directory mode = %o, want 700", gotMode)
	}
	if err := writePrivateProjectionArtifact("", runID, body); err == nil {
		t.Fatal("empty private artifact directory did not fail closed")
	}
}

func TestV9PrivateProjectionArtifactRejectsTraversalAndUnsafeParents(t *testing.T) {
	const runID = "7e4283a1-92f8-4f41-914e-b508aca0ec5e"
	body := []byte(`{"private":true}`)
	if err := writePrivateProjectionArtifact(t.TempDir(), "../escape", body); err == nil {
		t.Fatal("path-traversal run id was accepted")
	}
	if err := writePrivateProjectionArtifact("relative/private", runID, body); err == nil {
		t.Fatal("relative private directory was accepted")
	}

	root := t.TempDir()
	realDir := filepath.Join(root, "real")
	if err := os.Mkdir(realDir, 0o700); err != nil {
		t.Fatal(err)
	}
	symlinkDir := filepath.Join(root, "linked")
	if err := os.Symlink(realDir, symlinkDir); err != nil {
		t.Fatal(err)
	}
	if err := writePrivateProjectionArtifact(symlinkDir, runID, body); err == nil {
		t.Fatal("symlink private directory was accepted")
	}

	openDir := filepath.Join(root, "open")
	if err := os.Mkdir(openDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writePrivateProjectionArtifact(openDir, runID, body); err == nil {
		t.Fatal("world-readable private directory was accepted")
	}
}

func TestV9PrivateProjectionArtifactNeverFollowsOrOverwritesTarget(t *testing.T) {
	const runID = "7e4283a1-92f8-4f41-914e-b508aca0ec5e"
	body := []byte(`{"private":true}`)

	t.Run("symlink target", func(t *testing.T) {
		dir := t.TempDir()
		outside := filepath.Join(t.TempDir(), "outside")
		if err := os.WriteFile(outside, []byte("keep"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(outside, filepath.Join(dir, runID+".projection.json")); err != nil {
			t.Fatal(err)
		}
		if err := writePrivateProjectionArtifact(dir, runID, body); err == nil {
			t.Fatal("symlink target was followed")
		}
		got, err := os.ReadFile(outside)
		if err != nil || string(got) != "keep" {
			t.Fatalf("outside target changed: %q err=%v", got, err)
		}
	})

	t.Run("preexisting file", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, runID+".projection.json")
		if err := os.WriteFile(path, []byte("first"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := writePrivateProjectionArtifact(dir, runID, body); err == nil {
			t.Fatal("preexisting artifact was overwritten")
		}
		got, err := os.ReadFile(path)
		if err != nil || string(got) != "first" {
			t.Fatalf("preexisting artifact changed: %q err=%v", got, err)
		}
	})
}

func TestV9PrivateProjectionArtifactConcurrentDuplicateCreatesExactlyOnce(t *testing.T) {
	const runID = "7e4283a1-92f8-4f41-914e-b508aca0ec5e"
	dir := t.TempDir()
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"private":true}`)
	results := make(chan error, 2)
	var ready sync.WaitGroup
	ready.Add(2)
	start := make(chan struct{})
	for range 2 {
		go func() {
			ready.Done()
			<-start
			results <- writePrivateProjectionArtifact(dir, runID, body)
		}()
	}
	ready.Wait()
	close(start)
	successes := 0
	var failures []error
	for range 2 {
		if err := <-results; err == nil {
			successes++
		} else {
			failures = append(failures, err)
		}
	}
	if successes != 1 {
		t.Fatalf("successful concurrent creates = %d, want 1; failures=%v", successes, failures)
	}
	got, err := os.ReadFile(filepath.Join(dir, runID+".projection.json"))
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("durable artifact = %q err=%v", got, err)
	}
}

func TestV9PrivateProjectionArtifactConcurrentDistinctRunsCreateDirectoryOnce(t *testing.T) {
	runIDs := []string{
		"7e4283a1-92f8-4f41-914e-b508aca0ec5e",
		"d7455551-d450-47a2-81e2-ce2a9a76f0d7",
	}
	dir := filepath.Join(t.TempDir(), "first-use")
	results := make(chan error, len(runIDs))
	var ready sync.WaitGroup
	ready.Add(len(runIDs))
	start := make(chan struct{})
	for _, runID := range runIDs {
		go func() {
			ready.Done()
			<-start
			results <- writePrivateProjectionArtifact(dir, runID, []byte(runID))
		}()
	}
	ready.Wait()
	close(start)
	for range runIDs {
		if err := <-results; err != nil {
			t.Fatalf("distinct first-use write failed: %v", err)
		}
	}
	for _, runID := range runIDs {
		got, err := os.ReadFile(filepath.Join(dir, runID+".projection.json"))
		if err != nil || string(got) != runID {
			t.Fatalf("run %s artifact = %q err=%v", runID, got, err)
		}
	}
}

type chunkWriter struct {
	bytes.Buffer
	max int
}

func (w *chunkWriter) Write(p []byte) (int, error) {
	if len(p) > w.max {
		p = p[:w.max]
	}
	return w.Buffer.Write(p)
}

type zeroWriter struct{}

func (zeroWriter) Write([]byte) (int, error) { return 0, nil }

type failingWriter struct{ calls int }

func (w *failingWriter) Write(p []byte) (int, error) {
	w.calls++
	if w.calls == 1 {
		return min(2, len(p)), nil
	}
	return 0, errors.New("injected write failure")
}

func TestWriteAllHandlesPartialWritesAndErrors(t *testing.T) {
	body := []byte("projection replay material")
	partial := &chunkWriter{max: 3}
	if err := writeAll(partial, body); err != nil {
		t.Fatalf("partial writes: %v", err)
	}
	if !bytes.Equal(partial.Bytes(), body) {
		t.Fatalf("partial output = %q", partial.Bytes())
	}
	if err := writeAll(zeroWriter{}, body); !errors.Is(err, io.ErrShortWrite) {
		t.Fatalf("zero write error = %v", err)
	}
	failing := &failingWriter{}
	if err := writeAll(failing, body); err == nil || !strings.Contains(err.Error(), "injected") {
		t.Fatalf("injected error = %v", err)
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

func TestTranscriptCanonicalBytesDoesNotReorderV9AttributionEvidence(t *testing.T) {
	transcripts := []transcriptCase{
		{
			CaseID: "z-case",
			Execution: runner.CaseExecution{
				ModelAttributionComplete: true,
				ModelInferenceObserved:   true,
			},
		},
		{
			CaseID: "a-case",
			Execution: runner.CaseExecution{
				ModelAttributionComplete: true,
			},
		},
	}
	artifact := transcriptArtifact{
		RunID: "run-v9-order", BenchVersion: protocol.BenchVersionV9,
		DatasetSHA256: strings.Repeat("a", 64), Cases: transcripts,
	}
	_, body, err := artifact.canonicalBytes()
	if err != nil {
		t.Fatal(err)
	}
	var canonical transcriptArtifact
	if err := json.Unmarshal(body, &canonical); err != nil {
		t.Fatal(err)
	}
	if got := []string{canonical.Cases[0].CaseID, canonical.Cases[1].CaseID}; !reflect.DeepEqual(got, []string{"a-case", "z-case"}) {
		t.Fatalf("canonical artifact was not sorted: %v", got)
	}
	if got := []string{transcripts[0].CaseID, transcripts[1].CaseID}; !reflect.DeepEqual(got, []string{"z-case", "a-case"}) {
		t.Fatalf("canonicalization reordered live attribution evidence: %v", got)
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
	if round.ModelRelay.Requests != 3 || round.ModelRelay.Successes != 2 || round.ModelRelay.CallerCancellations != 1 || round.ModelRelay.Retries != 1 || round.ModelRelay.RouteProbeAttempts != 2 || round.ModelRelay.RouteProbeRouted != 1 {
		t.Fatalf("model relay summary mismatch: %+v", round.ModelRelay)
	}
}
