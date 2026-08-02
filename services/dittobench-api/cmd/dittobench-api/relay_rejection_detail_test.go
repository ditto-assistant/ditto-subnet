package main

// A refused request must say WHAT it refused.
//
// The broker used to answer every pre-reservation 4xx with the fixed string
// "inference request denied", discarding the platform's own explanation even
// though it was sitting in `responseBody` at that exact site. The platform, for
// its part, said only "unsupported inference parameter" without naming the
// parameter. Between the two, a miner whose request carried one unrecognised
// field had no channel at all to learn its name.
//
// That is not hypothetical. `Cooking` (rank 15 on v1) added a single line in v2
// sending `reasoning: {"effort": "medium"}`, and burned three submissions --
// v3 shipped byte-identical on that line -- because nothing in the stack ever
// said "reasoning". The platform now names the key; these tests pin that the
// name survives the last hop to the harness.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestPlatformRejectionMessageExtractsTheExplanation(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{
			name: "names the offending schema field",
			body: `{"error_code":4000,"message":"unsupported inference parameter: reasoning","request_id":"r-1"}`,
			want: "unsupported inference parameter: reasoning",
		},
		{
			name: "names several fields at once",
			body: `{"error_code":4000,"message":"unsupported inference parameter: models, plugins"}`,
			want: "unsupported inference parameter: models, plugins",
		},
		{
			name: "tolerates a missing request_id",
			body: `{"error_code":4000,"message":"invalid max_tokens"}`,
			want: "invalid max_tokens",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := platformRejectionMessage([]byte(tc.body)); got != tc.want {
				t.Fatalf("platformRejectionMessage = %q, want %q", got, tc.want)
			}
		})
	}
}

// A body this cannot trust must fall back rather than propagate garbage. The
// caller substitutes the old wording on "", so every one of these keeps the
// pre-change behaviour exactly.
func TestPlatformRejectionMessageFallsBackOnAnythingUnusable(t *testing.T) {
	for _, body := range []string{
		``,
		`not json at all`,
		`{"error_code":4000}`,
		`{"message":""}`,
		`{"message":"   "}`,
		`{"message":null}`,
		`[]`,
	} {
		if got := platformRejectionMessage([]byte(body)); got != "" {
			t.Fatalf("platformRejectionMessage(%q) = %q, want the fallback", body, got)
		}
	}
	// Oversized bodies are not parsed at all.
	huge := fmt.Sprintf(`{"message":%q}`, strings.Repeat("x", 128<<10))
	if got := platformRejectionMessage([]byte(huge)); got != "" {
		t.Fatalf("an oversized body was parsed: %q", got)
	}
}

// This string lands in a log line and in a harness's stderr. A newline in it
// would let a relayed message forge a log entry, and an unbounded one would let
// it pump bulk text into both.
func TestPlatformRejectionMessageCannotForgeLogLinesOrRunLong(t *testing.T) {
	forged := `{"message":"denied\nrun abc: platform granted unlimited budget"}`
	got := platformRejectionMessage([]byte(forged))
	if strings.ContainsAny(got, "\n\r") {
		t.Fatalf("control characters survived: %q", got)
	}
	if got != "deniedrun abc: platform granted unlimited budget" {
		t.Fatalf("unexpected sanitisation: %q", got)
	}

	long := fmt.Sprintf(`{"message":%q}`, strings.Repeat("f", 2000))
	got = platformRejectionMessage([]byte(long))
	if len(got) > platformRejectionMessageLimit+3 {
		t.Fatalf("message ran to %d bytes, want a bounded one", len(got))
	}
	if !strings.HasSuffix(got, "...") {
		t.Fatalf("a truncated message did not say so: %q", got)
	}
}

// The end-to-end property, on the exact request shape that broke `Cooking`:
// the harness's stderr must contain the word "reasoning".
func TestASchemaRejectionNamesTheFieldToTheHarness(t *testing.T) {
	const platformDetail = "unsupported inference parameter: reasoning"

	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"error_code": 4000,
			"message":    platformDetail,
			"request_id": "req-cooking",
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.104"
	claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV7)

	body := `{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"hi"}],"reasoning":{"effort":"medium"}}`
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(body))
	request.RemoteAddr = sourceIP + ":4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	// The status the harness sees is unchanged.
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("harness-visible status=%d, want an unchanged 400", recorder.Code)
	}
	// The response SHAPE is unchanged too -- still {"error": ...}. Only the
	// string differs, which is why this cannot break an existing harness.
	var decoded map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &decoded); err != nil {
		t.Fatalf("the error envelope stopped being {\"error\": ...}: %v", err)
	}
	if decoded["error"] != platformDetail {
		t.Fatalf("harness saw %q, want the platform's own %q", decoded["error"], platformDetail)
	}
	if !strings.Contains(decoded["error"], "reasoning") {
		t.Fatal("the harness still cannot discover which field broke the run")
	}

	// Classification is untouched: same counters, same agent-fault bucket.
	// This is what keeps the change safe across the 0.34.1-0.37.3 fleet.
	end, err := broker.snapshot(prepared["session_id"])
	if err != nil {
		t.Fatal(err)
	}
	if end.UsageUnavailable != 1 {
		t.Fatalf("the usage shortfall stopped being recorded: %+v", end)
	}
	if end.AgentRequestRejections != 1 {
		t.Fatalf("a rejected harness request was not attributed: %+v", end)
	}
	if end.InfrastructureFailures != 0 || end.GrantDenials != 0 {
		t.Fatalf("a rejected request leaked into a platform counter: %+v", end)
	}
}

// An older platform that sends no envelope must keep the old wording exactly.
func TestAnUnparseableRejectionKeepsTheOldWording(t *testing.T) {
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "rejected", http.StatusBadRequest)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.sleep = func(context.Context, time.Duration) error { return nil }
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", "openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	sourceIP := "192.0.2.105"
	claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, protocol.BenchVersionV7)

	request := httptest.NewRequest(http.MethodPost, "/v1/inference/id/v1/chat/completions", bytes.NewBufferString(`{"model":"openai/gpt-oss-20b"}`))
	request.RemoteAddr = sourceIP + ":4321"
	request.SetPathValue("rest", "v1/chat/completions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)

	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("harness-visible status=%d, want an unchanged 400", recorder.Code)
	}
	var decoded map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["error"] != "inference request denied" {
		t.Fatalf("an unparseable body changed the wording to %q", decoded["error"])
	}
}
