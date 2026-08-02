package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func testVector(value float64, dimensions int) []float64 {
	vector := make([]float64, dimensions)
	for index := range vector {
		vector[index] = value
	}
	return vector
}

func callProxy(handler http.Handler, body string) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/embed", bytes.NewBufferString(body))
	handler.ServeHTTP(recorder, request)
	return recorder
}

func TestProxyLocksTranslationPreservesBatchOrderAndCaches(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if got := r.Header.Get("Authorization"); got != "Bearer test-key" {
			t.Fatalf("authorization = %q", got)
		}
		var request openRouterRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != openRouterModel || request.Dimensions != embeddingDimensions ||
			request.EncodingFormat != "float" || len(request.Input) != 2 ||
			request.Input[0] != "first private memory" || request.Input[1] != "second private memory" {
			t.Fatalf("unlocked request: %#v", request)
		}
		if len(request.Provider.Order) != 1 || request.Provider.Order[0] != openRouterProvider ||
			request.Provider.AllowFallbacks || request.Provider.DataCollection != "deny" {
			t.Fatalf("unlocked provider route: %#v", request.Provider)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"data": []map[string]any{
				{"index": 1, "embedding": testVector(2, embeddingDimensions)},
				{"index": 0, "embedding": testVector(1, embeddingDimensions)},
			},
			"usage": map[string]int{"prompt_tokens": 7, "total_tokens": 7},
		})
	}))
	defer upstream.Close()
	proxy := newProxy("test-key", upstream.URL, t.TempDir(), upstream.Client())
	body := `{"model":"embeddinggemma","input":["first private memory","second private memory"]}`
	for run := 0; run < 2; run++ {
		recorder := callProxy(proxy, body)
		if recorder.Code != http.StatusOK {
			t.Fatalf("run %d status=%d body=%s", run, recorder.Code, recorder.Body.String())
		}
		var response ollamaResponse
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		if response.Model != harnessModel || response.PromptEvalCount != 7 || len(response.Embeddings) != 2 ||
			response.Embeddings[0][0] != 1 || response.Embeddings[1][0] != 2 {
			t.Fatalf("wrong translated response: %#v", response)
		}
	}
	if calls.Load() != 1 || proxy.stats.CacheHits.Load() != 1 || proxy.stats.CacheMisses.Load() != 1 {
		t.Fatalf("cache stats: calls=%d hits=%d misses=%d", calls.Load(), proxy.stats.CacheHits.Load(), proxy.stats.CacheMisses.Load())
	}
}

func TestProxyCoalescesIdenticalInflightBatches(t *testing.T) {
	var calls atomic.Int64
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		<-release
		writeJSON(w, http.StatusOK, map[string]any{
			"data":  []map[string]any{{"index": 0, "embedding": testVector(1, embeddingDimensions)}},
			"usage": map[string]int{"prompt_tokens": 1, "total_tokens": 1},
		})
	}))
	defer upstream.Close()
	proxy := newProxy("test-key", upstream.URL, t.TempDir(), upstream.Client())
	body := `{"model":"embeddinggemma","input":"same input"}`
	var wait sync.WaitGroup
	statuses := make(chan int, 2)
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			statuses <- callProxy(proxy, body).Code
		}()
	}
	deadline := time.Now().Add(time.Second)
	for calls.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	close(release)
	wait.Wait()
	close(statuses)
	for status := range statuses {
		if status != http.StatusOK {
			t.Fatalf("status = %d", status)
		}
	}
	if calls.Load() != 1 || proxy.stats.CoalescedWaits.Load() != 1 {
		t.Fatalf("calls=%d coalesced=%d", calls.Load(), proxy.stats.CoalescedWaits.Load())
	}
}

func TestProxyRejectsInvalidDimensionsOrderingAndProviderErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   any
	}{
		{"dimensions", http.StatusOK, map[string]any{"data": []map[string]any{{"index": 0, "embedding": testVector(1, embeddingDimensions-1)}}}},
		{"ordering", http.StatusOK, map[string]any{"data": []map[string]any{{"index": 1, "embedding": testVector(1, embeddingDimensions)}}}},
		{"provider", http.StatusTooManyRequests, map[string]any{"error": map[string]string{"message": "private upstream detail"}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				writeJSON(w, test.status, test.body)
			}))
			defer upstream.Close()
			proxy := newProxy("test-key", upstream.URL, t.TempDir(), upstream.Client())
			recorder := callProxy(proxy, `{"model":"embeddinggemma","input":["private input"]}`)
			if recorder.Code != http.StatusServiceUnavailable || bytes.Contains(recorder.Body.Bytes(), []byte("private")) {
				t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
			}
			if proxy.stats.Failures.Load() != 1 {
				t.Fatalf("failures=%d", proxy.stats.Failures.Load())
			}
		})
	}
}

func TestCacheWriteFailureDoesNotDiscardValidEmbedding(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"data":  []map[string]any{{"index": 0, "embedding": testVector(1, embeddingDimensions)}},
			"usage": map[string]int{"prompt_tokens": 1, "total_tokens": 1},
		})
	}))
	defer upstream.Close()
	blocker := filepath.Join(t.TempDir(), "not-a-directory")
	if err := os.WriteFile(blocker, []byte("block"), 0o600); err != nil {
		t.Fatal(err)
	}
	proxy := newProxy("test-key", upstream.URL, filepath.Join(blocker, "cache"), upstream.Client())
	recorder := callProxy(proxy, `{"model":"embeddinggemma","input":["private input"]}`)
	if recorder.Code != http.StatusOK || proxy.stats.CacheWriteFailures.Load() != 1 {
		t.Fatalf("status=%d writes=%d body=%s", recorder.Code, proxy.stats.CacheWriteFailures.Load(), recorder.Body.String())
	}
}

func TestProxyRejectsUnreviewedHarnessRequestsWithoutUpstream(t *testing.T) {
	var calls atomic.Int64
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	defer upstream.Close()
	proxy := newProxy("test-key", upstream.URL, t.TempDir(), upstream.Client())
	tests := []string{
		`{"model":"other","input":["x"]}`,
		`{"model":"embeddinggemma","input":[]}`,
		`{"model":"embeddinggemma","input":["x"],"provider":{"allow_fallbacks":true}}`,
	}
	for _, body := range tests {
		if got := callProxy(proxy, body).Code; got != http.StatusBadRequest {
			t.Fatalf("status=%d body=%s", got, body)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("rejected requests reached upstream %d times", calls.Load())
	}
}

func TestHealthContainsAggregateProvenanceOnly(t *testing.T) {
	proxy := newProxy("secret-key", "https://example.invalid", "", nil)
	proxy.stats.PromptTokens.Store(25)
	recorder := httptest.NewRecorder()
	proxy.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))
	if recorder.Code != http.StatusOK || bytes.Contains(recorder.Body.Bytes(), []byte("secret-key")) {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var health map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &health); err != nil {
		t.Fatal(err)
	}
	if health["profile_revision"] != profileRevision || health["dimensions"] != float64(embeddingDimensions) ||
		health["estimated_cost_usd"] != float64(25)*promptUSDPerToken {
		t.Fatalf("health=%#v", health)
	}
}
