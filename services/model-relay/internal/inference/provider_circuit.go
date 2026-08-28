package inference

import (
	"context"
	"log/slog"
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
func receiptFreeOverload(phases []phaseTrace, model, provider string) (int, bool) {
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
		if !providerBackpressureIsReceiptFree(result, model, provider) {
			return 0, false
		}
		lastStatus = phase.status
	}
	return lastStatus, true
}

func receiptFreeResultOverload(result *providerHTTPResult, model, provider string) bool {
	return result != nil &&
		providerIsBackpressure(result.status, result.header) &&
		providerBackpressureIsReceiptFree(result, model, provider)
}
