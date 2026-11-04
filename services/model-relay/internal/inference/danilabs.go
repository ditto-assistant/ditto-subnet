package inference

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"net/http"
	"strconv"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

const (
	daniLabsRoute = "danilabs"
	daniLabsURL   = "https://api.danilabs.ai/v1/chat/completions"
	daniLabsModel = "openai/gpt-oss-20b"
	// Deliberately narrower than the advertised 32K window until its tokenizer,
	// template overhead and long-context behavior have been qualified.
	daniLabsMaxRequestBytes = 8192
	daniLabsMaxOutputTokens = 8192
)

// Size the finalized payload exactly as postProviderWithRetry serializes it.
// Keep the historical path allocation-free when the live share is zero.
func selectDaniLabsForPayload(cfg config.InferenceProxyConfig, model, purpose string, payload map[string]any, maxTokens int, now time.Time) bool {
	if !daniLabsEligible(cfg, model, purpose, maxTokens, now) {
		return false
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false
	}
	return selectDaniLabs(cfg, model, purpose, len(encoded), maxTokens, now)
}

// selectDaniLabs is a per-request sample of the eligible aggregate route. The
// secret, explicit trial scope, and deadline are independent of the live board.
// An invalid, expired, or more-than-seven-hours-away deadline fails closed.
func selectDaniLabs(cfg config.InferenceProxyConfig, model, purpose string, bodyBytes, maxTokens int, now time.Time) bool {
	return daniLabsEligible(cfg, model, purpose, maxTokens, now) && bodyBytes <= daniLabsMaxRequestBytes && rand.IntN(100) < cfg.DaniLabsPercent
}

func daniLabsEligible(cfg config.InferenceProxyConfig, model, purpose string, maxTokens int, now time.Time) bool {
	if cfg.DaniLabsPercent <= 0 || cfg.DaniLabsPercent > 100 || cfg.DaniLabsAPIKey == "" ||
		cfg.RoutingMode != config.RoutingModeAggregateThroughput || model != daniLabsModel ||
		maxTokens > daniLabsMaxOutputTokens || maxTokens < 1 {
		return false
	}
	if cfg.DaniLabsTrialScope != "fleet" && !(cfg.DaniLabsTrialScope == "canary" && purpose == "benchmark_canary") {
		return false
	}
	until, err := time.Parse(time.RFC3339, cfg.DaniLabsTrialUntil)
	if err != nil || !until.After(now.Add(time.Duration(cfg.TimeoutSeconds)*time.Second)) || until.After(now.Add(7*time.Hour)) {
		return false
	}
	return true
}

// completeChatViaDaniLabs dispatches once. Unknown outcomes are never replayed
// to OpenRouter: the first request may have reached the provider. The trial is
// free, so successful usage is metered in tokens with zero cost only while the
// explicit free-trial deadline permits selection.
func completeChatViaDaniLabs(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	payload map[string]any, model string, sleep sleepFunc) (*chatCompletionResult, *chatProviderExhausted) {
	return completeChatViaDaniLabsURL(ctx, client, cfg, payload, model, sleep, daniLabsURL)
}

func completeChatViaDaniLabsURL(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	payload map[string]any, model string, sleep sleepFunc, endpoint string) (*chatCompletionResult, *chatProviderExhausted) {
	sent, _ := json.Marshal(payload)
	trace := phaseTrace{route: daniLabsRoute, payload: sent}
	fail := func(code string, attempts int, timedOut bool) (*chatCompletionResult, *chatProviderExhausted) {
		trace.errorCode, trace.attempts, trace.timedOut = code, attempts, timedOut
		return nil, &chatProviderExhausted{upstreamAttempts: attempts, terminalErrorCode: code,
			timedOut: timedOut, upstreamProvider: daniLabsRoute, phases: []phaseTrace{trace}}
	}
	headers := map[string]string{"Authorization": "Bearer " + cfg.DaniLabsAPIKey, "Content-Type": "application/json"}
	// Never send this credential through a redirect, including to a subdomain.
	noRedirect := *client
	noRedirect.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	res, err := postProviderWithRetry(ctx, &noRedirect, endpoint, payload, headers, cfg.ResponseBodyBytes, cfg.TimeoutSeconds, false, "", sleep)
	if err != nil {
		if err.timedOut {
			return fail("provider_timeout", err.attempts, true)
		}
		return fail("provider_transport", err.attempts, false)
	}
	trace.status, trace.headers, trace.body = res.status, res.header, res.body
	if res.bodyOverLimit {
		return fail("response_too_large", res.attempts, false)
	}
	if res.status < 200 || res.status >= 300 {
		return fail("upstream_http_"+strconv.Itoa(res.status), res.attempts, res.status == 408 || res.status == 504)
	}
	decoded, _ := decodeJSONNumbers(res.body)
	response, ok := decoded.(map[string]any)
	if !ok {
		return fail("invalid_provider_response", res.attempts, false)
	}
	if _, _, isError := providerErrorEnvelope(response); isError {
		return fail("provider_unavailable", res.attempts, false)
	}
	if response["model"] != model {
		return fail("provider_identity_mismatch", res.attempts, false)
	}
	prompt, completion, ok := boundedUsage(response)
	if !ok {
		return fail("invalid_provider_response", res.attempts, false)
	}
	public, problem := publicProviderResponse(response, res.body)
	if problem != nil {
		return fail(phaseErrorCode(problem), res.attempts, false)
	}
	raw, marshalErr := compactJSON(public)
	if marshalErr != nil {
		return fail("invalid_provider_response", res.attempts, false)
	}
	trace.attempts = res.attempts
	return &chatCompletionResult{raw: raw, promptTokens: prompt, completionTokens: completion,
		upstreamProvider: daniLabsRoute, upstreamAttempts: res.attempts, phases: []phaseTrace{trace}}, nil
}
