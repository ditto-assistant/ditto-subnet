package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func sandboxContext() context.Context { return TrustSandbox(context.Background()) }

func TestSandboxContextAllowsLoopbackWithoutRelaxingExternalURLs(t *testing.T) {
	Configure(false)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	if err := Health(context.Background(), srv.URL); err == nil || !strings.Contains(err.Error(), "blocked connection") {
		t.Fatalf("external loopback URL must remain blocked, got %v", err)
	}
	if err := Health(sandboxContext(), srv.URL); err != nil {
		t.Fatalf("validator-owned sandbox URL should be reachable: %v", err)
	}
}

func TestHealthOK(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()

	if err := Health(sandboxContext(), srv.URL); err != nil {
		t.Fatalf("expected healthy, got %v", err)
	}
}

func TestHealthBad(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	if err := Health(sandboxContext(), srv.URL); err == nil {
		t.Fatalf("expected error for 503 health")
	}
}

func TestRunHarnessEchoesTool(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var req protocol.RunRequest
		json.NewDecoder(r.Body).Decode(&req)
		resp := protocol.RunResponse{
			FinalText: "ok",
			ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}},
			LatencyMs: 5,
		}
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	ds := protocol.Dataset{ToolCases: []protocol.ToolCase{
		{ID: "c1", Category: "web_search", Prompt: "search foo"},
	}}
	out, err := RunHarness(sandboxContext(), srv.URL, ds, nil)
	if err != nil {
		t.Fatalf("RunHarness error: %v", err)
	}
	got, ok := out["c1"]
	if !ok {
		t.Fatalf("missing case c1 in output")
	}
	if len(got.ToolCalls) != 1 || got.ToolCalls[0].Name != "search_web" {
		t.Fatalf("unexpected response: %+v", got)
	}
}

func TestRunHarnessPerCaseFailureIsEmpty(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	ds := protocol.Dataset{ToolCases: []protocol.ToolCase{{ID: "c1", Prompt: "x"}}}
	out, err := RunHarness(sandboxContext(), srv.URL, ds, nil)
	if err != nil {
		t.Fatalf("RunHarness should not abort on per-case failure: %v", err)
	}
	if got, ok := out["c1"]; !ok || len(got.ToolCalls) != 0 {
		t.Fatalf("failed case should be present and empty, got ok=%v %+v", ok, got)
	}
}

func TestSeed(t *testing.T) {
	var got protocol.SeedRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/seed" || r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		json.NewDecoder(r.Body).Decode(&got)
		json.NewEncoder(w).Encode(protocol.SeedResponse{
			Pairs:    len(got.Pairs),
			Subjects: len(got.Subjects),
			Links:    len(got.Links),
		})
	}))
	defer srv.Close()

	req := protocol.SeedRequest{
		UserID:   "miner",
		Pairs:    []protocol.MemoryPair{{PairID: "p1", Prompt: "hi", Response: "yo"}},
		Subjects: []protocol.Subject{{ID: "s1", SubjectText: "x"}},
		Links:    []protocol.SubjectLink{{SubjectID: "s1", PairID: "p1"}},
	}
	resp, err := Seed(sandboxContext(), srv.URL, req)
	if err != nil {
		t.Fatalf("Seed error: %v", err)
	}
	if resp.Pairs != 1 || resp.Subjects != 1 || resp.Links != 1 {
		t.Fatalf("unexpected seed response: %+v", resp)
	}
	if got.UserID != "miner" || len(got.Pairs) != 1 {
		t.Fatalf("harness received wrong body: %+v", got)
	}
}

func TestMarshalSeedRequestV9HasExactHostileWireShape(t *testing.T) {
	req := protocol.SeedRequest{UserID: "8ec86f06-e794-4d1c-a920-97d3fcf5ce8b", Wave: 41}
	body, err := marshalSeedRequest(req, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"user_id":"8ec86f06-e794-4d1c-a920-97d3fcf5ce8b","pairs":[],"subjects":[],"links":[]}`
	if string(body) != want {
		t.Fatalf("v9 seed bytes:\n got %s\nwant %s", body, want)
	}
	for _, forbidden := range []string{"wave", "seed", "run_size", "question_type", "dataset"} {
		if strings.Contains(string(body), forbidden) {
			t.Fatalf("v9 seed body exposed %q: %s", forbidden, body)
		}
	}
}

func TestMarshalSeedRequestV8RemainsCanonicalProtocolJSON(t *testing.T) {
	req := protocol.SeedRequest{
		UserID: "miner", Wave: 3,
		Pairs: []protocol.MemoryPair{{PairID: "project-04-origin", SessionID: "project-04", Prompt: "p", Response: "r"}},
	}
	want, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	got, err := marshalSeedRequest(req, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("v8 seed bytes changed:\n got %s\nwant %s", got, want)
	}
}

func TestV9HostileHarnessCapturesExactRequestBytesAndHeaders(t *testing.T) {
	type capture struct {
		method string
		target string
		header http.Header
		body   []byte
	}
	captures := make(chan capture, 2)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read %s: %v", r.URL.Path, err)
		}
		captures <- capture{method: r.Method, target: r.RequestURI, header: r.Header.Clone(), body: body}
		if r.URL.Path == "/seed" {
			_, _ = io.WriteString(w, `{"pairs":1,"subjects":0,"links":0}`)
			return
		}
		_, _ = io.WriteString(w, `{"final_text":"ok","tool_calls":[],"prompt_tokens":1,"output_tokens":1,"latency_ms":999}`)
	}))
	defer srv.Close()

	const userID = "8ec86f06-e794-4d1c-a920-97d3fcf5ce8b"
	const pairID = "f493ee76-36e6-49da-b842-03378db9d35c"
	const sessionID = "4d86aa61-8bde-444e-88a0-6e4346ee8fb2"
	const caseID = "f0e310c2-8c21-42e1-9e85-17d34ca9d51a"
	seed := protocol.SeedRequest{UserID: userID, Wave: 27, Pairs: []protocol.MemoryPair{{PairID: pairID, SessionID: sessionID, Timestamp: "2026-01-01T00:00:00Z", Prompt: "hello", Response: "world"}}}
	if _, err := SeedForVersion(sandboxContext(), srv.URL, seed, protocol.BenchVersionV9); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if _, err := RunCase(sandboxContext(), srv.URL, caseID, "ordinary production-semantic question", []protocol.ToolDefinition{{Name: "search_web", Description: "Search"}}, CaseOptions{BenchVersion: protocol.BenchVersionV9, UserID: userID, ToolEndpoint: "http://host.docker.internal:11436/v1/tools/opaque/tool"}); err != nil {
		t.Fatalf("run: %v", err)
	}

	seedCapture, runCapture := <-captures, <-captures
	if seedCapture.method != http.MethodPost || seedCapture.target != "/seed" {
		t.Fatalf("seed request line = %s %s", seedCapture.method, seedCapture.target)
	}
	if runCapture.method != http.MethodPost || runCapture.target != "/run" {
		t.Fatalf("run request line = %s %s", runCapture.method, runCapture.target)
	}
	for _, got := range []capture{seedCapture, runCapture} {
		if got.header.Get("Content-Type") != "application/json" {
			t.Errorf("%s content-type = %q", got.target, got.header.Get("Content-Type"))
		}
		var complete strings.Builder
		complete.WriteString(got.method)
		complete.WriteString(got.target)
		for key, values := range got.header {
			complete.WriteString(key)
			complete.WriteString(strings.Join(values, ","))
		}
		complete.Write(got.body)
		for _, forbidden := range []string{"project-04", "trip-01", "story-02", "memory-canary", "run_size", "question_type", "expected_answer", "dataset_sha256", "778899001122334455"} {
			if strings.Contains(complete.String(), forbidden) {
				t.Errorf("%s leaked %q in complete capture: %s", got.target, forbidden, complete.String())
			}
		}
	}
	if strings.Contains(string(seedCapture.body), `"wave"`) {
		t.Fatalf("v9 /seed exposed wave: %s", seedCapture.body)
	}
	for _, required := range []string{`"user_id":"` + userID + `"`, `"pair_id":"` + pairID + `"`, `"session_id":"` + sessionID + `"`, `"subjects":[]`, `"links":[]`} {
		if !strings.Contains(string(seedCapture.body), required) {
			t.Errorf("v9 /seed missing %s: %s", required, seedCapture.body)
		}
	}
	for _, required := range []string{`"case_id":"` + caseID + `"`, `"bench_version":9`, `"user_id":"` + userID + `"`, `"user_input":"ordinary production-semantic question"`} {
		if !strings.Contains(string(runCapture.body), required) {
			t.Errorf("v9 /run missing %s: %s", required, runCapture.body)
		}
	}
}

func TestHarnessWireDoesNotExposeDatasetSeed(t *testing.T) {
	// The validator needs the dataset seed to regenerate and score a run, but the
	// miner-controlled harness receives only the derived memories and questions.
	// Keep a recognizable sentinel out of the complete request target, headers,
	// and body for both harness endpoints so a later protocol change cannot turn
	// the reproducibility input into a harness-side oracle.
	const datasetSeed = "778899001122334455"
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read %s body: %v", r.URL.Path, err)
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		var wire strings.Builder
		wire.WriteString(r.RequestURI)
		for key, values := range r.Header {
			wire.WriteString(key)
			wire.WriteString(strings.Join(values, ","))
		}
		wire.Write(body)
		if strings.Contains(wire.String(), datasetSeed) {
			t.Errorf("%s request exposed the validator-only dataset seed", r.URL.Path)
		}
		requests++
		switch r.URL.Path {
		case "/seed":
			_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: 1})
		case "/run":
			_ = json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "ok"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	seedReq := protocol.SeedRequest{
		UserID: "miner",
		Pairs: []protocol.MemoryPair{{
			PairID: "derived-pair", Prompt: "derived memory", Response: "acknowledged",
		}},
	}
	if _, err := SeedForVersion(sandboxContext(), srv.URL, seedReq, 8); err != nil {
		t.Fatalf("seed harness: %v", err)
	}
	if _, err := RunCase(
		sandboxContext(), srv.URL, "derived-case", "derived question", nil,
		CaseOptions{BenchVersion: 8, UserID: "miner"},
	); err != nil {
		t.Fatalf("run harness case: %v", err)
	}
	if requests != 2 {
		t.Fatalf("observed %d harness requests, want seed and run", requests)
	}
}

func TestSeedError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	if _, err := Seed(sandboxContext(), srv.URL, protocol.SeedRequest{}); err == nil {
		t.Fatal("expected error for 500 /seed")
	}
}

func TestSeedStoreLockTimeoutIsTypedOnlyForExact503Contract(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		typed  bool
	}{
		{"exact", http.StatusServiceUnavailable, `{"error":"seed exceeded 600s, aborted to release the store lock"}`, true},
		{"wrong status", http.StatusBadGateway, `{"error":"seed exceeded 600s, aborted to release the store lock"}`, false},
		{"wrong duration", http.StatusServiceUnavailable, `{"error":"seed exceeded 599s, aborted to release the store lock"}`, false},
		{"prose only", http.StatusServiceUnavailable, `seed exceeded 600s, aborted to release the store lock`, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(test.status)
				_, _ = w.Write([]byte(test.body))
			}))
			defer srv.Close()

			_, err := SeedForVersion(sandboxContext(), srv.URL, protocol.SeedRequest{}, 8)
			if err == nil {
				t.Fatal("expected /seed failure")
			}
			if got := errors.Is(err, ErrSeedStoreLockTimeout); got != test.typed {
				t.Fatalf("errors.Is(store timeout) = %v, want %v: %v", got, test.typed, err)
			}
		})
	}
}

func TestRunCase(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req protocol.RunRequest
		json.NewDecoder(r.Body).Decode(&req)
		json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "answer:" + req.UserInput})
	}))
	defer srv.Close()
	resp, err := RunCase(sandboxContext(), srv.URL, "m1", "what color?", nil, CaseOptions{})
	if err != nil {
		t.Fatalf("RunCase error: %v", err)
	}
	if resp.FinalText != "answer:what color?" {
		t.Fatalf("unexpected: %q", resp.FinalText)
	}
}

func TestRunCaseSendsBenchVersionOnlyForV7Plus(t *testing.T) {
	versions := make(chan int, 2)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req protocol.RunRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode request: %v", err)
		}
		versions <- req.BenchVersion
		json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "ok"})
	}))
	defer srv.Close()

	for _, version := range []int{6, 7} {
		if _, err := RunCase(
			sandboxContext(), srv.URL, "m1", "question", nil,
			CaseOptions{BenchVersion: version},
		); err != nil {
			t.Fatalf("v%d RunCase: %v", version, err)
		}
	}
	if got := <-versions; got != 0 {
		t.Fatalf("v6 wire bench_version = %d, want omitted/zero", got)
	}
	if got := <-versions; got != 7 {
		t.Fatalf("v7 wire bench_version = %d, want 7", got)
	}
}

func TestRunCaseRetriesTransientThenSucceeds(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&calls, 1)
		if n < 3 { // first two attempts: transient failures (429 then 503)
			if n == 1 {
				w.WriteHeader(http.StatusTooManyRequests)
			} else {
				w.WriteHeader(http.StatusServiceUnavailable)
			}
			return
		}
		json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "recovered"})
	}))
	defer srv.Close()

	out, err := RunCase(sandboxContext(), srv.URL, "c1", "hi", nil, CaseOptions{})
	if err != nil {
		t.Fatalf("RunCase should have recovered after retries: %v", err)
	}
	if out.FinalText != "recovered" {
		t.Fatalf("unexpected response: %+v", out)
	}
	if got := atomic.LoadInt32(&calls); got != 3 {
		t.Fatalf("expected 3 attempts (2 transient + 1 success), got %d", got)
	}
}

func TestRunCaseTelemetryRecordsTrustedAttempts(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if atomic.AddInt32(&calls, 1) == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "recovered"})
	}))
	defer srv.Close()

	resp, execution, err := RunCaseWithTelemetry(sandboxContext(), srv.URL, "c1", "hi", nil, CaseOptions{})
	if err != nil {
		t.Fatalf("RunCaseWithTelemetry error: %v", err)
	}
	if resp.FinalText != "recovered" || execution.TerminalOutcome != "success" {
		t.Fatalf("unexpected result: response=%+v execution=%+v", resp, execution)
	}
	if len(execution.Attempts) != 2 {
		t.Fatalf("attempts = %d, want 2", len(execution.Attempts))
	}
	if execution.Attempts[0].Outcome != "rate_limited" || execution.Attempts[0].HTTPStatus != http.StatusTooManyRequests {
		t.Fatalf("first attempt = %+v", execution.Attempts[0])
	}
	if execution.Attempts[1].Outcome != "success" || execution.Attempts[1].HTTPStatus != http.StatusOK {
		t.Fatalf("second attempt = %+v", execution.Attempts[1])
	}
	if execution.TimedOut || execution.Cancelled || execution.TotalDurationMs < 0 {
		t.Fatalf("unexpected execution flags: %+v", execution)
	}
}

func TestRunCaseTelemetryClassifiesTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(100 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	ctx, cancel := context.WithTimeout(sandboxContext(), 10*time.Millisecond)
	defer cancel()
	_, execution, err := RunCaseWithTelemetry(ctx, srv.URL, "c1", "hi", nil, CaseOptions{})
	if err == nil {
		t.Fatal("expected deadline error")
	}
	if !execution.TimedOut || execution.Cancelled || execution.TerminalOutcome != "timeout" {
		t.Fatalf("timeout classification = %+v", execution)
	}
	if len(execution.Attempts) != 1 || execution.Attempts[0].Outcome != "timeout" {
		t.Fatalf("attempt telemetry = %+v", execution.Attempts)
	}
}

func TestRunCaseDoesNotRetryClientError(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusBadRequest) // 400: not transient
	}))
	defer srv.Close()

	if _, err := RunCase(sandboxContext(), srv.URL, "c1", "hi", nil, CaseOptions{}); err == nil {
		t.Fatal("expected an error for a 400 response")
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("a 4xx must not be retried; got %d attempts", got)
	}
}

func TestRunCaseGivesUpAfterAttempts(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		w.WriteHeader(http.StatusServiceUnavailable) // always transient
	}))
	defer srv.Close()

	if _, err := RunCase(sandboxContext(), srv.URL, "c1", "hi", nil, CaseOptions{}); err == nil {
		t.Fatal("expected an error after exhausting attempts")
	}
	if got := atomic.LoadInt32(&calls); got != int32(runAttempts) {
		t.Fatalf("expected %d attempts, got %d", runAttempts, got)
	}
}

// v7 timeout envelopes: client-side only (the wire bytes never change), gated
// on bench_version >= 7 so historical replay timing is untouched.
func TestPerCaseTimeoutForVersion(t *testing.T) {
	for _, v := range []int{0, 2, 6} {
		if got := perCaseTimeoutFor(v); got != perCaseTimeout {
			t.Fatalf("v%d per-case timeout must stay %v, got %v", v, perCaseTimeout, got)
		}
	}
	if got := perCaseTimeoutFor(7); got != v7PerCaseTimeout {
		t.Fatalf("v7 per-case timeout should be %v, got %v", v7PerCaseTimeout, got)
	}
	if v7PerCaseTimeout <= perCaseTimeout {
		t.Fatalf("v7 per-case timeout must exceed the historical %v, got %v", perCaseTimeout, v7PerCaseTimeout)
	}
}

func TestSeedTimeoutForVersion(t *testing.T) {
	for _, v := range []int{0, 2, 6} {
		if got := seedTimeoutFor(v); got != seedTimeout {
			t.Fatalf("v%d seed timeout must stay %v, got %v", v, seedTimeout, got)
		}
	}
	if got := seedTimeoutFor(7); got < seedTimeout || got != v7SeedTimeout {
		t.Fatalf("v7 seed timeout should be %v (>= historical), got %v", v7SeedTimeout, got)
	}
}

// SeedForVersion must marshal byte-identical /seed requests to Seed — the v7
// difference is only the client-side deadline.
func TestSeedForVersionWireIdentity(t *testing.T) {
	var bodies [][]byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		bodies = append(bodies, b)
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"loaded_pairs":1}`))
	}))
	defer srv.Close()

	req := protocol.SeedRequest{UserID: "u1"}
	if _, err := Seed(sandboxContext(), srv.URL, req); err != nil {
		t.Fatalf("Seed: %v", err)
	}
	if _, err := SeedForVersion(sandboxContext(), srv.URL, req, 7); err != nil {
		t.Fatalf("SeedForVersion: %v", err)
	}
	if len(bodies) != 2 || !bytes.Equal(bodies[0], bodies[1]) {
		t.Fatalf("v7 seed request bytes must be identical to the legacy path: %q vs %q", bodies[0], bodies[1])
	}
}
