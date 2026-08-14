package inference

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/metrics"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

// Request kinds (inference_requests.request_kind).
const (
	kindChat      = "chat"
	kindEmbedding = "embedding"
)

// laneLimits are the admission rails for one lane, selected from the
// (possibly settings-overlaid) proxy config.
type laneLimits struct {
	perTicketConcurrency    int
	perValidatorConcurrency int
	globalConcurrency       int
	perTicketRPM            int
	perValidatorRPM         int
	globalRPM               int
}

func limitsForKind(cfg config.InferenceProxyConfig, kind string) laneLimits {
	if kind == kindEmbedding {
		return laneLimits{
			perTicketConcurrency:    cfg.EmbeddingTicketConcurrency,
			perValidatorConcurrency: cfg.EmbeddingValidatorConcurrency,
			globalConcurrency:       cfg.EmbeddingGlobalConcurrency,
			perTicketRPM:            cfg.EmbeddingTicketRPM,
			perValidatorRPM:         cfg.EmbeddingValidatorRPM,
			globalRPM:               cfg.EmbeddingGlobalRPM,
		}
	}
	return laneLimits{
		perTicketConcurrency:    cfg.TicketConcurrency,
		perValidatorConcurrency: cfg.ValidatorConcurrency,
		globalConcurrency:       cfg.GlobalConcurrency,
		perTicketRPM:            cfg.TicketRPM,
		perValidatorRPM:         cfg.ValidatorRPM,
		globalRPM:               cfg.GlobalRPM,
	}
}

func pgUUID(id uuid.UUID) pgtype.UUID       { return pgtype.UUID{Bytes: id, Valid: true} }
func pgTime(t time.Time) pgtype.Timestamptz { return pgtype.Timestamptz{Time: t, Valid: true} }

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// allowedModels parses the grant's allowed_models JSONB array.
func allowedModels(grant *postgres.InferenceGrant) []string {
	var models []string
	if err := json.Unmarshal(grant.AllowedModels, &models); err != nil {
		return nil
	}
	return models
}

// beginParams are the inputs of begin_inference_request.
type beginParams struct {
	grantID             uuid.UUID
	nonce               uuid.UUID
	bearer              string
	model               string
	tokenReservation    int64
	maxChargeableTokens int64
	now                 time.Time
	kind                string
	timeoutSeconds      int
	limits              laneLimits
}

// beginResult is the successful admission: the LOCKED grant row (with the
// in-transaction mutations applied to the in-memory copy) and the inserted
// request row.
type beginResult struct {
	grant   postgres.InferenceGrant
	request postgres.InferenceRequest
}

// atCapacity records which admission gate declined (metric label), then
// answers the one wire value.
func atCapacity(kind, scope string) relayhttp.Decline {
	metrics.AdmissionAtCapacity.WithLabelValues(kind, scope).Inc()
	return relayhttp.DeclineAtCapacity
}

// beginInferenceRequest reproduces ditto/db/queries/inference.py::
// begin_inference_request statement-for-statement inside the caller's
// transaction. It returns (result, nil, nil) on success and
// (nil, &decline, nil) on a refusal. The caller (the endpoint) must ROLL
// BACK the transaction on a decline — the mutations the decline paths make
// (stale reclamation, revoked/exhausted status writes) are discarded on the
// relay endpoints, matching the deployed Python transaction boundary
// (DB contract section 5.3).
//
// Lock order: validator_tickets (rank 1) -> inference_grants (rank 2) ->
// inference_requests (rank 3). Never deviate.
func beginInferenceRequest(ctx context.Context, tx pgx.Tx, q *postgres.Queries, p beginParams) (*beginResult, *relayhttp.Decline, error) {
	decline := func(d relayhttp.Decline) (*beginResult, *relayhttp.Decline, error) {
		return nil, &d, nil
	}
	// Step 1: request-kind gate (no SQL).
	if p.kind != kindChat && p.kind != kindEmbedding {
		return decline(relayhttp.DeclineUnattributed)
	}
	// Step 2: unlocked snapshot, only to learn the owning ticket PK.
	snapshot, err := q.GetInferenceGrant(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return decline(relayhttp.DeclineUnattributed)
		}
		return nil, nil, err
	}
	// Step 3: ticket FOR UPDATE (lock rank 1). A missing ticket is legal
	// here; the liveness gate below fails closed.
	var ticket *postgres.ValidatorTicket
	ticketRow, err := q.GetValidatorTicketForUpdate(ctx, postgres.GetValidatorTicketForUpdateParams{
		AgentID:         snapshot.AgentID,
		BenchVersion:    snapshot.BenchVersion,
		ValidatorHotkey: snapshot.ValidatorHotkey,
	})
	if err == nil {
		ticket = &ticketRow
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, nil, err
	}
	// Step 4: grant FOR UPDATE (lock rank 2). Every gate below uses ONLY
	// this row's values (the populate_existing re-read equivalent).
	grant, err := q.GetInferenceGrantForUpdate(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return decline(relayhttp.DeclineUnattributed)
		}
		return nil, nil, err
	}
	// Step 5: THE authentication gate. Nothing above this line may name a
	// reason; everything below it may.
	if !grant.BearerDigest.Valid || !constantTimeEqual(grant.BearerDigest.String, bearerDigest(p.bearer)) {
		return decline(relayhttp.DeclineUnattributed)
	}
	// Step 6: lease expiry.
	if !grant.ExpiresAt.Time.After(p.now) {
		return decline(relayhttp.DeclineLeaseExpired)
	}
	// Step 7: model gate.
	if p.kind == kindChat {
		permitted := false
		for _, m := range allowedModels(&grant) {
			if m == p.model {
				permitted = true
				break
			}
		}
		if !permitted {
			return decline(relayhttp.DeclineModelNotPermitted)
		}
	} else if grant.BenchVersion < 7 || p.model != grant.EmbeddingModel {
		return decline(relayhttp.DeclineModelNotPermitted)
	}
	// Step 8: status gate (persistent terminals). "exhausted" re-derives
	// which allowance is spent rather than storing it.
	if grant.Status != "active" {
		switch grant.Status {
		case "exhausted":
			if p.kind == kindChat && grant.PromptTokens+grant.CompletionTokens+p.tokenReservation > grant.TokenBudget {
				return decline(relayhttp.DeclineTokenBudgetExhausted)
			}
			return decline(relayhttp.DeclineBudgetExhausted)
		case "revoked":
			return decline(relayhttp.DeclineGrantRevoked)
		default: // "pending" — minted but never exchanged.
			return decline(relayhttp.DeclineGrantNotExchanged)
		}
	}
	// Step 9: stale reclamation (this grant + lane, rank 3 locks). Canceled
	// requests are charged their full reservation as prompt tokens (the
	// estimate, not the byte count); the lane counter is then RECOUNTED.
	staleCutoff := p.now.Add(-2 * time.Duration(p.timeoutSeconds) * time.Second)
	stale, err := q.ListStaleStartedInferenceRequestsForUpdate(ctx, postgres.ListStaleStartedInferenceRequestsForUpdateParams{
		GrantID:     pgUUID(p.grantID),
		RequestKind: p.kind,
		Cutoff:      pgTime(staleCutoff),
	})
	if err != nil {
		return nil, nil, err
	}
	for _, req := range stale {
		if err := q.CancelInferenceRequestChargingReservation(ctx, postgres.CancelInferenceRequestChargingReservationParams{
			Now:     pgTime(p.now),
			GrantID: req.GrantID,
			Nonce:   req.Nonce,
		}); err != nil {
			return nil, nil, err
		}
		if p.kind == kindChat {
			if err := q.AddReclaimedChatTokens(ctx, postgres.AddReclaimedChatTokensParams{
				ReservedTokens: req.ReservedTokens,
				Now:            pgTime(p.now),
				GrantID:        req.GrantID,
			}); err != nil {
				return nil, nil, err
			}
			grant.PromptTokens += req.ReservedTokens
		} else {
			if err := q.AddReclaimedEmbeddingTokens(ctx, postgres.AddReclaimedEmbeddingTokensParams{
				ReservedTokens: req.ReservedTokens,
				Now:            pgTime(p.now),
				GrantID:        req.GrantID,
			}); err != nil {
				return nil, nil, err
			}
			grant.EmbeddingTokens += req.ReservedTokens
		}
	}
	if len(stale) > 0 {
		activeCount, err := q.CountStartedInferenceRequests(ctx, postgres.CountStartedInferenceRequestsParams{
			GrantID:     pgUUID(p.grantID),
			RequestKind: p.kind,
		})
		if err != nil {
			return nil, nil, err
		}
		if p.kind == kindChat {
			if err := q.SetGrantChatActiveRequests(ctx, postgres.SetGrantChatActiveRequestsParams{
				ActiveRequests: int32(activeCount),
				Now:            pgTime(p.now),
				GrantID:        pgUUID(p.grantID),
			}); err != nil {
				return nil, nil, err
			}
			grant.ActiveRequests = int32(activeCount)
		} else {
			if err := q.SetGrantEmbeddingActiveRequests(ctx, postgres.SetGrantEmbeddingActiveRequestsParams{
				EmbeddingActiveRequests: int32(activeCount),
				Now:                     pgTime(p.now),
				GrantID:                 pgUUID(p.grantID),
			}); err != nil {
				return nil, nil, err
			}
			grant.EmbeddingActiveRequests = int32(activeCount)
		}
	}
	// Step 10: ticket liveness. Failure revokes the grant (a write the
	// endpoint's decline rollback discards).
	if ticket == nil ||
		ticket.Status != postgres.TicketstatusIssued ||
		!ticket.Deadline.Time.Equal(grant.TicketDeadline.Time) ||
		!ticket.Deadline.Time.After(p.now) {
		if err := q.RevokeInferenceGrant(ctx, postgres.RevokeInferenceGrantParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return nil, nil, err
		}
		return decline(relayhttp.DeclineGrantRevoked)
	}
	// Step 11: request-count budgets. Chat marks the grant exhausted;
	// embedding deliberately does NOT (the chat lane must survive).
	if p.kind == kindChat && int64(grant.RequestCount) >= int64(grant.RequestBudget) {
		if err := q.MarkInferenceGrantExhausted(ctx, postgres.MarkInferenceGrantExhaustedParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return nil, nil, err
		}
		return decline(relayhttp.DeclineBudgetExhausted)
	}
	if p.kind == kindEmbedding && int64(grant.EmbeddingRequestCount) >= int64(grant.EmbeddingRequestBudget) {
		return decline(relayhttp.DeclineBudgetExhausted)
	}
	// Step 12: caller-shape reservation gate.
	if p.tokenReservation < 1 {
		return decline(relayhttp.DeclineUnattributed)
	}
	// Step 13: token headroom.
	activeReserved, err := q.SumStartedReservedTokens(ctx, postgres.SumStartedReservedTokensParams{
		GrantID:     pgUUID(p.grantID),
		RequestKind: p.kind,
	})
	if err != nil {
		return nil, nil, err
	}
	spent := grant.PromptTokens + grant.CompletionTokens
	tokenBudget := grant.TokenBudget
	if p.kind == kindEmbedding {
		spent = grant.EmbeddingTokens
		tokenBudget = grant.EmbeddingTokenBudget
	}
	if spent+activeReserved+p.tokenReservation > tokenBudget {
		if p.tokenReservation > tokenBudget {
			return decline(relayhttp.DeclineReservationTooLarge)
		}
		if spent+p.tokenReservation > tokenBudget {
			if p.kind == kindChat {
				if err := q.MarkInferenceGrantExhausted(ctx, postgres.MarkInferenceGrantExhaustedParams{
					Now:     pgTime(p.now),
					GrantID: pgUUID(p.grantID),
				}); err != nil {
					return nil, nil, err
				}
			}
			return decline(relayhttp.DeclineTokenBudgetExhausted)
		}
		// Only the in-flight reservations overflow: backpressure.
		return decline(atCapacity(p.kind, metrics.ScopeTokenReservation))
	}
	// Step 14: exact per-ticket concurrency (protected by the grant row
	// lock).
	activeRequests := grant.ActiveRequests
	if p.kind == kindEmbedding {
		activeRequests = grant.EmbeddingActiveRequests
	}
	if int(activeRequests) >= p.limits.perTicketConcurrency {
		return decline(atCapacity(p.kind, metrics.ScopePerTicket))
	}
	// Step 15: fast nonce-replay check.
	if _, err := q.GetInferenceRequest(ctx, postgres.GetInferenceRequestParams{
		GrantID: pgUUID(p.grantID),
		Nonce:   pgUUID(p.nonce),
	}); err == nil {
		return decline(relayhttp.DeclineNonceReplayed)
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, nil, err
	}
	// Step 16: cross-grant best-effort rails (deliberately unlocked; a burst
	// may overshoot by at most the number of racers — do NOT add locks).
	validatorActive, err := q.SumValidatorActiveLaneRequests(ctx, grant.ValidatorHotkey)
	if err != nil {
		return nil, nil, err
	}
	globalActive, err := q.SumGlobalActiveLaneRequests(ctx)
	if err != nil {
		return nil, nil, err
	}
	minuteStart := p.now.Add(-time.Minute)
	validatorRecent, err := q.CountRecentValidatorRequests(ctx, postgres.CountRecentValidatorRequestsParams{
		ValidatorHotkey: grant.ValidatorHotkey,
		RequestKind:     p.kind,
		Since:           pgTime(minuteStart),
	})
	if err != nil {
		return nil, nil, err
	}
	ticketRecent, err := q.CountRecentGrantRequests(ctx, postgres.CountRecentGrantRequestsParams{
		GrantID:     pgUUID(p.grantID),
		RequestKind: p.kind,
		Since:       pgTime(minuteStart),
	})
	if err != nil {
		return nil, nil, err
	}
	globalRecent, err := q.CountRecentGlobalRequests(ctx, postgres.CountRecentGlobalRequestsParams{
		RequestKind: p.kind,
		Since:       pgTime(minuteStart),
	})
	if err != nil {
		return nil, nil, err
	}
	validatorLaneActive := validatorActive.ChatActive
	globalLaneActive := globalActive.ChatActive
	if p.kind == kindEmbedding {
		validatorLaneActive = validatorActive.EmbeddingActive
		globalLaneActive = globalActive.EmbeddingActive
	}
	// Narrowest-to-widest; the FIRST at-limit gate names the metric scope.
	type gate struct {
		scope    string
		observed int64
		limit    int64
	}
	for _, g := range []gate{
		{metrics.ScopePerValidator, validatorLaneActive, int64(p.limits.perValidatorConcurrency)},
		{metrics.ScopeGlobal, globalLaneActive, int64(p.limits.globalConcurrency)},
		{metrics.ScopePerTicketRPM, ticketRecent, int64(p.limits.perTicketRPM)},
		{metrics.ScopePerValidatorRPM, validatorRecent, int64(p.limits.perValidatorRPM)},
		{metrics.ScopeGlobalRPM, globalRecent, int64(p.limits.globalRPM)},
	} {
		if g.observed >= g.limit {
			return decline(atCapacity(p.kind, g.scope))
		}
	}
	// Step 17: the admission INSERT inside a savepoint; a PK violation on
	// (grant_id, nonce) is the distributed replay guard.
	maxChargeable := p.tokenReservation
	if p.maxChargeableTokens > maxChargeable {
		maxChargeable = p.maxChargeableTokens
	}
	sp, err := tx.Begin(ctx)
	if err != nil {
		return nil, nil, err
	}
	request, err := q.WithTx(sp).InsertInferenceRequest(ctx, postgres.InsertInferenceRequestParams{
		GrantID:             pgUUID(p.grantID),
		Nonce:               pgUUID(p.nonce),
		Generation:          grant.Generation,
		RequestKind:         p.kind,
		Model:               p.model,
		ReservedTokens:      p.tokenReservation,
		MaxChargeableTokens: maxChargeable,
		StartedAt:           pgTime(p.now),
	})
	if err != nil {
		_ = sp.Rollback(ctx)
		if isUniqueViolation(err) {
			return decline(relayhttp.DeclineNonceReplayed)
		}
		return nil, nil, err
	}
	if err := sp.Commit(ctx); err != nil {
		return nil, nil, err
	}
	// Step 18: lane counters.
	if p.kind == kindChat {
		if err := q.IncrementGrantChatAdmission(ctx, postgres.IncrementGrantChatAdmissionParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return nil, nil, err
		}
		grant.RequestCount++
		grant.ActiveRequests++
	} else {
		if err := q.IncrementGrantEmbeddingAdmission(ctx, postgres.IncrementGrantEmbeddingAdmissionParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return nil, nil, err
		}
		grant.EmbeddingRequestCount++
		grant.EmbeddingActiveRequests++
	}
	grant.UpdatedAt = pgTime(p.now)
	return &beginResult{grant: grant, request: request}, nil, nil
}
