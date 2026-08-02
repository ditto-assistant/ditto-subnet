package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

func TestStreamingToolLoadRecordsTTFTAndConformance(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body["stream"] != true || body["tool_choice"] == nil {
			t.Fatalf("body = %#v", body)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		fmt.Fprint(w, "data: {\"provider\":\"Test\",\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"lookup_weather\",\"arguments\":\"{\\\"city\\\":\\\"Paris\\\",\\\"unit\\\":\\\"celsius\\\"}\"}}]}}]}\n\n")
		flusher.Flush()
		fmt.Fprint(w, "data: {\"choices\":[{\"finish_reason\":\"tool_calls\",\"delta\":{}}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15,\"cost\":0.001}}\n\n")
		fmt.Fprint(w, "data: [DONE]\n\n")
	}))
	defer server.Close()

	cfg := config{Endpoint: server.URL, Model: "test-model", APIKey: "test-key", Concurrency: []int{1}, Waves: 1, MaxTokens: 32, RequestTimeout: time.Second, Stream: true, Fixture: "tool"}
	got := run(context.Background(), cfg)
	level := got.Levels[0]
	if level.Successful != 1 || level.Conformant != 1 || level.P50TTFTMS < 0 || level.CompletionTokens != 5 {
		t.Fatalf("level = %#v", level)
	}
}

func TestCanceledRunProducesInterruptedPartialReport(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	cfg := config{Endpoint: server.URL, Model: "test-model", APIKey: "test-key", Concurrency: []int{1, 2}, Waves: 1, MaxTokens: 32, RequestTimeout: time.Second, Fixture: "text"}
	got := run(ctx, cfg)
	if !got.Interrupted || len(got.Levels) != 1 || got.Levels[0].OtherErrors != 1 {
		t.Fatalf("report = %#v", got)
	}
}

func TestFixedRequestsAndFallbackCost(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}`))
	}))
	defer server.Close()
	cfg := config{
		Endpoint: server.URL, Model: "test-model", APIKey: "test-key",
		Concurrency: []int{4}, Waves: 3, RequestsPerLevel: 2, MaxTokens: 32,
		RequestTimeout: time.Second, Fixture: "text",
		InputPricePerMillion: 1, OutputPricePerMillion: 2,
	}
	level := run(context.Background(), cfg).Levels[0]
	if level.Requests != 2 || level.Successful != 2 {
		t.Fatalf("requests = %#v", level)
	}
	if !level.CostEstimated || level.CostUSD != 0.0001 {
		t.Fatalf("cost = %#v", level)
	}
}

func TestFailedResponsePreservesReportedCost(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":{"code":429},"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10,"cost":0.001}}`))
	}))
	defer server.Close()
	cfg := config{Endpoint: server.URL, Model: "test-model", APIKey: "test-key", Concurrency: []int{1}, Waves: 1, MaxTokens: 32, RequestTimeout: time.Second, Fixture: "text"}
	level := run(context.Background(), cfg).Levels[0]
	if level.RateLimited != 1 || level.CostUSD != 0.001 || level.PromptTokens != 10 {
		t.Fatalf("level = %#v", level)
	}
}

func TestOpenRouterLoadPinsProviderAndAttributesApp(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("HTTP-Referer"); got != providercert.AppReferer {
			t.Fatalf("HTTP-Referer = %q", got)
		}
		for _, header := range []string{"X-OpenRouter-Title", "X-Title"} {
			if got := r.Header.Get(header); got != providercert.AppTitle {
				t.Fatalf("%s = %q", header, got)
			}
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		provider := body["provider"].(map[string]any)
		if provider["allow_fallbacks"] != false || provider["only"].([]any)[0] != "nebius" {
			t.Fatalf("provider routing = %#v", provider)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"provider":"Nebius","choices":[{"finish_reason":"stop","native_finish_reason":"stop","message":{"content":"load response"}}],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30,"cost":0.001}}`))
	}))
	defer server.Close()

	cfg := config{Endpoint: server.URL + "/chat/completions?openrouter.ai", Model: providercert.DefaultModel, Provider: "nebius", APIKey: "test-key", Concurrency: []int{1}, Waves: 1, MaxTokens: 32, RequestTimeout: time.Second}
	got := run(context.Background(), cfg)
	if len(got.Levels) != 1 || got.Levels[0].Successful != 1 || got.Levels[0].CompletionTokens != 20 {
		t.Fatalf("report = %#v", got)
	}
}

func TestParseLevels(t *testing.T) {
	got, err := parseLevels("1, 4,4,16")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || got[0] != 1 || got[1] != 4 || got[2] != 16 {
		t.Fatalf("levels = %v", got)
	}
	if _, err := parseLevels("65"); err == nil {
		t.Fatal("expected upper-bound error")
	}
}
