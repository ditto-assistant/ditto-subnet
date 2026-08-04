package main

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The embedding door used to accept exactly one model string. Chat has
// substituted any string since #102; embeddings 400'd on anything that was not
// the literal `embeddinggemma`, including `embeddinggemma:latest` -- the same
// model with an ordinary Ollama tag. Embeddings are roughly two thirds of a v7
// run's ~1,067 inference requests, so that 400 landed almost immediately and
// took the whole run with it.
//
// These tests pin the fix from both directions: any string is accepted, and the
// string the broker SENDS upstream is still the pinned one either way.

// harnessEmbedModels is the interesting set. `embeddinggemma` is the literal
// every shipped harness sends (ditto-harness DEFAULT_EMBED_MODEL); the rest are
// what a harness that was written against a slightly different Ollama setup
// sends, and each of them was a lost run.
var harnessEmbedModels = []string{
	"embeddinggemma",
	"embeddinggemma:latest",
	"embeddinggemma:300m",
	"nomic-embed-text",
	"mxbai-embed-large",
	"a string nobody has ever shipped",
	"",
}

func hostedEmbeddingUpstream(t *testing.T, seen *atomic.Value, calls *atomic.Int64) *httptest.Server {
	t.Helper()
	vector := make([]float64, embeddingDimensions)
	return httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("hosted embedding upstream got undecodable body: %v", err)
			return
		}
		model, _ := payload["model"].(string)
		seen.Store(model)
		writeJSON(w, http.StatusOK, map[string]any{
			"object": "list", "model": hostedEmbeddingModel,
			"data":  []map[string]any{{"object": "embedding", "index": 0, "embedding": vector}},
			"usage": map[string]int{"prompt_tokens": 4, "total_tokens": 4},
		})
	}))
}

// TestV7EmbeddingBrokerSubstitutesAnyRequestedModel is the compatibility half.
func TestV7EmbeddingBrokerSubstitutesAnyRequestedModel(t *testing.T) {
	var seen atomic.Value
	var calls atomic.Int64
	upstream := hostedEmbeddingUpstream(t, &seen, &calls)
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.160", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	for _, model := range harnessEmbedModels {
		before := calls.Load()
		recorder := callEmbeddingAs(broker, "192.0.2.160", model, "hosted text")
		if recorder.Code != http.StatusOK {
			t.Fatalf("embedding for model %q status=%d body=%s (a model name must not cost a run)",
				model, recorder.Code, recorder.Body.String())
		}
		if calls.Load() != before+1 {
			t.Fatalf("embedding for model %q did not reach the upstream", model)
		}
		// The relaxation is on what the broker ACCEPTS, never on what it SENDS.
		if got := seen.Load(); got != hostedEmbeddingModel {
			t.Fatalf("harness model %q leaked to the platform as %v, want the pinned %q",
				model, got, hostedEmbeddingModel)
		}
		var response embeddingResponse
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if len(response.Embeddings) != 1 || len(response.Embeddings[0]) != embeddingDimensions {
			t.Fatalf("model %q got %d vectors, first of %d dims", model,
				len(response.Embeddings), len(response.Embeddings[0]))
		}
	}
}

// TestLocalEmbeddingBrokerSubstitutesAnyRequestedModel is the same property on
// the v2-v6 lane, which pins `embeddinggemma` at its own upstream. Both doors
// discard the caller's model, so both accept any string; only the constant they
// substitute differs.
func TestLocalEmbeddingBrokerSubstitutesAnyRequestedModel(t *testing.T) {
	var seen atomic.Value
	var calls atomic.Int64
	vector := make([]float64, embeddingDimensions)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("local embedding upstream got undecodable body: %v", err)
			return
		}
		model, _ := payload["model"].(string)
		seen.Store(model)
		writeJSON(w, http.StatusOK, map[string]any{
			"embeddings": [][]float64{vector}, "prompt_eval_count": 3,
		})
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.161")

	for _, model := range harnessEmbedModels {
		recorder := callEmbeddingAs(broker, "192.0.2.161", model, "local text")
		if recorder.Code != http.StatusOK {
			t.Fatalf("local embedding for model %q status=%d body=%s",
				model, recorder.Code, recorder.Body.String())
		}
		if got := seen.Load(); got != embeddingModel {
			t.Fatalf("harness model %q leaked to the Ollama upstream as %v, want the pinned %q",
				model, got, embeddingModel)
		}
	}
}

// TestEmbeddingBrokerLogsModelSubstitution pins the operator signal. A model
// name is no longer an error, but it is still evidence -- a harness naming a
// model nobody told it to name is either ignoring its injected configuration or
// probing for a model it was not granted -- and it is recorded with the same
// sentence the chat door has used since #102.
func TestEmbeddingBrokerLogsModelSubstitution(t *testing.T) {
	var seen atomic.Value
	var calls atomic.Int64
	upstream := hostedEmbeddingUpstream(t, &seen, &calls)
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.162", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	for _, test := range []struct {
		model  string
		logged bool
	}{
		// The literal every shipped harness sends is the door's published
		// contract, so it is silent. It would be useless as a signal anyway:
		// logging it would emit ~671 identical lines per well-behaved v7 run.
		{embeddingModel, false},
		{"embeddinggemma:latest", true},
		{"nomic-embed-text", true},
		{"mxbai-embed-large", true},
		{"a string nobody has ever shipped", true},
	} {
		var logs bytes.Buffer
		original := log.Writer()
		log.SetOutput(&logs)
		recorder := callEmbeddingAs(broker, "192.0.2.162", test.model, "hosted text")
		log.SetOutput(original)
		if recorder.Code != http.StatusOK {
			t.Fatalf("embedding for model %q status=%d", test.model, recorder.Code)
		}
		line := logs.String()
		if got := strings.Contains(line, "harness requested model"); got != test.logged {
			t.Fatalf("model %q: substitution logged = %v, want %v (log: %q)",
				test.model, got, test.logged, line)
		}
		if !test.logged {
			continue
		}
		for _, want := range []string{runID, test.model, hostedEmbeddingModel} {
			if !strings.Contains(line, want) {
				t.Fatalf("model %q: substitution log %q omits %q", test.model, line, want)
			}
		}
	}
}

// TestShippedHarnessEmbedModelIsUnaffected is the regression guard for the
// twelve legacy agents. Every one of them inherits ditto-harness
// DEFAULT_EMBED_MODEL verbatim, so the literal below is the only embedding
// model string in production today, and it must stay byte-for-byte accepted,
// silent, and served the pinned hosted model.
func TestShippedHarnessEmbedModelIsUnaffected(t *testing.T) {
	if embeddingModel != "embeddinggemma" {
		t.Fatalf("the door's published model is %q; every shipped harness sends %q",
			embeddingModel, "embeddinggemma")
	}
	var seen atomic.Value
	var calls atomic.Int64
	upstream := hostedEmbeddingUpstream(t, &seen, &calls)
	defer upstream.Close()

	broker := newInferenceBroker(1)
	proxyURL := configureBrokerUpstream(broker, upstream)
	prepared := prepareBrokerSession(t, broker)
	activateBrokerSessionFor(t, broker, prepared, proxyURL, "openrouter",
		"openrouter-route-0123456789abcdef-v1", llm.V7HarnessModel)
	runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], "192.0.2.163", protocol.BenchVersionV8)
	if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
		t.Fatal("failed to admit v7 embedding phase")
	}
	defer broker.endEmbeddingPhase(prepared["session_id"], runID)

	var logs bytes.Buffer
	original := log.Writer()
	log.SetOutput(&logs)
	request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(
		`{"model":"embeddinggemma","input":["shipped harness text"]}`,
	))
	request.RemoteAddr = "192.0.2.163:4321"
	recorder := httptest.NewRecorder()
	broker.handleEmbedding(recorder, request)
	log.SetOutput(original)

	if recorder.Code != http.StatusOK {
		t.Fatalf("shipped harness embedding status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if strings.Contains(logs.String(), "harness requested model") {
		t.Fatalf("the shipped harness default is now logged as a substitution: %q", logs.String())
	}
	if got := seen.Load(); got != hostedEmbeddingModel {
		t.Fatalf("shipped harness served %v, want the pinned %q", got, hostedEmbeddingModel)
	}
}

// TestEmbeddingBrokerStillRejectsUnknownFieldsForAnyModel keeps the two halves
// of the relaxation apart. Only the model field's allowlist is gone;
// DisallowUnknownFields still governs the rest of the payload, and it must not
// be reachable around it by pairing an unknown field with a novel model name.
func TestEmbeddingBrokerStillRejectsUnknownFieldsForAnyModel(t *testing.T) {
	var upstreamCalls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		upstreamCalls.Add(1)
	}))
	defer upstream.Close()
	broker := newInferenceBroker(1)
	broker.embeddingURL = upstream.URL + embeddingAPIPath
	broker.client.Transport = upstream.Client().Transport
	admittedEmbeddingBrokerSession(t, broker, "192.0.2.164")

	for _, model := range harnessEmbedModels {
		body := `{"model":` + jsonString(model) + `,"input":["x"],"keep_alive":"24h"}`
		request := httptest.NewRequest(http.MethodPost, embeddingAPIPath, bytes.NewBufferString(body))
		request.RemoteAddr = "192.0.2.164:4321"
		recorder := httptest.NewRecorder()
		broker.handleEmbedding(recorder, request)
		if recorder.Code != http.StatusBadRequest {
			t.Fatalf("unknown field with model %q status=%d, want 400", model, recorder.Code)
		}
	}
	if upstreamCalls.Load() != 0 {
		t.Fatalf("malformed embedding payloads reached upstream %d time(s)", upstreamCalls.Load())
	}
}

func jsonString(value string) string {
	encoded, _ := json.Marshal(value)
	return string(encoded)
}
