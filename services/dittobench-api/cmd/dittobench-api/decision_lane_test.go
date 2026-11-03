package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func callDecision(broker *inferenceBroker, source, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(http.MethodPost, "/v1/inference/decisions", strings.NewReader(body))
	request.RemoteAddr = source + ":4321"
	request.SetPathValue("rest", "decisions")
	recorder := httptest.NewRecorder()
	broker.handle(recorder, request)
	return recorder
}

func TestV15DecisionLaneForwardsSignedBodyAndRelaysAnswer(t *testing.T) {
	var calls atomic.Int64
	const decisionBody = `{"state":{"request":"post the update"},"questions":{"tool":{"type":"choice","options":["slack_post_message","save_memory"]}}}`
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodPost || r.URL.Path != platformDecisionAPIPath {
			t.Errorf("unexpected decision route %s %s", r.Method, r.URL.Path)
		}
		for _, header := range []string{"Authorization", "X-Ditto-Grant", "X-Ditto-Generation", "X-Ditto-Nonce", "X-Ditto-Requested-At", "X-Ditto-Proof"} {
			if r.Header.Get(header) == "" {
				t.Errorf("missing signed decision header %s", header)
			}
		}
		body, _ := io.ReadAll(r.Body)
		if string(body) != decisionBody {
			t.Errorf("decision body was rewritten: %s", body)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"model":   "typesafe/jev-1.14",
			"answers": map[string]any{"tool": map[string]any{"label": "save_memory", "p": 0.91}},
			"usage":   map[string]int{"input_tokens": 120, "output_tokens": 0},
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV15))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.71", protocol.BenchVersionV15)

	response := callDecision(broker, "192.0.2.71", decisionBody)
	if response.Code != http.StatusOK {
		t.Fatalf("decision status=%d body=%s", response.Code, response.Body.String())
	}
	var answer map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &answer); err != nil || answer["model"] != "typesafe/jev-1.14" {
		t.Fatalf("decision answer not relayed: %s", response.Body.String())
	}
	if calls.Load() != 1 {
		t.Fatalf("decision upstream calls=%d, want 1", calls.Load())
	}
}

func TestDecisionLaneIsAbsentBelowV15(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV14))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.72", protocol.BenchVersionV14)

	response := callDecision(broker, "192.0.2.72", `{"state":{},"questions":{"q":{"type":"choice"}}}`)
	if response.Code != http.StatusNotFound {
		t.Fatalf("v14 decision status=%d, want 404", response.Code)
	}
	if calls.Load() != 0 {
		t.Fatalf("v14 decision reached the platform %d times", calls.Load())
	}
}

func TestV15SandboxEnvCarriesTheDecisionsURL(t *testing.T) {
	v14 := harnessSandboxEnv(nil, protocol.BenchVersionV14, "session-a")
	if _, ok := v14[harnessDecisionsEnvKey]; ok {
		t.Fatal("v14 sandbox carries the decisions URL")
	}
	v15 := harnessSandboxEnv(nil, protocol.BenchVersionV15, "session-a")
	if got, want := v15[harnessDecisionsEnvKey], v15["DITTOBENCH_INFERENCE_BASE_URL"]+"/decisions"; got != want {
		t.Fatalf("v15 decisions URL = %q, want %q", got, want)
	}
	if !lockedEnvKeys[harnessDecisionsEnvKey] {
		t.Fatal("the decisions URL must be a locked key a miner cannot override")
	}
}

func TestPlatformDecisionURLRequiresTheChatRoute(t *testing.T) {
	got, err := platformDecisionURL("https://platform.example" + platformInferenceAPIPath)
	if err != nil || got != "https://platform.example"+platformDecisionAPIPath {
		t.Fatalf("decision URL = %q, %v", got, err)
	}
	if _, err := platformDecisionURL("https://platform.example/elsewhere"); err == nil {
		t.Fatal("a non-chat proxy route must be refused")
	}
}

func TestDecisionLaneCallIsCancelledWhenTheSessionIsDestroyed(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		select {
		case <-r.Context().Done():
		case <-release:
		}
	}))
	defer upstream.Close()
	defer close(release)

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter", llm.V9AggregateProfileRevision, llm.HarnessModelForVersion(protocol.BenchVersionV15))
	claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.73", protocol.BenchVersionV15)

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		done <- callDecision(broker, "192.0.2.73", `{"state":{},"questions":{"q":{"type":"choice"}}}`)
	}()
	<-entered
	broker.mu.RLock()
	session := broker.sessions[prepared["session_id"]]
	broker.mu.RUnlock()
	destroyBrokerSession(session)
	select {
	case response := <-done:
		if response.Code != http.StatusBadGateway {
			t.Fatalf("cancelled decision status=%d, want 502", response.Code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("destroying the session did not cancel the in-flight decision")
	}
}
