package inference

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

const (
	openRouterCircuitProvider = "openrouter"
	providerCircuitCooldown   = 2 * time.Minute
	providerCircuitWriteTTL   = 5 * time.Second
)

func (d *Deps) openProviderCircuit(parent context.Context, startedAt time.Time, status int, code string) bool {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), providerCircuitWriteTTL)
	defer cancel()
	now := d.now()
	_, err := d.Queries.OpenProviderOutageCircuit(ctx, postgres.OpenProviderOutageCircuitParams{
		Provider:      openRouterCircuitProvider,
		Epoch:         pgUUID(uuid.New()),
		Now:           pgTime(now),
		RetryAt:       pgTime(now.Add(providerCircuitCooldown)),
		LastStatus:    pgtype.Int4{Int32: int32(status), Valid: status != 0},
		LastErrorCode: code,
	})
	if err != nil {
		d.Logger.Error("provider circuit: open", slog.String("error", err.Error()))
		return false
	}
	d.Logger.Warn("provider circuit opened",
		slog.String("provider", openRouterCircuitProvider),
		slog.Int("status", status),
		slog.String("error_code", code),
		slog.Time("request_started_at", startedAt))
	return true
}

func (d *Deps) closeProviderCircuit(parent context.Context, startedAt time.Time) bool {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), providerCircuitWriteTTL)
	defer cancel()
	now := d.now()
	closed, err := d.Queries.CloseProviderOutageCircuit(ctx, postgres.CloseProviderOutageCircuitParams{
		Now:              pgTime(now),
		Provider:         openRouterCircuitProvider,
		RequestStartedAt: pgTime(startedAt),
	})
	if err != nil {
		d.Logger.Error("provider circuit: close", slog.String("error", err.Error()))
		return false
	}
	if closed > 0 {
		d.Logger.Info("provider circuit closed",
			slog.String("provider", openRouterCircuitProvider),
			slog.Time("request_started_at", startedAt))
	}
	return true
}

// receiptFreeOverload accepts only an exhausted sequence made entirely of
// canonical pre-provider 429/503 responses. A timeout, transport ambiguity,
// receipt-bearing response, or ordinary provider rejection cannot stop fleet
// work. This is deliberately the same accounting boundary that authorizes the
// relay's in-place retry.
func receiptFreeOverload(phases []phaseTrace, model string) (int, bool) {
	if len(phases) == 0 {
		return 0, false
	}
	lastStatus := 0
	for _, phase := range phases {
		if phase.status == 0 || !providerIsBackpressure(phase.status, phase.headers) {
			return 0, false
		}
		result := &providerHTTPResult{
			status: phase.status,
			header: phase.headers,
			body:   phase.body,
		}
		if !providerBackpressureIsReceiptFree(result, model) {
			return 0, false
		}
		lastStatus = phase.status
	}
	return lastStatus, true
}

// gatewayCreditExhausted accepts only an exhausted sequence in which every
// phase is the gateway's account-level credit refusal: HTTP 402 whose error
// envelope says code 402 and names the OpenRouter balance (structured
// limit_source, or the gateway's "Insufficient credits" message), with no
// completion or usage beside it. The gateway refuses before routing, for every
// grant on the relay's key, so a scored run that keeps going is measuring the
// Platform balance instead of the agent. On 2026-10-31 03:59-04:09Z this
// refusal failed 4,121 of Wonder v6's retest calls while the circuit stayed
// closed and both runs were accepted at 0.65/0.70 against a 0.92 median.
// Opening the circuit never re-dispatches a request, so no receipt-free proof
// is needed beyond the envelope shape.
func gatewayCreditExhausted(phases []phaseTrace) (int, bool) {
	if len(phases) == 0 {
		return 0, false
	}
	for _, phase := range phases {
		// A Ditto Router 402 is one consenting user's wallet, never the fleet.
		if phase.route == dittoRouterRoute ||
			phase.status != http.StatusPaymentRequired ||
			!gatewayCreditEnvelope(phase.body) {
			return 0, false
		}
	}
	return http.StatusPaymentRequired, true
}

func gatewayCreditEnvelope(body []byte) bool {
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(body)
	if !ok {
		return false
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		return false
	}
	if _, present := payload["choices"]; present {
		return false
	}
	if _, present := payload["usage"]; present {
		return false
	}
	errorPayload, ok := payload["error"].(map[string]any)
	if !ok {
		return false
	}
	code, ok := errorPayload["code"].(json.Number)
	if !ok || code.String() != "402" {
		return false
	}
	if meta, isMap := errorPayload["metadata"].(map[string]any); isMap {
		if source, _ := meta["limit_source"].(string); source == "openrouter_credits" {
			return true
		}
	}
	message, _ := errorPayload["message"].(string)
	return strings.Contains(strings.ToLower(message), "insufficient credits")
}

func receiptFreeResultOverload(result *providerHTTPResult, model string) bool {
	return result != nil &&
		providerIsBackpressure(result.status, result.header) &&
		providerBackpressureIsReceiptFree(result, model)
}
