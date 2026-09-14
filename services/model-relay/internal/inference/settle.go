package inference

import (
	"context"
	"errors"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// finishParams are the inputs of finish_inference_request.
type finishParams struct {
	grantID            uuid.UUID
	nonce              uuid.UUID
	generation         int64
	status             string // "completed" | "failed"
	promptTokens       int64
	completionTokens   int64
	costMicrousd       int64
	usageAvailable     bool
	now                time.Time
	upstreamProvider   pgtype.Text
	timedOut           bool
	latencyMs          int32
	upstreamAttempts   int32
	openrouterAttempts int32
	fallbackPhase      int32
	terminalErrorCode  pgtype.Text
}

// finishInferenceRequest reproduces ditto/db/queries/inference.py::
// finish_inference_request inside the caller's (settle) transaction. Lock
// order matches begin: unlocked grant snapshot -> ticket FOR UPDATE ->
// grant FOR UPDATE -> request FOR UPDATE. Returns deliverable.
func finishInferenceRequest(ctx context.Context, q *postgres.Queries, p finishParams) (bool, error) {
	snapshot, err := q.GetInferenceGrant(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	var ticket *postgres.ValidatorTicket
	ticketRow, err := q.GetValidatorTicketForShare(ctx, postgres.GetValidatorTicketForShareParams{
		AgentID:         snapshot.AgentID,
		BenchVersion:    snapshot.BenchVersion,
		ValidatorHotkey: snapshot.ValidatorHotkey,
	})
	if err == nil {
		ticket = &ticketRow
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return false, err
	}
	grant, err := q.GetInferenceGrantForUpdate(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	request, err := q.GetInferenceRequestForUpdate(ctx, postgres.GetInferenceRequestForUpdateParams{
		GrantID: pgUUID(p.grantID),
		Nonce:   pgUUID(p.nonce),
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	if (request.Status != "started" && request.Status != "canceled") ||
		int64(request.Generation) != p.generation {
		return false, nil
	}
	wasStarted := request.Status == "started"
	if !wasStarted && (request.PromptTokens > 0 || request.CompletionTokens > 0 || request.CostMicrousd > 0) {
		// Already charged by a reclamation path: double-settle guard.
		return false, nil
	}
	deliverable := p.status == "completed" &&
		p.usageAvailable &&
		grant.Status == "active" &&
		int64(grant.Generation) == p.generation &&
		wasStarted &&
		grant.ExpiresAt.Time.After(p.now) &&
		ticket != nil &&
		ticket.Status == postgres.TicketstatusIssued &&
		ticket.Deadline.Time.Equal(grant.TicketDeadline.Time) &&
		ticket.Deadline.Time.After(p.now)

	promptTokens := max64(0, p.promptTokens)
	completionTokens := max64(0, p.completionTokens)
	costMicrousd := max64(0, p.costMicrousd)
	if !p.usageAvailable {
		// No receipt, no spend. Charging the reservation estimate booked
		// megatokens against grants that never saw provider usage.
		promptTokens = 0
		completionTokens = 0
	} else if promptTokens > request.MaxChargeableTokens-completionTokens {
		// Overflow-safe form of promptTokens+completionTokens >
		// MaxChargeableTokens (Python compares with arbitrary-precision
		// ints; both operands here are already clamped non-negative).
		// Untrusted provider accounting is clamped against
		// max_chargeable_tokens, NOT reserved_tokens, and the call becomes
		// non-deliverable.
		promptTokens = request.MaxChargeableTokens
		completionTokens = 0
		deliverable = false
	}
	newStatus := "canceled"
	if wasStarted && (deliverable || p.status != "completed") {
		newStatus = p.status
	}
	if err := q.SettleInferenceRequest(ctx, postgres.SettleInferenceRequestParams{
		Status:             newStatus,
		PromptTokens:       promptTokens,
		CompletionTokens:   completionTokens,
		CostMicrousd:       costMicrousd,
		UpstreamProvider:   p.upstreamProvider,
		UpstreamAttempts:   max32(0, p.upstreamAttempts),
		OpenrouterAttempts: max32(0, p.openrouterAttempts),
		FallbackPhase:      clamp32(p.fallbackPhase, 0, 1),
		TerminalErrorCode:  p.terminalErrorCode,
		TimedOut:           p.timedOut,
		LatencyMs:          pgtype.Int4{Int32: p.latencyMs, Valid: true},
		CompletedAt:        pgTime(p.now),
		GrantID:            pgUUID(p.grantID),
		Nonce:              pgUUID(p.nonce),
	}); err != nil {
		return false, err
	}
	if request.RequestKind == kindChat {
		active := grant.ActiveRequests
		if wasStarted {
			active = max32(0, active-1)
		}
		updated, err := q.ApplyGrantChatSettlement(ctx, postgres.ApplyGrantChatSettlementParams{
			ActiveRequests:   active,
			PromptTokens:     promptTokens,
			CompletionTokens: completionTokens,
			CostMicrousd:     costMicrousd,
			Now:              pgTime(p.now),
			GrantID:          pgUUID(p.grantID),
		})
		if err != nil {
			return false, err
		}
		if updated.PromptTokens+updated.CompletionTokens >= updated.TokenBudget {
			if err := q.MarkInferenceGrantExhausted(ctx, postgres.MarkInferenceGrantExhaustedParams{
				Now:     pgTime(p.now),
				GrantID: pgUUID(p.grantID),
			}); err != nil {
				return false, err
			}
		}
	} else {
		active := grant.EmbeddingActiveRequests
		if wasStarted {
			active = max32(0, active-1)
		}
		if _, err := q.ApplyGrantEmbeddingSettlement(ctx, postgres.ApplyGrantEmbeddingSettlementParams{
			EmbeddingActiveRequests: active,
			EmbeddingTokens:         promptTokens,
			EmbeddingCostMicrousd:   costMicrousd,
			Now:                     pgTime(p.now),
			GrantID:                 pgUUID(p.grantID),
		}); err != nil {
			return false, err
		}
	}
	return deliverable, nil
}

// recordRouteObservation reproduces inference_routing.py::
// record_route_observation in the settle transaction (chat lane only).
// Route/policy rows sit outside the hot-table lock chain and are locked
// singly after it.
func recordRouteObservation(ctx context.Context, q *postgres.Queries, grant *postgres.InferenceGrant,
	success bool, latencyMs float64, completionTokens int64, costMicrousd int64, timedOut bool, now time.Time) error {
	if !grant.RouteProvider.Valid || grant.RouteProvider.String == "" {
		return nil
	}
	if !grant.RouteProfile.Valid || grant.RouteProfile.String == "" {
		return nil
	}
	models := allowedModels(grant)
	if len(models) == 0 {
		return nil
	}
	latency := latencyMs
	if latency < 0 {
		latency = 0
	}
	tokensPerSecondObserved := success && latencyMs > 0 && completionTokens > 0
	tokensPerSecond := 0.0
	if tokensPerSecondObserved {
		tokensPerSecond = float64(completionTokens) / (latencyMs / 1000)
	}
	errObserved := 1.0
	if success {
		errObserved = 0.0
	}
	timeoutObserved := 0.0
	if timedOut {
		timeoutObserved = 1.0
	}
	// One statement: the EWMA folds, status, and cooldown are computed in SQL
	// from the current row and the model's routing policy, so the route row is
	// locked only for the UPDATE itself instead of across a SELECT ... FOR
	// UPDATE, a policy read, and a write-back. A missing route or policy row
	// folds nothing (0 rows), which is the same no-op as before.
	_, err := q.ObserveInferenceProviderRoute(ctx, postgres.ObserveInferenceProviderRouteParams{
		LatencyMs:               latency,
		TokensPerSecondObserved: tokensPerSecondObserved,
		TokensPerSecond:         tokensPerSecond,
		ErrorObserved:           errObserved,
		TimeoutObserved:         timeoutObserved,
		CostObserved:            costMicrousd >= 0,
		CostMicrousd:            float64(max64(costMicrousd, 0)),
		Success:                 success,
		Now:                     pgTime(now),
		Model:                   models[0],
		Provider:                grant.RouteProvider.String,
		ProfileRevision:         grant.RouteProfile.String,
	})
	return err
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func max32(a, b int32) int32 {
	if a > b {
		return a
	}
	return b
}

func clamp32(v, lo, hi int32) int32 {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}
