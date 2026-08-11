package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func platformEmbeddingProfileForTest(benchVersion int) string {
	if benchVersion == protocol.BenchVersionV9 {
		return llm.V9AggregateProfileRevision
	}
	return "openrouter-route-0123456789abcdef-v1"
}

func TestPlatformEmbeddingVersionContract(t *testing.T) {
	for _, benchVersion := range []int{
		protocol.BenchVersionV7,
		protocol.BenchVersionV8,
		protocol.BenchVersionV9,
	} {
		t.Run(strconv.Itoa(benchVersion), func(t *testing.T) {
			var chatCalls atomic.Int64
			var embeddingCalls atomic.Int64
			vector := make([]float64, embeddingDimensions)
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case platformInferenceAPIPath:
					chatCalls.Add(1)
					writeJSON(w, http.StatusOK, map[string]any{
						"usage":   map[string]int{"prompt_tokens": 2, "completion_tokens": 1},
						"choices": []map[string]any{{"message": map[string]string{"content": "OK"}}},
					})
				case platformEmbeddingAPIPath:
					embeddingCalls.Add(1)
					writeJSON(w, http.StatusOK, map[string]any{
						"object": "list", "model": hostedEmbeddingModel,
						"data": []map[string]any{{
							"object": "embedding", "index": 0, "embedding": vector,
						}},
						"usage": map[string]int{"prompt_tokens": 3, "total_tokens": 3},
					})
				default:
					t.Errorf("unexpected Platform path %q", r.URL.Path)
					http.Error(w, "unexpected path", http.StatusNotFound)
				}
			}))
			defer upstream.Close()

			broker := newInferenceBroker(1)
			proxyURL := configureBrokerUpstream(broker, upstream)
			prepared := prepareBrokerSession(t, broker)
			activateBrokerSessionFor(
				t,
				broker,
				prepared,
				proxyURL,
				"openrouter",
				platformEmbeddingProfileForTest(benchVersion),
				llm.HarnessModelForVersion(benchVersion),
			)
			sourceIP := "192.0.2." + strconv.Itoa(benchVersion)
			runID := claimAndBindBrokerSession(t, broker, prepared["session_id"], sourceIP, benchVersion)

			if err := broker.trustedProbe(context.Background(), prepared["session_id"]); err != nil {
				t.Fatalf("v%d trusted probe: %v", benchVersion, err)
			}
			if chatCalls.Load() != 1 || embeddingCalls.Load() != 1 {
				t.Fatalf(
					"v%d trusted probe calls: chat=%d embedding=%d, want 1 each",
					benchVersion,
					chatCalls.Load(),
					embeddingCalls.Load(),
				)
			}

			if !broker.beginEmbeddingPhase(prepared["session_id"], runID) {
				t.Fatalf("v%d embedding phase was not admitted", benchVersion)
			}
			defer broker.endEmbeddingPhase(prepared["session_id"], runID)
			response := callEmbedding(broker, sourceIP, "versioned hosted text")
			if response.Code != http.StatusOK {
				t.Fatalf("v%d embedding status=%d body=%s", benchVersion, response.Code, response.Body.String())
			}
			if embeddingCalls.Load() != 2 {
				t.Fatalf("v%d embedding calls=%d, want preflight plus harness call", benchVersion, embeddingCalls.Load())
			}
		})
	}
}

func TestPlatformEmbeddingVersionBoundary(t *testing.T) {
	for _, test := range []struct {
		benchVersion int
		want         bool
	}{
		{protocol.BenchVersionV2, false},
		{protocol.BenchVersionV6, false},
		{protocol.BenchVersionV7, true},
		{protocol.BenchVersionV8, true},
		{protocol.BenchVersionV9, true},
	} {
		if got := usesPlatformEmbedding(test.benchVersion); got != test.want {
			t.Fatalf("v%d uses Platform embedding=%t, want %t", test.benchVersion, got, test.want)
		}
	}
}
