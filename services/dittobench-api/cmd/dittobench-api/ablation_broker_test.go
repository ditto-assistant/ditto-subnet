package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/google/uuid"
)

type ablationRoundTripper func(*http.Request) (*http.Response, error)

func (fn ablationRoundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	return fn(request)
}

func newV9AblationBroker(t *testing.T) (*inferenceBroker, string, string, *httptest.Server, *atomic.Int64) {
	t.Helper()
	broker := newInferenceBroker(1, 1)
	var upstreamCalls atomic.Int64
	broker.client = &http.Client{Transport: ablationRoundTripper(func(*http.Request) (*http.Response, error) {
		upstreamCalls.Add(1)
		return &http.Response{
			StatusCode: http.StatusInternalServerError,
			Body:       io.NopCloser(strings.NewReader(`{"error":"must not be reached"}`)),
			Header:     make(http.Header),
		}, nil
	})}
	id, runID := "v9-ablation-session", uuid.NewString()
	broker.sessions[id] = &brokerSession{
		id: id, bearer: "ticket-only-secret", proxyURL: "https://platform.invalid/api/v1/inference/chat/completions",
		expiresAt: time.Now().Add(time.Hour), expectedSourceIP: "127.0.0.1",
		provider: "platform", model: llm.HarnessModelForVersion(ablation.BenchVersionV9),
		requestModel: llm.HarnessModelForVersion(ablation.BenchVersionV9), profileRevision: "v9-test-profile",
		boundRunID: runID, benchVersion: ablation.BenchVersionV9,
		embeddingPhaseStarted: true, embeddingPhaseActive: true, embeddingConcurrency: 1,
		embeddingCalls: make(map[chan struct{}]context.CancelFunc), cancels: make(map[string]context.CancelFunc),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/inference/{rest...}", broker.handle)
	mux.HandleFunc("POST /api/embed", broker.handleEmbedding)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return broker, id, runID, server, &upstreamCalls
}

func seedV9AblationTrace(broker *inferenceBroker, id, caseID string) {
	session := broker.sessions[id]
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.ablationTraces == nil {
		session.ablationTraces = make(map[string]*brokerAblationTrace)
	}
	session.ablationTraces[caseID] = &brokerAblationTrace{}
}

func TestV9InferenceAblationShortCircuitsBeforeUpstream(t *testing.T) {
	broker, id, runID, server, upstreamCalls := newV9AblationBroker(t)
	responder, err := ablation.NewResponder(ablation.InterventionInference, ablation.Budget{
		MaxChatRequests: 2, MaxChatInputBytes: 4096,
	})
	if err != nil {
		t.Fatal(err)
	}
	seedV9AblationTrace(broker, id, "case-a")
	lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
		Lane: ablation.LaneInference, CaseID: "case-a", OpaqueUserNamespace: "opaque-a", Responder: responder,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()

	response, err := http.Post(server.URL+"/v1/inference/chat/completions", "application/json", strings.NewReader(
		`{"model":"caller-controlled","messages":[{"role":"user","content":"hello"}]}`,
	))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("synthetic chat status = %d", response.StatusCode)
	}
	var completion ablation.ChatCompletion
	if err := json.NewDecoder(response.Body).Decode(&completion); err != nil {
		t.Fatal(err)
	}
	if completion.Model != llm.HarnessModelForVersion(ablation.BenchVersionV9) || len(completion.Choices) != 1 {
		t.Fatalf("unexpected synthetic completion: %+v", completion)
	}
	usage := responder.Snapshot()
	if usage.ChatApplied != 1 || usage.UpstreamRequests != 0 || usage.UpstreamInputTokens != 0 ||
		usage.UpstreamOutputTokens != 0 || usage.UpstreamProviderCostMicroUSD != 0 {
		t.Fatalf("unexpected synthetic usage: %+v", usage)
	}
	if got := upstreamCalls.Load(); got != 0 {
		t.Fatalf("upstream calls = %d, want 0", got)
	}
	snapshot, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.UpstreamAttempts != 0 || snapshot.PromptTokens != 0 || snapshot.CompletionTokens != 0 {
		t.Fatalf("synthetic traffic contaminated provider accounting: %+v", snapshot)
	}
}

func TestV9EmbeddingAblationShortCircuitsBeforeUpstream(t *testing.T) {
	broker, id, runID, server, upstreamCalls := newV9AblationBroker(t)
	responder, err := ablation.NewResponder(ablation.InterventionEmbedding, ablation.Budget{
		MaxEmbeddingRequests: 2, MaxEmbeddingInputs: 4, MaxEmbeddingInputBytes: 4096,
	})
	if err != nil {
		t.Fatal(err)
	}
	seedV9AblationTrace(broker, id, "case-b")
	lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
		Lane: ablation.LaneEmbedding, CaseID: "case-b", OpaqueUserNamespace: "opaque-b", Responder: responder,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Close()

	response, err := http.Post(server.URL+"/api/embed", "application/json", strings.NewReader(
		`{"model":"embeddinggemma","input":["one","two"]}`,
	))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("synthetic embedding status = %d", response.StatusCode)
	}
	var embedding ablation.EmbeddingResponse
	if err := json.NewDecoder(response.Body).Decode(&embedding); err != nil {
		t.Fatal(err)
	}
	if len(embedding.Embeddings) != 2 || len(embedding.Embeddings[0]) != ablation.EmbeddingDimensions {
		t.Fatalf("unexpected synthetic embedding shape")
	}
	usage := responder.Snapshot()
	if usage.EmbeddingApplied != 1 || usage.EmbeddingInputs != 2 || usage.UpstreamRequests != 0 ||
		usage.UpstreamInputTokens != 0 || usage.UpstreamOutputTokens != 0 || usage.UpstreamProviderCostMicroUSD != 0 {
		t.Fatalf("unexpected synthetic usage: %+v", usage)
	}
	if got := upstreamCalls.Load(); got != 0 {
		t.Fatalf("upstream calls = %d, want 0", got)
	}
}

func TestV9InterventionsReplayNonTargetLaneWithoutUpstream(t *testing.T) {
	t.Run("inference intervention replays ordinary embeddings", func(t *testing.T) {
		broker, id, runID, server, upstreamCalls := newV9AblationBroker(t)
		const requestBody = `{"model":"embeddinggemma","input":["same selected content"]}`
		ordinaryResponse, err := json.Marshal(embeddingResponse{
			Embeddings: [][]float64{make([]float64, embeddingDimensions)}, PromptEvalCount: 3,
		})
		if err != nil {
			t.Fatal(err)
		}
		seedV9AblationTrace(broker, id, "case-replay-embedding")
		trace := broker.sessions[id].ablationTraces["case-replay-embedding"]
		trace.embeddings = []brokerAblationCall{{requestSHA256: ablationCallSHA256([]byte(requestBody)), response: ordinaryResponse}}
		responder, err := ablation.NewResponder(ablation.InterventionInference, ablation.Budget{
			MaxChatRequests: 1, MaxChatInputBytes: 1024,
		})
		if err != nil {
			t.Fatal(err)
		}
		lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
			Lane: ablation.LaneInference, CaseID: "case-replay-embedding",
			OpaqueUserNamespace: "opaque", Responder: responder,
		})
		if err != nil {
			t.Fatal(err)
		}
		defer lease.Close()
		response, err := http.Post(server.URL+"/api/embed", "application/json", strings.NewReader(requestBody))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		got, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != http.StatusOK || string(got) != string(ordinaryResponse) {
			t.Fatalf("replayed embedding = %d %s", response.StatusCode, got)
		}
		if upstreamCalls.Load() != 0 || responder.Snapshot().EmbeddingAttempts != 0 {
			t.Fatal("non-target embedding replay contacted or contaminated synthetic accounting")
		}
	})

	t.Run("embedding intervention replays ordinary inference", func(t *testing.T) {
		broker, id, runID, server, upstreamCalls := newV9AblationBroker(t)
		model := llm.HarnessModelForVersion(ablation.BenchVersionV9)
		requestBody := `{"model":"` + model + `","messages":[{"role":"user","content":"same selected content"}]}`
		ordinaryResponse := []byte(`{"choices":[{"message":{"role":"assistant","content":"ordinary"}}],"usage":{"prompt_tokens":3,"completion_tokens":1}}`)
		normalizedRequest, err := normalizeChatRequest([]byte(requestBody), model, ablation.BenchVersionV9)
		if err != nil {
			t.Fatal(err)
		}
		seedV9AblationTrace(broker, id, "case-replay-chat")
		trace := broker.sessions[id].ablationTraces["case-replay-chat"]
		trace.chat = []brokerAblationCall{{requestSHA256: ablationCallSHA256(normalizedRequest), response: ordinaryResponse}}
		responder, err := ablation.NewResponder(ablation.InterventionEmbedding, ablation.Budget{
			MaxEmbeddingRequests: 1, MaxEmbeddingInputs: 1, MaxEmbeddingInputBytes: 1024,
		})
		if err != nil {
			t.Fatal(err)
		}
		lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
			Lane: ablation.LaneEmbedding, CaseID: "case-replay-chat",
			OpaqueUserNamespace: "opaque", Responder: responder,
		})
		if err != nil {
			t.Fatal(err)
		}
		defer lease.Close()
		response, err := http.Post(server.URL+"/v1/inference/chat/completions", "application/json", strings.NewReader(requestBody))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		got, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != http.StatusOK || string(got) != string(ordinaryResponse) {
			t.Fatalf("replayed inference = %d %s", response.StatusCode, got)
		}
		if upstreamCalls.Load() != 0 || responder.Snapshot().ChatAttempts != 0 {
			t.Fatal("non-target inference replay contacted or contaminated synthetic accounting")
		}
	})
}

func TestV9AblationBrokerCancellationSourceAndReplayMismatchFailClosed(t *testing.T) {
	t.Run("cancelled and wrong-source inference never reaches responder", func(t *testing.T) {
		broker, id, runID, _, upstreamCalls := newV9AblationBroker(t)
		seedV9AblationTrace(broker, id, "case-isolated")
		responder, err := ablation.NewResponder(ablation.InterventionInference, ablation.Budget{
			MaxChatRequests: 2, MaxChatInputBytes: 4096,
		})
		if err != nil {
			t.Fatal(err)
		}
		lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
			Lane: ablation.LaneInference, CaseID: "case-isolated", OpaqueUserNamespace: "opaque", Responder: responder,
		})
		if err != nil {
			t.Fatal(err)
		}
		defer lease.Close()
		body := `{"model":"` + llm.HarnessModelForVersion(ablation.BenchVersionV9) + `","messages":[]}`

		wrongSource := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
		wrongSource.RemoteAddr = "127.0.0.2:1234"
		wrongRecorder := httptest.NewRecorder()
		broker.proxy(wrongRecorder, wrongSource, broker.sessions[id], 0, "")
		if wrongRecorder.Code != http.StatusUnauthorized {
			t.Fatalf("wrong-source status = %d", wrongRecorder.Code)
		}

		cancelledContext, cancel := context.WithCancel(context.Background())
		cancel()
		cancelled := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body)).WithContext(cancelledContext)
		cancelled.RemoteAddr = "127.0.0.1:1234"
		cancelledRecorder := httptest.NewRecorder()
		broker.proxy(cancelledRecorder, cancelled, broker.sessions[id], 0, "")
		if cancelledRecorder.Code != http.StatusRequestTimeout {
			t.Fatalf("cancelled status = %d", cancelledRecorder.Code)
		}
		if usage := responder.Snapshot(); usage.ChatAttempts != 0 || usage.ChatApplied != 0 {
			t.Fatalf("rejected requests reached responder: %+v", usage)
		}
		if upstreamCalls.Load() != 0 {
			t.Fatal("rejected requests reached upstream")
		}
	})

	t.Run("non-target replay mismatch cannot fall through", func(t *testing.T) {
		broker, id, runID, server, upstreamCalls := newV9AblationBroker(t)
		model := llm.HarnessModelForVersion(ablation.BenchVersionV9)
		expected := `{"model":"` + model + `","messages":[{"role":"user","content":"expected"}]}`
		seedV9AblationTrace(broker, id, "case-mismatch")
		broker.sessions[id].ablationTraces["case-mismatch"].chat = []brokerAblationCall{{
			requestSHA256: ablationCallSHA256([]byte(expected)), response: []byte(`{"choices":[{}],"usage":{}}`),
		}}
		responder, err := ablation.NewResponder(ablation.InterventionEmbedding, ablation.Budget{
			MaxEmbeddingRequests: 1, MaxEmbeddingInputs: 1, MaxEmbeddingInputBytes: 1024,
		})
		if err != nil {
			t.Fatal(err)
		}
		lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
			Lane: ablation.LaneEmbedding, CaseID: "case-mismatch", OpaqueUserNamespace: "opaque", Responder: responder,
		})
		if err != nil {
			t.Fatal(err)
		}
		defer lease.Close()
		actual := `{"model":"` + model + `","messages":[{"role":"user","content":"changed"}]}`
		response, err := http.Post(server.URL+"/v1/inference/chat/completions", "application/json", strings.NewReader(actual))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("replay mismatch status = %d", response.StatusCode)
		}
		if upstreamCalls.Load() != 0 {
			t.Fatal("replay mismatch fell through to upstream")
		}
	})
}

func TestTrustedConfirmationChatHandlerCannotFallThroughToNetwork(t *testing.T) {
	broker, id, _, server, networkCalls := newV9AblationBroker(t)
	var handlerCalls atomic.Int64
	session := broker.sessions[id]
	session.mu.Lock()
	session.bearer = ""
	session.proxyURL = ""
	session.trustedChatHandler = http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		handlerCalls.Add(1)
		if request.URL.Path != "/v1/chat/completions" {
			t.Errorf("trusted handler path = %q", request.URL.Path)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"choices":[{"message":{"content":"ordinary"}}],"usage":{"prompt_tokens":2,"completion_tokens":1}}`)
	})
	session.mu.Unlock()
	body := `{"model":"` + llm.HarnessModelForVersion(ablation.BenchVersionV9) + `","messages":[]}`
	response, err := http.Post(server.URL+"/v1/inference/chat/completions", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || handlerCalls.Load() != 1 || networkCalls.Load() != 0 {
		t.Fatalf("status=%d handler=%d network=%d", response.StatusCode, handlerCalls.Load(), networkCalls.Load())
	}
	snapshot, err := broker.snapshot(id)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.UpstreamAttempts != 1 || snapshot.Successes != 1 {
		t.Fatalf("trusted in-process accounting = %+v", snapshot)
	}
}

func TestV9AblationScopeFailsClosedOnWrongIdentityAndOverlap(t *testing.T) {
	broker, id, runID, _, _ := newV9AblationBroker(t)
	responder, err := ablation.NewResponder(ablation.InterventionInference, ablation.Budget{
		MaxChatRequests: 1, MaxChatInputBytes: 1024,
	})
	if err != nil {
		t.Fatal(err)
	}
	valid := ablation.RunRequest{
		Lane: ablation.LaneInference, CaseID: "case", OpaqueUserNamespace: "opaque", Responder: responder,
	}
	for name, candidate := range map[string]struct {
		runID   string
		request ablation.RunRequest
	}{
		"wrong run":          {uuid.NewString(), valid},
		"ordinary responder": {runID, ablation.RunRequest{Lane: ablation.LaneOrdinary, CaseID: "case", OpaqueUserNamespace: "opaque", Responder: responder}},
		"missing namespace":  {runID, ablation.RunRequest{Lane: ablation.LaneInference, CaseID: "case", Responder: responder}},
	} {
		t.Run(name, func(t *testing.T) {
			if lease, err := broker.beginAblationCase(id, candidate.runID, candidate.request); err == nil {
				lease.Close()
				t.Fatal("invalid scope was accepted")
			}
		})
	}
	ordinary, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
		Lane: ablation.LaneOrdinary, CaseID: "case", OpaqueUserNamespace: "ordinary-opaque",
	})
	if err != nil {
		t.Fatal(err)
	}
	ordinary.Close()
	lease, err := broker.beginAblationCase(id, runID, valid)
	if err != nil {
		t.Fatal(err)
	}
	if overlapping, err := broker.beginAblationCase(id, runID, valid); err == nil {
		overlapping.Close()
		t.Fatal("overlapping scope was accepted")
	}
	lease.Close()
	lease.Close()
	if replacement, err := broker.beginAblationCase(id, runID, valid); err != nil {
		t.Fatalf("scope was not released: %v", err)
	} else {
		replacement.Close()
	}
}

func TestV9AblationDrainRejectsNewAdmissionsAndWaitsForHandlers(t *testing.T) {
	for _, lane := range []string{"chat", "embedding"} {
		t.Run(lane, func(t *testing.T) {
			broker, id, runID, _, upstreamCalls := newV9AblationBroker(t)
			lease, err := broker.beginAblationCase(id, runID, ablation.RunRequest{
				Lane: ablation.LaneOrdinary, CaseID: "case-drain-" + lane, OpaqueUserNamespace: "opaque",
			})
			if err != nil {
				t.Fatal(err)
			}
			defer lease.Close()
			reader, writer := io.Pipe()
			done := make(chan struct{})
			go func() {
				defer close(done)
				request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, reader)
				request.RemoteAddr = "127.0.0.1:4321"
				response := httptest.NewRecorder()
				if lane == "chat" {
					request.URL.Path = "/v1/inference/chat/completions"
					request.SetPathValue("rest", "chat/completions")
					broker.handle(response, request)
				} else {
					broker.handleEmbedding(response, request)
				}
			}()

			deadline := time.Now().Add(time.Second)
			for {
				broker.sessions[id].mu.Lock()
				active := lease.scope.activeHandlers
				broker.sessions[id].mu.Unlock()
				if active == 1 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("ablation handler was not admitted")
				}
				time.Sleep(time.Millisecond)
			}
			if err := lease.beginDrain(); err != nil {
				t.Fatal(err)
			}

			request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, strings.NewReader(
				`{"model":"embeddinggemma","input":["new"]}`))
			request.RemoteAddr = "127.0.0.1:4321"
			response := httptest.NewRecorder()
			if lane == "chat" {
				request = httptest.NewRequest(http.MethodPost, "/v1/inference/chat/completions", strings.NewReader(
					`{"model":"ignored","messages":[]}`))
				request.RemoteAddr = "127.0.0.1:4321"
				request.SetPathValue("rest", "chat/completions")
				broker.handle(response, request)
			} else {
				broker.handleEmbedding(response, request)
			}
			if response.Code != http.StatusConflict {
				t.Fatalf("new admission during drain status=%d body=%s", response.Code, response.Body.String())
			}

			drainResult := make(chan error, 1)
			go func() { drainResult <- lease.waitDrained(context.Background()) }()
			select {
			case err := <-drainResult:
				t.Fatalf("drain returned before handler completed: %v", err)
			case <-time.After(20 * time.Millisecond):
			}
			_ = writer.Close()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("blocked ablation handler did not exit")
			}
			select {
			case err := <-drainResult:
				if err != nil {
					t.Fatal(err)
				}
			case <-time.After(time.Second):
				t.Fatal("ablation drain did not complete")
			}
			if upstreamCalls.Load() != 0 {
				t.Fatalf("draining ablation reached upstream %d times", upstreamCalls.Load())
			}
		})
	}
}
