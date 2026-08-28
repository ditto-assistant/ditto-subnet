package providercert

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRequestBodyPinsOneProvider(t *testing.T) {
	sc := scenarios()[2]
	body := requestBody(DefaultModel, "deepinfra", sc)
	provider := body["provider"].(map[string]any)
	if got := provider["allow_fallbacks"]; got != false {
		t.Fatalf("allow_fallbacks = %v, want false", got)
	}
	for _, field := range []string{"only", "order"} {
		values := provider[field].([]string)
		if len(values) != 1 || values[0] != "deepinfra" {
			t.Fatalf("provider.%s = %#v", field, values)
		}
	}
	reasoning := body["reasoning"].(map[string]any)
	if reasoning["enabled"] != false {
		t.Fatalf("reasoning.enabled = %v, want false", reasoning["enabled"])
	}
	kwargs := body["chat_template_kwargs"].(map[string]any)
	if kwargs["enable_thinking"] != false {
		t.Fatalf("enable_thinking = %v, want false", kwargs["enable_thinking"])
	}
}

func TestDecodeStreamReassemblesToolCall(t *testing.T) {
	input := strings.Join([]string{
		`data: {"id":"gen-1","model":"qwen/qwen3-32b","provider":"DeepInfra","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup_","arguments":"{\"record"}}]}}]}`,
		`data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"record","arguments":"_id\":\"record-17\"}"}}]},"finish_reason":"tool_calls","native_finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"cost":0.0001}}`,
		`data: [DONE]`,
	}, "\n\n")
	out, ttft, err := decodeStream(strings.NewReader(input), time.Now().Add(-time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	if ttft <= 0 || len(out.Choices) != 1 || len(out.Choices[0].Message.ToolCalls) != 1 {
		t.Fatalf("unexpected stream output: %#v, ttft=%v", out, ttft)
	}
	call := out.Choices[0].Message.ToolCalls[0]
	if call.ID != "call_1" || call.Function.Name != "lookup_record" || call.Function.Arguments != `{"record_id":"record-17"}` {
		t.Fatalf("reassembled call = %#v", call)
	}
	if out.Usage.TotalTokens != 15 {
		t.Fatalf("usage.total_tokens = %d", out.Usage.TotalTokens)
	}
}

func TestChatParksRateLimitWithoutRetry(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-key" {
			t.Fatalf("authorization header was not set")
		}
		assertAttributionHeaders(t, r)
		calls.Add(1)
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":{"code":429,"message":"manual retry required"}}`))
	}))
	defer server.Close()
	c := Client{APIURL: server.URL, APIKey: "test-key", HTTPClient: server.Client(), MaxAttempts: 1}
	out, status, attempts, _, _, err := c.chat(context.Background(), map[string]any{"messages": []any{}}, false)
	if err == nil || status != http.StatusTooManyRequests || attempts != 1 || calls.Load() != 1 {
		t.Fatalf("status=%d attempts=%d out=%#v", status, attempts, out)
	}
}

func TestFetchInventoryMapsProviderSlug(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assertAttributionHeaders(t, r)
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/providers":
			_, _ = w.Write([]byte(`{"data":[{"name":"DeepInfra","slug":"deepinfra"}]}`))
		case "/models/qwen/qwen3-32b/endpoints":
			_, _ = w.Write([]byte(`{"data":{"id":"qwen/qwen3-32b","name":"Qwen","endpoints":[{"provider_name":"DeepInfra","tag":"deepinfra/fp8"}]}}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	c := Client{APIURL: server.URL, HTTPClient: server.Client()}
	inv, err := c.FetchInventory(context.Background(), DefaultModel)
	if err != nil {
		t.Fatal(err)
	}
	if len(inv.Endpoints) != 1 || inv.Endpoints[0].ProviderSlug != "deepinfra" {
		t.Fatalf("inventory = %#v", inv)
	}
}

func assertAttributionHeaders(t *testing.T, r *http.Request) {
	t.Helper()
	if got := r.Header.Get("HTTP-Referer"); got != AppReferer {
		t.Fatalf("HTTP-Referer = %q, want %q", got, AppReferer)
	}
	if got := r.Header.Get("X-OpenRouter-Title"); got != AppTitle {
		t.Fatalf("X-OpenRouter-Title = %q, want %q", got, AppTitle)
	}
	if got := r.Header.Get("X-Title"); got != AppTitle {
		t.Fatalf("X-Title = %q, want %q", got, AppTitle)
	}
}

func TestValidateRejectsMalformedToolArguments(t *testing.T) {
	r := Result{
		Model: DefaultModel, Provider: "DeepInfra", ResponseProvider: "DeepInfra",
		FinishReason: "tool_calls", Usage: Usage{TotalTokens: 1},
		ToolCalls: []ToolCall{{ID: "call-1", Name: "lookup_record", Arguments: json.RawMessage(`not-json`)}},
	}
	errs := validate("auto_tool", DefaultModel, r)
	if got := fmt.Sprint(errs); !strings.Contains(got, "not a JSON object") {
		t.Fatalf("errors = %v", errs)
	}
}

func TestSummarize(t *testing.T) {
	results := []Result{
		{Provider: "DeepInfra", ProviderSlug: "deepinfra", Scenario: "text", Success: true, LatencyMS: 10, Usage: Usage{Cost: 0.1}},
		{Provider: "DeepInfra", ProviderSlug: "deepinfra", Scenario: "auto_tool", Success: false, LatencyMS: 30, Usage: Usage{Cost: 0.2}},
	}
	summary := Summarize(results)
	if len(summary) != 1 || summary[0].SuccessRate != 0.5 || summary[0].ToolSuccessRate != 0 || summary[0].P50LatencyMS != 30 || summary[0].P95LatencyMS != 30 || summary[0].TotalCostUSD < 0.299 || summary[0].TotalCostUSD > 0.301 {
		t.Fatalf("summary = %#v", summary)
	}
}
