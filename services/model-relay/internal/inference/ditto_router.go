package inference

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/config"
)

// Ditto Router upstream — the relay dogfooding Ditto's own inference plane.
//
// Everything here is additive: a request is forwarded to the Router only when
// the lane is enabled AND the grant's agent belongs to a miner who linked a
// consenting Ditto account (apps/platform `miner_ditto_links`). The call then
// carries the on-behalf-of header so Ditto's receipts name that account; the
// relay never asserts a user id it did not read from the link row, and the
// Router itself re-checks the user's consent (402 when it is missing). Every
// other request takes the centralized OpenRouter path exactly as before.

const dittoRouterRoute = "ditto-router"

// dittoRouterOnBehalfOf resolves the consenting Ditto account for an agent.
// A missing link, a revoked link, or a lookup error all mean "not routed" —
// attribution can fail closed without failing the request.
func (d *Deps) dittoRouterOnBehalfOf(ctx context.Context, agentID pgtype.UUID) (string, bool) {
	if d.Queries == nil || !agentID.Valid {
		return "", false
	}
	link, err := d.Queries.GetActiveMinerDittoLinkForAgent(ctx, agentID)
	if err != nil {
		if !errors.Is(err, pgx.ErrNoRows) && d.Logger != nil {
			d.Logger.Warn("ditto router: link lookup", slog.String("error", err.Error()))
		}
		return "", false
	}
	if link.DittoUserID == "" {
		return "", false
	}
	return link.DittoUserID, true
}

// dittoRouterHeaders builds the outbound headers. No inbound client header is
// ever forwarded; the only identity is the link's verified user id.
func dittoRouterHeaders(cfg config.DittoRouterConfig, onBehalfOf string) map[string]string {
	return map[string]string{
		"Authorization":      "Bearer " + cfg.APIKey,
		"Content-Type":       "application/json",
		cfg.OnBehalfOfHeader: onBehalfOf,
	}
}

// completeChatViaDittoRouter performs one Router call and shapes the result
// like the OpenRouter path so settlement, tracing and the wire response stay
// identical. Provider preferences and the OpenRouter-only recovery ladder do
// not apply: the Router is a single upstream that picks the provider itself
// (the endpoint's locked model on the competition lane).
func completeChatViaDittoRouter(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	router config.DittoRouterConfig, payload map[string]any, model, onBehalfOf string,
	sleep sleepFunc) (*chatCompletionResult, *chatProviderExhausted) {

	sent, _ := json.Marshal(payload)
	trace := phaseTrace{phase: 0, route: dittoRouterRoute, payload: sent}
	exhausted := func(code string, attempts int, timedOut bool) *chatProviderExhausted {
		trace.errorCode, trace.attempts, trace.timedOut = code, attempts, timedOut
		return &chatProviderExhausted{
			upstreamAttempts:  attempts,
			terminalErrorCode: code,
			timedOut:          timedOut,
			upstreamProvider:  dittoRouterRoute,
			routeObservable:   true,
			phases:            []phaseTrace{trace},
		}
	}
	result, callErr := postProviderWithRetry(ctx, client, router.URL, payload, dittoRouterHeaders(router, onBehalfOf),
		cfg.ResponseBodyBytes, cfg.TimeoutSeconds, true, "", sleep)
	if callErr != nil {
		code := "provider_transport"
		if callErr.timedOut {
			code = "provider_timeout"
		}
		return nil, exhausted(code, callErr.attempts, callErr.timedOut)
	}
	trace.attempts, trace.status, trace.headers, trace.body = result.attempts, result.status, result.header, result.body
	if result.bodyOverLimit {
		return nil, exhausted("response_too_large", result.attempts, false)
	}
	if result.status >= 400 {
		// 402 is the Router refusing to bill the on-behalf-of user (no live
		// credits:spend grant or an exhausted daily budget). It is reported as
		// its own code so operators can tell "user did not consent" from an
		// outage, and it is never retried against the user's wallet.
		code := "upstream_http_" + strconv.Itoa(result.status)
		if result.status == http.StatusPaymentRequired {
			code = "ditto_router_payment_required"
		}
		return nil, exhausted(code, result.attempts, result.status == 408 || result.status == 504)
	}
	decoded, _ := decodeJSONNumbers(result.body)
	decodedMap, ok := decoded.(map[string]any)
	if !ok {
		return nil, exhausted("invalid_provider_response", result.attempts, false)
	}
	if _, _, providerError := providerErrorEnvelope(decodedMap); providerError {
		return nil, exhausted("provider_unavailable", result.attempts, false)
	}
	if m, isString := decodedMap["model"].(string); !isString || m == "" {
		return nil, exhausted("provider_identity_mismatch", result.attempts, false)
	}
	prompt, completion, usageOk := boundedUsage(decodedMap)
	if !usageOk {
		return nil, exhausted("invalid_provider_response", result.attempts, false)
	}
	// Ditto's receipt is the ledger of record for this call; the Router may
	// also echo usage.cost, which is kept when present and never invented.
	cost := boundedProviderCost(decodedMap)
	if cost < 0 {
		cost = 0
	}
	return &chatCompletionResult{
		raw:              result.body,
		promptTokens:     prompt,
		completionTokens: completion,
		costMicrousd:     cost,
		upstreamProvider: dittoRouterRoute,
		upstreamAttempts: result.attempts,
		fallbackPhase:    0,
		phases:           []phaseTrace{trace},
	}, nil
}
