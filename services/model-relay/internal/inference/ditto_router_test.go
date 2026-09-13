package inference

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

func dittoRouterTestConfig(url string) (config.InferenceProxyConfig, config.DittoRouterConfig) {
	return config.InferenceProxyConfig{ResponseBodyBytes: 1 << 20, TimeoutSeconds: 5},
		config.DittoRouterConfig{Enabled: true, URL: url, APIKey: "dk_test", OnBehalfOfHeader: "X-Ditto-On-Behalf-Of",
			Lanes: []string{config.DittoRouterLaneCompetition}, CompetitionAck: config.CompetitionAckValue}
}

func TestDittoRouterHeadersCarryOnlyTheKeyAndTheLinkedUser(t *testing.T) {
	_, router := dittoRouterTestConfig("https://inference.heyditto.ai/v1/chat/completions")
	headers := dittoRouterHeaders(router, "ditto-user-1")
	if headers["Authorization"] != "Bearer dk_test" || headers["X-Ditto-On-Behalf-Of"] != "ditto-user-1" {
		t.Fatalf("headers: %+v", headers)
	}
	if len(headers) != 3 {
		t.Fatalf("no inbound header may leak upstream: %+v", headers)
	}
}

func TestCompleteChatViaDittoRouterShapesASuccess(t *testing.T) {
	var seen *http.Request
	var body map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"chatcmpl-1","model":"dittobench-competition","choices":[{"message":{"role":"assistant","content":"hi"}}],"usage":{"prompt_tokens":12,"completion_tokens":3,"cost":0.0004}}`))
	}))
	defer srv.Close()
	cfg, router := dittoRouterTestConfig(srv.URL)
	payload := map[string]any{"model": "dittobench-competition", "messages": []any{map[string]any{"role": "user", "content": "hello"}}}
	result, exhausted := completeChatViaDittoRouter(context.Background(), srv.Client(), cfg, router, payload,
		"dittobench-competition", "ditto-user-1", func(context.Context, time.Duration) {})
	if exhausted != nil {
		t.Fatalf("exhausted: %+v", exhausted)
	}
	if seen.Header.Get("Authorization") != "Bearer dk_test" || seen.Header.Get("X-Ditto-On-Behalf-Of") != "ditto-user-1" {
		t.Fatalf("upstream headers: %+v", seen.Header)
	}
	if _, leaked := body["provider"]; leaked {
		t.Fatalf("OpenRouter provider preferences must not be sent to the Router: %+v", body)
	}
	if result.promptTokens != 12 || result.completionTokens != 3 || result.upstreamProvider != dittoRouterRoute || result.upstreamAttempts != 1 {
		t.Fatalf("result: %+v", result)
	}
	if result.costMicrousd <= 0 {
		t.Fatalf("usage.cost should be kept when the Router echoes it: %+v", result)
	}
	if len(result.phases) != 1 || result.phases[0].route != dittoRouterRoute || result.phases[0].status != 200 {
		t.Fatalf("trace phase: %+v", result.phases)
	}
}

func TestCompleteChatViaDittoRouterReportsPaymentRequiredDistinctly(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusPaymentRequired)
		_, _ = w.Write([]byte(`{"error":{"type":"app_spend_not_authorized","message":"the user has not authorized this app to spend"}}`))
	}))
	defer srv.Close()
	cfg, router := dittoRouterTestConfig(srv.URL)
	result, exhausted := completeChatViaDittoRouter(context.Background(), srv.Client(), cfg, router,
		map[string]any{"model": "m"}, "m", "ditto-user-2", func(context.Context, time.Duration) {})
	if result != nil || exhausted == nil {
		t.Fatalf("expected an exhausted outcome, got %+v / %+v", result, exhausted)
	}
	if exhausted.terminalErrorCode != "ditto_router_payment_required" || exhausted.timedOut {
		t.Fatalf("exhausted: %+v", exhausted)
	}
	if calls != 1 {
		t.Fatalf("a 402 must never be retried against the user's wallet; calls=%d", calls)
	}
	if len(exhausted.phases) != 1 || exhausted.phases[0].status != 402 {
		t.Fatalf("trace phase: %+v", exhausted.phases)
	}
}

func TestCompleteChatViaDittoRouterRejectsAResponseWithoutUsage(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"m","choices":[]}`))
	}))
	defer srv.Close()
	cfg, router := dittoRouterTestConfig(srv.URL)
	_, exhausted := completeChatViaDittoRouter(context.Background(), srv.Client(), cfg, router,
		map[string]any{"model": "m"}, "m", "u", func(context.Context, time.Duration) {})
	if exhausted == nil || exhausted.terminalErrorCode != "invalid_provider_response" {
		t.Fatalf("exhausted: %+v", exhausted)
	}
}
