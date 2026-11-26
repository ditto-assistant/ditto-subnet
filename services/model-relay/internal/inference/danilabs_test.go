package inference

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

func TestDaniLabsSelectionFailsClosed(t *testing.T) {
	now := time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC)
	base := config.InferenceProxyConfig{RoutingMode: config.RoutingModeAggregateThroughput,
		DaniLabsAPIKey: "test", DaniLabsTrialUntil: now.Add(time.Hour).Format(time.RFC3339),
		DaniLabsTrialScope: "canary", DaniLabsPercent: 100, TimeoutSeconds: 90}
	if !selectDaniLabs(base, daniLabsModel, "benchmark_canary", 8192, 8192, now) {
		t.Fatal("eligible canary was not selected at 100 percent")
	}
	tests := []struct {
		name           string
		cfg            config.InferenceProxyConfig
		model, purpose string
		bytes, tokens  int
	}{
		{"other ticket", base, daniLabsModel, "scored", 10, 10},
		{"other model", base, "other", "benchmark_canary", 10, 10},
		{"large input", base, daniLabsModel, "benchmark_canary", 8193, 10},
		{"large output", base, daniLabsModel, "benchmark_canary", 10, 8193},
	}
	off := base
	off.DaniLabsPercent = 0
	noKey := base
	noKey.DaniLabsAPIKey = ""
	expired := base
	expired.DaniLabsTrialUntil = now.Add(60 * time.Second).Format(time.RFC3339)
	long := base
	long.DaniLabsTrialUntil = now.Add(8 * time.Hour).Format(time.RFC3339)
	badScope := base
	badScope.DaniLabsTrialScope = ""
	for name, cfg := range map[string]config.InferenceProxyConfig{"off": off, "no key": noKey, "expired before timeout": expired, "long trial": long, "missing scope": badScope} {
		tests = append(tests, struct {
			name           string
			cfg            config.InferenceProxyConfig
			model, purpose string
			bytes, tokens  int
		}{name, cfg, daniLabsModel, "benchmark_canary", 10, 10})
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if selectDaniLabs(tc.cfg, tc.model, tc.purpose, tc.bytes, tc.tokens, now) {
				t.Fatal("selected ineligible request")
			}
		})
	}
	fleet := base
	fleet.DaniLabsTrialScope = "fleet"
	if !selectDaniLabs(fleet, daniLabsModel, "scored", 10, 10, now) {
		t.Fatal("fleet setting did not select eligible request")
	}
}

func TestDaniLabsSelectionMeasuresFinalizedPayload(t *testing.T) {
	now := time.Now()
	cfg := config.InferenceProxyConfig{RoutingMode: config.RoutingModeAggregateThroughput,
		DaniLabsAPIKey: "test", DaniLabsTrialUntil: now.Add(time.Hour).Format(time.RFC3339),
		DaniLabsTrialScope: "canary", DaniLabsPercent: 100, TimeoutSeconds: 90}
	incoming := map[string]any{"model": daniLabsModel, "messages": []any{map[string]any{"role": "user", "content": ""}}}
	base, _ := json.Marshal(incoming)
	incoming["messages"].([]any)[0].(map[string]any)["content"] = strings.Repeat("x", daniLabsMaxRequestBytes-len(base)-1)
	inbound, _ := json.Marshal(incoming)
	locked, herr := lockedUpstreamPayload(incoming, daniLabsModel, 64, 9)
	if herr != nil {
		t.Fatal(herr)
	}
	final, _ := json.Marshal(locked)
	if len(inbound) > daniLabsMaxRequestBytes || len(final) <= daniLabsMaxRequestBytes {
		t.Fatalf("fixture sizes inbound=%d final=%d", len(inbound), len(final))
	}
	if !selectDaniLabs(cfg, daniLabsModel, "benchmark_canary", len(inbound), 64, now) {
		t.Fatal("negative control did not reproduce the old selection")
	}
	if selectDaniLabsForPayload(cfg, daniLabsModel, "benchmark_canary", locked, 64, now) {
		t.Fatal("selected a finalized payload above the trial cap")
	}
}

type countingJSON struct{ calls *int }

func (v countingJSON) MarshalJSON() ([]byte, error) {
	*v.calls++
	return []byte(`"value"`), nil
}

func TestDaniLabsSelectionSkipsMarshalingIneligibleRequests(t *testing.T) {
	now := time.Now()
	base := config.InferenceProxyConfig{RoutingMode: config.RoutingModeAggregateThroughput,
		DaniLabsAPIKey: "test", DaniLabsTrialUntil: now.Add(time.Hour).Format(time.RFC3339),
		DaniLabsTrialScope: "canary", DaniLabsPercent: 100, TimeoutSeconds: 90}
	expired := base
	expired.DaniLabsTrialUntil = now.Add(-time.Second).Format(time.RFC3339)
	noKey := base
	noKey.DaniLabsAPIKey = ""
	for _, tc := range []struct {
		name           string
		cfg            config.InferenceProxyConfig
		model, purpose string
		tokens         int
	}{
		{"other model", base, "other", "benchmark_canary", 64},
		{"other purpose", base, daniLabsModel, "scored", 64},
		{"expired", expired, daniLabsModel, "benchmark_canary", 64},
		{"no key", noKey, daniLabsModel, "benchmark_canary", 64},
		{"too many tokens", base, daniLabsModel, "benchmark_canary", 8193},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			if selectDaniLabsForPayload(tc.cfg, tc.model, tc.purpose, map[string]any{"probe": countingJSON{&calls}}, tc.tokens, now) {
				t.Fatal("selected an ineligible request")
			}
			if calls != 0 {
				t.Fatalf("marshaled an ineligible request %d times", calls)
			}
		})
	}
	calls := 0
	if !selectDaniLabsForPayload(base, daniLabsModel, "benchmark_canary", map[string]any{"probe": countingJSON{&calls}}, 64, now) || calls != 1 {
		t.Fatalf("eligible request: expected selection and one marshal, got %d marshals", calls)
	}
}

func TestDaniLabsCompletionSingleShotAndSanitized(t *testing.T) {
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get("Authorization") != "Bearer test-key" {
			t.Error("incorrect authorization")
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if _, ok := body["provider"]; ok {
			t.Error("OpenRouter preferences leaked")
		}
		_, _ = w.Write([]byte(`{"id":"1","object":"chat.completion","created":1,"model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"hello","reasoning":"private"}}],"usage":{"prompt_tokens":12,"completion_tokens":3}}`))
	}))
	defer srv.Close()
	cfg := config.InferenceProxyConfig{DaniLabsAPIKey: "test-key", ResponseBodyBytes: 1 << 20, TimeoutSeconds: 5}
	result, exhausted := completeChatViaDaniLabsURL(context.Background(), srv.Client(), cfg,
		map[string]any{"model": daniLabsModel}, daniLabsModel, defaultSleep, srv.URL)
	if exhausted != nil || result == nil {
		t.Fatalf("result=%+v exhausted=%+v", result, exhausted)
	}
	if requests != 1 || result.promptTokens != 12 || result.completionTokens != 3 || result.costMicrousd != 0 || result.upstreamProvider != daniLabsRoute {
		t.Fatalf("requests=%d result=%+v", requests, result)
	}
	var public map[string]any
	if err := json.Unmarshal(result.raw, &public); err != nil {
		t.Fatal(err)
	}
	message := public["choices"].([]any)[0].(map[string]any)["message"].(map[string]any)
	if _, ok := message["reasoning"]; ok {
		t.Fatal("reasoning leaked")
	}
}

func TestDaniLabsFailureIsNotReplayed(t *testing.T) {
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests++; w.WriteHeader(503) }))
	defer srv.Close()
	cfg := config.InferenceProxyConfig{DaniLabsAPIKey: "test", ResponseBodyBytes: 1024, TimeoutSeconds: 5}
	result, exhausted := completeChatViaDaniLabsURL(context.Background(), srv.Client(), cfg, map[string]any{"model": daniLabsModel}, daniLabsModel, defaultSleep, srv.URL)
	if result != nil || exhausted == nil || requests != 1 || exhausted.upstreamProvider != daniLabsRoute {
		t.Fatalf("requests=%d result=%+v exhausted=%+v", requests, result, exhausted)
	}
}

func TestDaniLabsBodyLevelErrorIsProviderUnavailable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"error":{"code":503,"message":"temporarily unavailable"}}`))
	}))
	defer srv.Close()
	cfg := config.InferenceProxyConfig{DaniLabsAPIKey: "test", ResponseBodyBytes: 1024, TimeoutSeconds: 5}
	_, exhausted := completeChatViaDaniLabsURL(context.Background(), srv.Client(), cfg,
		map[string]any{"model": daniLabsModel}, daniLabsModel, defaultSleep, srv.URL)
	if exhausted == nil || exhausted.terminalErrorCode != "provider_unavailable" {
		t.Fatalf("exhausted: %+v", exhausted)
	}
}

func TestDaniLabsMalformedResponseIsInvalid(t *testing.T) {
	for _, body := range []string{`not json`, `[]`} {
		t.Run(body, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(body)) }))
			defer srv.Close()
			cfg := config.InferenceProxyConfig{DaniLabsAPIKey: "test", ResponseBodyBytes: 1024, TimeoutSeconds: 5}
			_, exhausted := completeChatViaDaniLabsURL(context.Background(), srv.Client(), cfg,
				map[string]any{"model": daniLabsModel}, daniLabsModel, defaultSleep, srv.URL)
			if exhausted == nil || exhausted.terminalErrorCode != "invalid_provider_response" {
				t.Fatalf("exhausted: %+v", exhausted)
			}
		})
	}
}

func TestDaniLabsDoesNotFollowRedirects(t *testing.T) {
	forwarded := 0
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { forwarded++ }))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	cfg := config.InferenceProxyConfig{DaniLabsAPIKey: "test", ResponseBodyBytes: 1024, TimeoutSeconds: 5}
	_, exhausted := completeChatViaDaniLabsURL(context.Background(), server.Client(), cfg, map[string]any{"model": daniLabsModel}, daniLabsModel, defaultSleep, server.URL)
	if forwarded != 0 || exhausted == nil || exhausted.terminalErrorCode != "upstream_http_307" {
		t.Fatalf("forwarded=%d exhausted=%+v", forwarded, exhausted)
	}
}
