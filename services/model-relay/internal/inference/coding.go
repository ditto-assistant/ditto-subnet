package inference

import (
	"context"
	"crypto/ed25519"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

func (d *Deps) handleCodingChatCompletions(w http.ResponseWriter, r *http.Request) {
	headers, ok := parseProxyHeaders(r)
	if !ok {
		relayhttp.WriteValidationError(w, r)
		return
	}
	if !d.Cfg.Inference.CodingEnabled || d.Cfg.Inference.OpenRouterAPIKey == "" ||
		d.Cfg.Inference.CodingAccountGuardrail != codingAccountGuardrail {
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "coding inference proxy is disabled", nil)
		return
	}
	if r.URL.RawQuery != "" || len(r.Header.Values("Content-Type")) != 1 ||
		!codingJSONContentType(r.Header.Get("Content-Type")) ||
		len(r.Header.Values("Content-Encoding")) != 0 {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "coding inference request is invalid", nil)
		return
	}
	if headers.anyMissing() {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "missing coding inference proof", nil)
		return
	}
	if !codingProofHeadersUnique(r.Header) {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "coding inference request is invalid", nil)
		return
	}
	provided, hasBearer := strings.CutPrefix(headers.authorization, "Bearer ")
	if !hasBearer || !codingBearerPattern.MatchString(provided) {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid coding inference proof", nil)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, codingMaxDispatchBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if len(body) > codingMaxDispatchBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "coding inference request is too large", nil)
		return
	}
	defer zeroCodingBytes(body)
	if absDuration(d.now().Sub(headers.requestedAt)) > proxyMaxAge {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "coding inference request is stale", nil)
		return
	}
	dispatch, lockedBody, err := parseCodingDispatch(body)
	if err != nil {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "coding inference request is invalid", nil)
		return
	}
	defer zeroCodingBytes(lockedBody)
	grantID, err := uuid.Parse(dispatch.GrantID)
	if err != nil || headers.grant != grantID || headers.generation != int64(dispatch.Generation) {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid coding inference proof", nil)
		return
	}

	ctx := context.WithoutCancel(r.Context())
	admitted, admissionErr := d.admitCodingDispatch(ctx, headers, provided, body, dispatch)
	if admissionErr != nil {
		writeHTTPError(w, r, admissionErr)
		return
	}
	deadline := admitted.expiresAt
	callContext, cancel := context.WithDeadline(ctx, deadline)
	outcome, providerErr := d.completeCodingProvider(callContext, dispatch, lockedBody)
	cancel()
	if providerErr != nil {
		d.Logger.Warn("coding inference provider attempt unsettled", slog.String("class", providerErr.Error()))
		if err := d.markCodingUnsettled(ctx, admitted, "provider_settlement_unavailable"); err != nil {
			d.Logger.Error("coding inference: persist unsettled provider attempt", slog.String("error", err.Error()))
		}
		relayhttp.WriteHTTPError(w, r, http.StatusBadGateway, "coding inference provider settlement unavailable", nil)
		return
	}
	defer zeroCodingBytes(outcome.normalized)
	defer zeroCodingBytes(outcome.failureProjection)
	if err := validateCodingSettlement(outcome.settlement, dispatch); err != nil {
		d.Logger.Warn("coding inference provider settlement rejected", slog.String("class", err.Error()))
		if persistErr := d.markCodingUnsettled(ctx, admitted, "invalid_provider_settlement"); persistErr != nil {
			d.Logger.Error("coding inference: persist invalid settlement", slog.String("error", persistErr.Error()))
		}
		relayhttp.WriteHTTPError(w, r, http.StatusBadGateway, "coding inference provider settlement unavailable", nil)
		return
	}
	responseBody, err := codingDispatchResultBody(dispatch, outcome)
	if err != nil {
		if persistErr := d.markCodingUnsettled(ctx, admitted, "invalid_provider_settlement"); persistErr != nil {
			d.Logger.Error("coding inference: persist response-build failure", slog.String("error", persistErr.Error()))
		}
		relayhttp.WriteInternalError(w, r)
		return
	}
	defer zeroCodingBytes(responseBody)
	if err := d.settleCodingDispatch(ctx, admitted, dispatch, outcome); err != nil {
		if persistErr := d.markCodingUnsettled(ctx, admitted, "invalid_provider_settlement"); persistErr != nil {
			d.Logger.Error("coding inference: settle and terminal fallback failed", slog.String("error", persistErr.Error()))
		}
		relayhttp.WriteInternalError(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(responseBody)
}

type codingLedgerKind uint8

const (
	codingLedgerTicket codingLedgerKind = iota
	codingLedgerCertification
)

type codingAdmittedDispatch struct {
	kind          codingLedgerKind
	expiresAt     time.Time
	ticketGrant   postgres.CodingInferenceGrant
	ticketRequest postgres.CodingInferenceRequest
	certGrant     postgres.CodingCertificationInferenceGrant
	certRequest   postgres.CodingCertificationInferenceRequest
}

func (d *Deps) admitCodingDispatch(
	ctx context.Context,
	headers *proxyHeaders,
	bearer string,
	body []byte,
	dispatch codingDispatchRequest,
) (codingAdmittedDispatch, *httpError) {
	var zero codingAdmittedDispatch
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := d.Queries.WithTx(tx)
	grant, err := q.GetCodingInferenceGrantForUpdate(ctx, pgUUID(headers.grant))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return d.admitCertificationCodingDispatch(ctx, tx, q, headers, bearer, body, dispatch)
		}
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	if !codingGrantAuthenticates(grant, headers, bearer, body) {
		return zero, httpErrorf(401, "invalid coding inference proof")
	}
	now, err := codingDatabaseNow(ctx, q)
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	deadline, deadlineErr := time.Parse(time.RFC3339Nano, dispatch.Deadline)
	if deadlineErr != nil || !grant.ExpiresAt.Valid || !deadline.Equal(grant.ExpiresAt.Time.UTC()) ||
		!grant.ExpiresAt.Time.After(now) || !codingGrantMatchesDispatch(grant, dispatch) {
		if grant.Status == "active" && grant.ExpiresAt.Valid && !grant.ExpiresAt.Time.After(now) {
			if revokeErr := q.RevokeCodingInferenceGrantUnsettled(ctx, postgres.RevokeCodingInferenceGrantUnsettledParams{
				Now: pgTime(now), GrantID: grant.GrantID,
			}); revokeErr != nil || tx.Commit(ctx) != nil {
				return zero, httpErrorf(500, "coding inference admission failed")
			}
		}
		return zero, httpErrorf(409, "coding inference grant is not live")
	}
	if grant.Status != "active" || grant.ActiveRequests != 0 ||
		grant.PromptTokens >= grant.PromptTokenBudget ||
		grant.CompletionTokens >= grant.CompletionTokenBudget ||
		grant.CostUsdMicros >= grant.CostBudgetUsdMicros {
		if grant.Status == "active" && grant.ActiveRequests == 0 {
			if settleErr := q.ApplyCodingInferenceGrantSettlement(ctx, postgres.ApplyCodingInferenceGrantSettlementParams{
				Status: "exhausted", Now: pgTime(now), GrantID: grant.GrantID,
			}); settleErr != nil || tx.Commit(ctx) != nil {
				return zero, httpErrorf(500, "coding inference admission failed")
			}
		}
		return zero, httpErrorf(409, "coding inference grant is unavailable")
	}
	latest, latestErr := q.GetLatestCodingInferenceRequestForUpdate(ctx, grant.GrantID)
	requestIncrement := int32(0)
	if errors.Is(latestErr, pgx.ErrNoRows) {
		if grant.RequestCount != 0 || dispatch.Sequence != 1 || dispatch.RequestSequence != 1 || dispatch.Attempt != 1 {
			return zero, httpErrorf(409, "coding inference request order is invalid")
		}
		requestIncrement = 1
	} else if latestErr != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	} else {
		if grant.RequestCount != latest.RequestSequence || latest.WeightEligible {
			return zero, httpErrorf(409, "coding inference request history is inconsistent")
		}
		switch latest.Status {
		case "receipt_free_retry":
			if dispatch.Sequence != latest.Sequence+1 || dispatch.RequestSequence != latest.RequestSequence ||
				dispatch.Attempt != latest.Attempt+1 || dispatch.RequestID != uuid.UUID(latest.RequestID.Bytes).String() ||
				dispatch.LockedRequestSHA256 != latest.LockedRequestSha256 {
				return zero, httpErrorf(409, "coding inference retry identity is invalid")
			}
		case "complete":
			if dispatch.Sequence != latest.Sequence+1 || dispatch.RequestSequence != latest.RequestSequence+1 || dispatch.Attempt != 1 {
				return zero, httpErrorf(409, "coding inference request order is invalid")
			}
			requestIncrement = 1
		default:
			return zero, httpErrorf(409, "coding inference request history is terminal")
		}
	}
	if requestIncrement == 1 && grant.RequestCount >= grant.RequestBudget {
		if err := q.ApplyCodingInferenceGrantSettlement(ctx, postgres.ApplyCodingInferenceGrantSettlementParams{
			Status: "exhausted", Now: pgTime(now), GrantID: grant.GrantID,
		}); err != nil || tx.Commit(ctx) != nil {
			return zero, httpErrorf(500, "coding inference admission failed")
		}
		return zero, httpErrorf(409, "coding inference request budget is exhausted")
	}
	remainingCompletion := grant.CompletionTokenBudget - grant.CompletionTokens
	if dispatch.LockedRequest.MaxCompletionTokens > remainingCompletion {
		return zero, httpErrorf(409, "coding inference completion budget is exhausted")
	}
	// Serialize cross-grant COUNT + INSERT so concurrent transactions locking
	// different grants cannot all pass the concurrency rails before inserting.
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext('coding_inference_admission'))"); err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	validatorActive, err := q.CountActiveCodingInferenceRequestsForValidator(ctx, postgres.CountActiveCodingInferenceRequestsForValidatorParams{
		Now: pgTime(now), ValidatorHotkey: grant.ValidatorHotkey,
	})
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	globalActive, err := q.CountActiveCodingInferenceRequestsGlobal(ctx, pgTime(now))
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	if validatorActive >= int64(d.Cfg.Inference.CodingValidatorConcurrency) ||
		globalActive >= int64(d.Cfg.Inference.CodingGlobalConcurrency) {
		return zero, &httpError{
			status: http.StatusTooManyRequests, message: "coding inference is at capacity",
			headers: map[string]string{"Retry-After": "1"},
		}
	}
	requestID, _ := uuid.Parse(dispatch.RequestID)
	ticketID, _ := uuid.Parse(dispatch.TicketID)
	row, err := q.InsertCodingInferenceRequest(ctx, postgres.InsertCodingInferenceRequestParams{
		RequestRowID: pgUUID(uuid.New()), GrantID: grant.GrantID, TicketID: pgUUID(ticketID),
		Generation: dispatch.Generation, Sequence: dispatch.Sequence,
		RequestSequence: dispatch.RequestSequence, Attempt: dispatch.Attempt,
		RequestID: pgUUID(requestID), CaseID: dispatch.CaseID,
		ProfileCapabilityID:  dispatch.ProfileCapabilityID,
		InferenceGrantSha256: dispatch.InferenceGrantSHA256,
		LockedRequestSha256:  dispatch.LockedRequestSHA256, StartedAt: pgTime(now),
	})
	if err != nil {
		return zero, httpErrorf(409, "coding inference request conflicts with durable state")
	}
	if err := q.BeginCodingInferenceGrantRequest(ctx, postgres.BeginCodingInferenceGrantRequestParams{
		RequestIncrement: requestIncrement, Now: pgTime(now), GrantID: grant.GrantID,
	}); err != nil || tx.Commit(ctx) != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	grant.RequestCount += requestIncrement
	grant.ActiveRequests = 1
	return codingAdmittedDispatch{
		kind: codingLedgerTicket, expiresAt: grant.ExpiresAt.Time.UTC(),
		ticketGrant: grant, ticketRequest: row,
	}, nil
}

func codingGrantAuthenticates(
	grant postgres.CodingInferenceGrant,
	headers *proxyHeaders,
	bearer string,
	body []byte,
) bool {
	if !grant.BearerDigest.Valid || !grant.BrokerPublicKey.Valid || grant.Generation != int32(headers.generation) ||
		len(headers.proof) < 86 || len(headers.proof) > 88 ||
		!constantTimeEqual(grant.BearerDigest.String, bearerDigest(bearer)) {
		return false
	}
	publicKey, err := decodeBrokerPublicKey(grant.BrokerPublicKey.String)
	if err != nil {
		return false
	}
	proof, err := decodeProof(headers.proof)
	if err != nil || len(proof) != ed25519.SignatureSize {
		return false
	}
	return ed25519.Verify(publicKey, proxyMessage(headers.grant, headers.generation, headers.nonce, headers.requestedAt, body), proof)
}

func codingGrantMatchesDispatch(grant postgres.CodingInferenceGrant, dispatch codingDispatchRequest) bool {
	return uuid.UUID(grant.GrantID.Bytes).String() == dispatch.GrantID &&
		uuid.UUID(grant.TicketID.Bytes).String() == dispatch.TicketID &&
		grant.Generation == dispatch.Generation && grant.TaskCount == 1 &&
		grant.CaseID == dispatch.CaseID && grant.ProfileCapabilityID == dispatch.ProfileCapabilityID &&
		grant.InferenceGrantSha256 == dispatch.InferenceGrantSHA256 &&
		grant.InferenceGrantSha256 == codingInferenceGrantSHA256 && grant.Model == codingModel &&
		grant.ProviderApi == codingProviderAPI && grant.ProviderRoute == codingProviderRoute &&
		grant.ReceiptProvider == codingReceiptProvider && grant.ProviderRouteProfile == codingProviderRouteProfile &&
		grant.ProviderAccountGuardrail == codingAccountGuardrail && grant.ProviderPipelinePolicy == codingPipelinePolicy &&
		grant.ProviderCachePolicy == codingCachePolicy && grant.ReasoningEffort == codingReasoningEffort &&
		grant.RequestBudget >= 1 && grant.RequestBudget <= codingMaxRequests &&
		grant.PromptTokenBudget >= 1 && grant.PromptTokenBudget <= codingMaxPromptTokens &&
		grant.CompletionTokenBudget >= 1 && grant.CompletionTokenBudget <= codingMaxCompletionTokens &&
		grant.CostBudgetUsdMicros >= 1 && grant.CostBudgetUsdMicros <= codingMaxCostUSDMicros && !grant.WeightEligible
}

func (d *Deps) admitCertificationCodingDispatch(
	ctx context.Context,
	tx pgx.Tx,
	q *postgres.Queries,
	headers *proxyHeaders,
	bearer string,
	body []byte,
	dispatch codingDispatchRequest,
) (codingAdmittedDispatch, *httpError) {
	var zero codingAdmittedDispatch
	grant, err := q.GetCodingCertificationInferenceGrantForUpdate(ctx, pgUUID(headers.grant))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return zero, httpErrorf(401, "invalid coding inference proof")
		}
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	if !codingCertificationGrantAuthenticates(grant, headers, bearer, body) {
		return zero, httpErrorf(401, "invalid coding inference proof")
	}
	now, err := codingDatabaseNow(ctx, q)
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	deadline, deadlineErr := time.Parse(time.RFC3339Nano, dispatch.Deadline)
	if deadlineErr != nil || !grant.ExpiresAt.Valid || !deadline.Equal(grant.ExpiresAt.Time.UTC()) ||
		!grant.ExpiresAt.Time.After(now) || !codingCertificationGrantMatchesDispatch(grant, dispatch) {
		if grant.Status == "active" && grant.ExpiresAt.Valid && !grant.ExpiresAt.Time.After(now) {
			if revokeErr := q.RevokeCodingCertificationInferenceGrantUnsettled(ctx, postgres.RevokeCodingCertificationInferenceGrantUnsettledParams{
				Now: pgTime(now), GrantID: grant.GrantID,
			}); revokeErr != nil || tx.Commit(ctx) != nil {
				return zero, httpErrorf(500, "coding inference admission failed")
			}
		}
		return zero, httpErrorf(409, "coding inference grant is not live")
	}
	if grant.Status != "active" || grant.ActiveRequests != 0 ||
		grant.PromptTokens >= grant.PromptTokenBudget ||
		grant.CompletionTokens >= grant.CompletionTokenBudget ||
		grant.CostUsdMicros >= grant.CostBudgetUsdMicros {
		if grant.Status == "active" && grant.ActiveRequests == 0 {
			if settleErr := q.ApplyCodingCertificationInferenceGrantSettlement(ctx, postgres.ApplyCodingCertificationInferenceGrantSettlementParams{
				Status: "exhausted", Now: pgTime(now), GrantID: grant.GrantID,
			}); settleErr != nil || tx.Commit(ctx) != nil {
				return zero, httpErrorf(500, "coding inference admission failed")
			}
		}
		return zero, httpErrorf(409, "coding inference grant is unavailable")
	}
	latest, latestErr := q.GetLatestCodingCertificationInferenceRequestForUpdate(ctx, grant.GrantID)
	requestIncrement := int32(0)
	if errors.Is(latestErr, pgx.ErrNoRows) {
		if grant.RequestCount != 0 || dispatch.Sequence != 1 || dispatch.RequestSequence != 1 || dispatch.Attempt != 1 {
			return zero, httpErrorf(409, "coding inference request order is invalid")
		}
		requestIncrement = 1
	} else if latestErr != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	} else {
		if grant.RequestCount != latest.RequestSequence || latest.WeightEligible {
			return zero, httpErrorf(409, "coding inference request history is inconsistent")
		}
		switch latest.Status {
		case "receipt_free_retry":
			if dispatch.Sequence != latest.Sequence+1 || dispatch.RequestSequence != latest.RequestSequence ||
				dispatch.Attempt != latest.Attempt+1 || dispatch.RequestID != uuid.UUID(latest.RequestID.Bytes).String() ||
				dispatch.LockedRequestSHA256 != latest.LockedRequestSha256 {
				return zero, httpErrorf(409, "coding inference retry identity is invalid")
			}
		case "complete":
			if dispatch.Sequence != latest.Sequence+1 || dispatch.RequestSequence != latest.RequestSequence+1 || dispatch.Attempt != 1 {
				return zero, httpErrorf(409, "coding inference request order is invalid")
			}
			requestIncrement = 1
		default:
			return zero, httpErrorf(409, "coding inference request history is terminal")
		}
	}
	if requestIncrement == 1 && grant.RequestCount >= grant.RequestBudget {
		if err := q.ApplyCodingCertificationInferenceGrantSettlement(ctx, postgres.ApplyCodingCertificationInferenceGrantSettlementParams{
			Status: "exhausted", Now: pgTime(now), GrantID: grant.GrantID,
		}); err != nil || tx.Commit(ctx) != nil {
			return zero, httpErrorf(500, "coding inference admission failed")
		}
		return zero, httpErrorf(409, "coding inference request budget is exhausted")
	}
	remainingCompletion := grant.CompletionTokenBudget - grant.CompletionTokens
	if dispatch.LockedRequest.MaxCompletionTokens > remainingCompletion {
		return zero, httpErrorf(409, "coding inference completion budget is exhausted")
	}
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext('coding_inference_admission'))"); err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	validatorActive, err := q.CountActiveCodingInferenceRequestsForValidator(ctx, postgres.CountActiveCodingInferenceRequestsForValidatorParams{
		Now: pgTime(now), ValidatorHotkey: grant.ValidatorHotkey,
	})
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	globalActive, err := q.CountActiveCodingInferenceRequestsGlobal(ctx, pgTime(now))
	if err != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	if validatorActive >= int64(d.Cfg.Inference.CodingValidatorConcurrency) ||
		globalActive >= int64(d.Cfg.Inference.CodingGlobalConcurrency) {
		return zero, &httpError{
			status: http.StatusTooManyRequests, message: "coding inference is at capacity",
			headers: map[string]string{"Retry-After": "1"},
		}
	}
	requestID, _ := uuid.Parse(dispatch.RequestID)
	leaseID, _ := uuid.Parse(dispatch.TicketID)
	row, err := q.InsertCodingCertificationInferenceRequest(ctx, postgres.InsertCodingCertificationInferenceRequestParams{
		RequestRowID: pgUUID(uuid.New()), GrantID: grant.GrantID, LeaseID: pgUUID(leaseID),
		Generation: dispatch.Generation, Sequence: dispatch.Sequence,
		RequestSequence: dispatch.RequestSequence, Attempt: dispatch.Attempt,
		RequestID: pgUUID(requestID), CaseID: dispatch.CaseID,
		ProfileCapabilityID:  dispatch.ProfileCapabilityID,
		InferenceGrantSha256: dispatch.InferenceGrantSHA256,
		LockedRequestSha256:  dispatch.LockedRequestSHA256, StartedAt: pgTime(now),
	})
	if err != nil {
		return zero, httpErrorf(409, "coding inference request conflicts with durable state")
	}
	if err := q.BeginCodingCertificationInferenceGrantRequest(ctx, postgres.BeginCodingCertificationInferenceGrantRequestParams{
		RequestIncrement: requestIncrement, Now: pgTime(now), GrantID: grant.GrantID,
	}); err != nil || tx.Commit(ctx) != nil {
		return zero, httpErrorf(500, "coding inference admission failed")
	}
	return codingAdmittedDispatch{
		kind: codingLedgerCertification, expiresAt: grant.ExpiresAt.Time.UTC(),
		certGrant: grant, certRequest: row,
	}, nil
}

func codingCertificationGrantAuthenticates(
	grant postgres.CodingCertificationInferenceGrant,
	headers *proxyHeaders,
	bearer string,
	body []byte,
) bool {
	if !grant.BearerDigest.Valid || !grant.BrokerPublicKey.Valid || grant.Generation != int32(headers.generation) ||
		len(headers.proof) < 86 || len(headers.proof) > 88 ||
		!constantTimeEqual(grant.BearerDigest.String, bearerDigest(bearer)) {
		return false
	}
	publicKey, err := decodeBrokerPublicKey(grant.BrokerPublicKey.String)
	if err != nil {
		return false
	}
	proof, err := decodeProof(headers.proof)
	if err != nil || len(proof) != ed25519.SignatureSize {
		return false
	}
	return ed25519.Verify(publicKey, proxyMessage(headers.grant, headers.generation, headers.nonce, headers.requestedAt, body), proof)
}

func codingCertificationGrantMatchesDispatch(
	grant postgres.CodingCertificationInferenceGrant,
	dispatch codingDispatchRequest,
) bool {
	return uuid.UUID(grant.GrantID.Bytes).String() == dispatch.GrantID &&
		uuid.UUID(grant.LeaseID.Bytes).String() == dispatch.TicketID &&
		grant.Generation == dispatch.Generation &&
		grant.CaseID == dispatch.CaseID && grant.ProfileCapabilityID == dispatch.ProfileCapabilityID &&
		grant.CaseID == codingCanaryCaseID && grant.ProfileCapabilityID == codingCanaryProfileID &&
		grant.InferenceGrantSha256 == dispatch.InferenceGrantSHA256 &&
		grant.InferenceGrantSha256 == codingInferenceGrantSHA256 && grant.Model == codingModel &&
		grant.ProviderApi == codingProviderAPI && grant.ProviderRoute == codingProviderRoute &&
		grant.ReceiptProvider == codingReceiptProvider && grant.ProviderRouteProfile == codingProviderRouteProfile &&
		grant.ProviderAccountGuardrail == codingAccountGuardrail && grant.ProviderPipelinePolicy == codingPipelinePolicy &&
		grant.ProviderCachePolicy == codingCachePolicy && grant.ReasoningEffort == codingReasoningEffort &&
		grant.RequestBudget == codingCanaryRequestBudget &&
		grant.PromptTokenBudget == codingCanaryPromptTokenBudget &&
		grant.CompletionTokenBudget == codingCanaryCompletionTokenBudget &&
		grant.CostBudgetUsdMicros == codingMaxCostUSDMicros && !grant.WeightEligible
}

func (d *Deps) settleCodingDispatch(
	ctx context.Context,
	admitted codingAdmittedDispatch,
	dispatch codingDispatchRequest,
	outcome codingProviderOutcome,
) error {
	if admitted.kind == codingLedgerCertification {
		return d.settleCertificationCodingDispatch(ctx, admitted.certGrant, admitted.certRequest, dispatch, outcome)
	}
	return d.settleTicketCodingDispatch(ctx, admitted.ticketGrant, admitted.ticketRequest, dispatch, outcome)
}

func (d *Deps) settleTicketCodingDispatch(
	ctx context.Context,
	grant postgres.CodingInferenceGrant,
	request postgres.CodingInferenceRequest,
	dispatch codingDispatchRequest,
	outcome codingProviderOutcome,
) error {
	settlementSHA256, settlementJSON, err := codingSettlementDigest(outcome.settlement)
	if err != nil {
		return err
	}
	txCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	tx, err := d.Pool.Begin(txCtx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(txCtx) }()
	q := d.Queries.WithTx(tx)
	lockedGrant, err := q.GetCodingInferenceGrantForUpdate(txCtx, grant.GrantID)
	if err != nil {
		return err
	}
	lockedRequest, err := q.GetCodingInferenceRequestForUpdate(txCtx, postgres.GetCodingInferenceRequestForUpdateParams{
		GrantID: grant.GrantID, Sequence: dispatch.Sequence,
	})
	if err != nil || lockedRequest.Status != "started" || lockedRequest.RequestRowID != request.RequestRowID ||
		lockedRequest.Generation != dispatch.Generation || lockedRequest.RequestSequence != dispatch.RequestSequence ||
		lockedRequest.Attempt != dispatch.Attempt || uuid.UUID(lockedRequest.RequestID.Bytes).String() != dispatch.RequestID ||
		lockedRequest.LockedRequestSha256 != dispatch.LockedRequestSHA256 {
		return errors.New("coding inference settlement binding drifted")
	}
	if lockedGrant.Generation != dispatch.Generation || (lockedGrant.Status != "active" && lockedGrant.Status != "revoked") ||
		(lockedGrant.Status == "active" && lockedGrant.ActiveRequests != 1) {
		return errors.New("coding inference grant cannot settle request")
	}
	providerGeneration := pgtype.Text{}
	if outcome.settlement.ProviderGenerationID != nil {
		providerGeneration = pgtype.Text{String: *outcome.settlement.ProviderGenerationID, Valid: true}
	}
	if _, err := q.FindCodingInferenceSettlementIdentity(txCtx, postgres.FindCodingInferenceSettlementIdentityParams{
		RequestRowID: lockedRequest.RequestRowID, ProviderSettlementSha256: settlementSHA256,
		ProviderGenerationID: providerGeneration,
	}); err == nil {
		return errors.New("coding inference provider identity was reused")
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	now, err := codingDatabaseNow(txCtx, q)
	if err != nil {
		return err
	}
	if err := q.SettleCodingInferenceRequest(txCtx, postgres.SettleCodingInferenceRequestParams{
		Status: outcome.settlement.Outcome, ProviderSettlementSha256: settlementSHA256,
		ProviderGenerationID: providerGeneration, ProviderSettlementJson: string(settlementJSON),
		SettledAt: pgTime(now), RequestRowID: lockedRequest.RequestRowID,
	}); err != nil {
		return err
	}
	status := lockedGrant.Status
	if status == "active" {
		switch outcome.settlement.Outcome {
		case "provider_failure":
			status = "revoked"
		case "receipt_free_retry":
			if dispatch.Attempt >= codingMaxAttempts {
				status = "revoked"
			}
		case "complete":
			if lockedGrant.RequestCount >= lockedGrant.RequestBudget ||
				lockedGrant.PromptTokens+outcome.settlement.PromptTokens >= lockedGrant.PromptTokenBudget ||
				lockedGrant.CompletionTokens+outcome.settlement.CompletionTokens >= lockedGrant.CompletionTokenBudget ||
				lockedGrant.CostUsdMicros+outcome.settlement.CostUSDMicros >= lockedGrant.CostBudgetUsdMicros {
				status = "exhausted"
			}
		}
	}
	if err := q.ApplyCodingInferenceGrantSettlement(txCtx, postgres.ApplyCodingInferenceGrantSettlementParams{
		Status: status, PromptTokens: outcome.settlement.PromptTokens,
		CompletionTokens: outcome.settlement.CompletionTokens, CostUsdMicros: outcome.settlement.CostUSDMicros,
		Now: pgTime(now), GrantID: lockedGrant.GrantID,
	}); err != nil {
		return err
	}
	return tx.Commit(txCtx)
}

func (d *Deps) settleCertificationCodingDispatch(
	ctx context.Context,
	grant postgres.CodingCertificationInferenceGrant,
	request postgres.CodingCertificationInferenceRequest,
	dispatch codingDispatchRequest,
	outcome codingProviderOutcome,
) error {
	settlementSHA256, settlementJSON, err := codingSettlementDigest(outcome.settlement)
	if err != nil {
		return err
	}
	txCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	tx, err := d.Pool.Begin(txCtx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(txCtx) }()
	q := d.Queries.WithTx(tx)
	lockedGrant, err := q.GetCodingCertificationInferenceGrantForUpdate(txCtx, grant.GrantID)
	if err != nil {
		return err
	}
	lockedRequest, err := q.GetCodingCertificationInferenceRequestForUpdate(txCtx, postgres.GetCodingCertificationInferenceRequestForUpdateParams{
		GrantID: grant.GrantID, Sequence: dispatch.Sequence,
	})
	if err != nil || lockedRequest.Status != "started" || lockedRequest.RequestRowID != request.RequestRowID ||
		lockedRequest.Generation != dispatch.Generation || lockedRequest.RequestSequence != dispatch.RequestSequence ||
		lockedRequest.Attempt != dispatch.Attempt || uuid.UUID(lockedRequest.RequestID.Bytes).String() != dispatch.RequestID ||
		lockedRequest.LockedRequestSha256 != dispatch.LockedRequestSHA256 {
		return errors.New("coding inference settlement binding drifted")
	}
	if lockedGrant.Generation != dispatch.Generation || (lockedGrant.Status != "active" && lockedGrant.Status != "revoked") ||
		(lockedGrant.Status == "active" && lockedGrant.ActiveRequests != 1) {
		return errors.New("coding inference grant cannot settle request")
	}
	providerGeneration := pgtype.Text{}
	if outcome.settlement.ProviderGenerationID != nil {
		providerGeneration = pgtype.Text{String: *outcome.settlement.ProviderGenerationID, Valid: true}
	}
	if _, err := q.FindCodingInferenceSettlementIdentity(txCtx, postgres.FindCodingInferenceSettlementIdentityParams{
		RequestRowID: lockedRequest.RequestRowID, ProviderSettlementSha256: settlementSHA256,
		ProviderGenerationID: providerGeneration,
	}); err == nil {
		return errors.New("coding inference provider identity was reused")
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	now, err := codingDatabaseNow(txCtx, q)
	if err != nil {
		return err
	}
	if err := q.SettleCodingCertificationInferenceRequest(txCtx, postgres.SettleCodingCertificationInferenceRequestParams{
		Status: outcome.settlement.Outcome, ProviderSettlementSha256: settlementSHA256,
		ProviderGenerationID: providerGeneration, ProviderSettlementJson: string(settlementJSON),
		SettledAt: pgTime(now), RequestRowID: lockedRequest.RequestRowID,
	}); err != nil {
		return err
	}
	status := lockedGrant.Status
	if status == "active" {
		switch outcome.settlement.Outcome {
		case "provider_failure":
			status = "revoked"
		case "receipt_free_retry":
			if dispatch.Attempt >= codingMaxAttempts {
				status = "revoked"
			}
		case "complete":
			if lockedGrant.RequestCount >= lockedGrant.RequestBudget ||
				lockedGrant.PromptTokens+outcome.settlement.PromptTokens >= lockedGrant.PromptTokenBudget ||
				lockedGrant.CompletionTokens+outcome.settlement.CompletionTokens >= lockedGrant.CompletionTokenBudget ||
				lockedGrant.CostUsdMicros+outcome.settlement.CostUSDMicros >= lockedGrant.CostBudgetUsdMicros {
				status = "exhausted"
			}
		}
	}
	if err := q.ApplyCodingCertificationInferenceGrantSettlement(txCtx, postgres.ApplyCodingCertificationInferenceGrantSettlementParams{
		Status: status, PromptTokens: outcome.settlement.PromptTokens,
		CompletionTokens: outcome.settlement.CompletionTokens, CostUsdMicros: outcome.settlement.CostUSDMicros,
		Now: pgTime(now), GrantID: lockedGrant.GrantID,
	}); err != nil {
		return err
	}
	return tx.Commit(txCtx)
}

func (d *Deps) markCodingUnsettled(
	ctx context.Context,
	admitted codingAdmittedDispatch,
	reason string,
) error {
	if admitted.kind == codingLedgerCertification {
		return d.markCertificationCodingUnsettled(ctx, admitted.certGrant, admitted.certRequest, reason)
	}
	return d.markTicketCodingUnsettled(ctx, admitted.ticketGrant, admitted.ticketRequest, reason)
}

func (d *Deps) markTicketCodingUnsettled(
	ctx context.Context,
	grant postgres.CodingInferenceGrant,
	request postgres.CodingInferenceRequest,
	reason string,
) error {
	if reason != "provider_settlement_unavailable" && reason != "invalid_provider_settlement" {
		return errors.New("invalid coding inference unsettled reason")
	}
	txCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	tx, err := d.Pool.Begin(txCtx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(txCtx) }()
	q := d.Queries.WithTx(tx)
	lockedGrant, err := q.GetCodingInferenceGrantForUpdate(txCtx, grant.GrantID)
	if err != nil {
		return err
	}
	lockedRequest, err := q.GetCodingInferenceRequestForUpdate(txCtx, postgres.GetCodingInferenceRequestForUpdateParams{
		GrantID: grant.GrantID, Sequence: request.Sequence,
	})
	if err != nil {
		return err
	}
	if lockedRequest.RequestRowID != request.RequestRowID {
		return errors.New("coding inference unsettled request drifted")
	}
	if lockedRequest.Status != "started" {
		if lockedRequest.Status == "unsettled" && lockedRequest.UnsettledReason.Valid && lockedRequest.UnsettledReason.String == reason {
			return nil
		}
		return errors.New("coding inference request is already terminal")
	}
	now, err := codingDatabaseNow(txCtx, q)
	if err != nil {
		return err
	}
	if err := q.MarkCodingInferenceRequestUnsettled(txCtx, postgres.MarkCodingInferenceRequestUnsettledParams{
		UnsettledReason: reason, SettledAt: pgTime(now), RequestRowID: lockedRequest.RequestRowID,
	}); err != nil {
		return err
	}
	if lockedGrant.Status == "active" {
		if err := q.RevokeCodingInferenceGrantUnsettled(txCtx, postgres.RevokeCodingInferenceGrantUnsettledParams{
			Now: pgTime(now), GrantID: lockedGrant.GrantID,
		}); err != nil {
			return err
		}
	}
	return tx.Commit(txCtx)
}

func (d *Deps) markCertificationCodingUnsettled(
	ctx context.Context,
	grant postgres.CodingCertificationInferenceGrant,
	request postgres.CodingCertificationInferenceRequest,
	reason string,
) error {
	if reason != "provider_settlement_unavailable" && reason != "invalid_provider_settlement" {
		return errors.New("invalid coding inference unsettled reason")
	}
	txCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	tx, err := d.Pool.Begin(txCtx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(txCtx) }()
	q := d.Queries.WithTx(tx)
	lockedGrant, err := q.GetCodingCertificationInferenceGrantForUpdate(txCtx, grant.GrantID)
	if err != nil {
		return err
	}
	lockedRequest, err := q.GetCodingCertificationInferenceRequestForUpdate(txCtx, postgres.GetCodingCertificationInferenceRequestForUpdateParams{
		GrantID: grant.GrantID, Sequence: request.Sequence,
	})
	if err != nil {
		return err
	}
	if lockedRequest.RequestRowID != request.RequestRowID {
		return errors.New("coding inference unsettled request drifted")
	}
	if lockedRequest.Status != "started" {
		if lockedRequest.Status == "unsettled" && lockedRequest.UnsettledReason.Valid && lockedRequest.UnsettledReason.String == reason {
			return nil
		}
		return errors.New("coding inference request is already terminal")
	}
	now, err := codingDatabaseNow(txCtx, q)
	if err != nil {
		return err
	}
	if err := q.MarkCodingCertificationInferenceRequestUnsettled(txCtx, postgres.MarkCodingCertificationInferenceRequestUnsettledParams{
		UnsettledReason: reason, SettledAt: pgTime(now), RequestRowID: lockedRequest.RequestRowID,
	}); err != nil {
		return err
	}
	if lockedGrant.Status == "active" {
		if err := q.RevokeCodingCertificationInferenceGrantUnsettled(txCtx, postgres.RevokeCodingCertificationInferenceGrantUnsettledParams{
			Now: pgTime(now), GrantID: lockedGrant.GrantID,
		}); err != nil {
			return err
		}
	}
	return tx.Commit(txCtx)
}

func validateCodingSettlement(settlement codingProviderSettlement, dispatch codingDispatchRequest) error {
	if settlement.Schema != codingSettlementSchema || settlement.CodingContractVersion != 1 ||
		settlement.TicketID != dispatch.TicketID || settlement.CaseID != dispatch.CaseID ||
		settlement.ProfileCapabilityID != dispatch.ProfileCapabilityID ||
		settlement.InferenceGrantSHA256 != dispatch.InferenceGrantSHA256 ||
		settlement.GrantID != dispatch.GrantID || settlement.Generation != dispatch.Generation ||
		settlement.RequestID != dispatch.RequestID || settlement.RequestSequence != dispatch.RequestSequence ||
		settlement.Attempt != dispatch.Attempt || settlement.LockedRequestSHA256 != dispatch.LockedRequestSHA256 ||
		settlement.Model != codingModel || settlement.ProviderAPI != codingProviderAPI ||
		settlement.ProviderRoute != codingProviderRoute || settlement.ProviderRouteProfile != codingProviderRouteProfile ||
		settlement.ProviderAccountGuardrail != codingAccountGuardrail ||
		settlement.ProviderPipelinePolicy != codingPipelinePolicy || settlement.ProviderCachePolicy != codingCachePolicy ||
		!settlement.RouterMetadataVerified || settlement.FallbackUsed || len(settlement.RouterAttempts) != 1 ||
		settlement.RouterAttempts[0].Provider != codingReceiptProvider || len(settlement.PipelineStages) != 0 ||
		settlement.HTTPStatus < 0 || settlement.HTTPStatus > 599 ||
		(settlement.ResponseSHA256 != nil && !validCodingSHA256(*settlement.ResponseSHA256)) ||
		(settlement.ProviderGenerationID != nil && !validCodingIdentifier(*settlement.ProviderGenerationID, 256)) ||
		settlement.PromptTokens < 0 || settlement.CompletionTokens < 0 || settlement.CostUSDMicros < 0 ||
		(!settlement.UsageAvailable && (settlement.PromptTokens != 0 || settlement.CompletionTokens != 0 || settlement.TotalTokens != 0)) ||
		(!settlement.CostAvailable && settlement.CostUSDMicros != 0) ||
		settlement.PromptTokens > math.MaxInt64-settlement.CompletionTokens ||
		settlement.TotalTokens != settlement.PromptTokens+settlement.CompletionTokens ||
		settlement.PromptTokens > codingMaxPromptTokens || settlement.CompletionTokens > codingMaxCompletionPerCall ||
		settlement.TotalTokens > codingMaxTotalTokens || settlement.CostUSDMicros > codingMaxCostUSDMicros {
		return errors.New("coding provider settlement is invalid")
	}
	selected := settlement.RouterAttempts[0].Selected
	responsePresent := settlement.ResponseSHA256 != nil && validCodingSHA256(*settlement.ResponseSHA256)
	switch settlement.Outcome {
	case "complete":
		if settlement.TerminalErrorCode != nil || settlement.HTTPStatus < 200 || settlement.HTTPStatus >= 300 ||
			!responsePresent || settlement.ResponseDigestKind != "normalized_v1" ||
			settlement.ProviderGenerationID == nil || !validCodingIdentifier(*settlement.ProviderGenerationID, 256) ||
			settlement.ReceiptProvider == nil || *settlement.ReceiptProvider != codingReceiptProvider ||
			!selected || !settlement.UsageAvailable || !settlement.CostAvailable || settlement.TimedOut {
			return errors.New("coding completed settlement is invalid")
		}
	case "receipt_free_retry":
		if settlement.TerminalErrorCode == nil || *settlement.TerminalErrorCode != "pre_provider_unavailable" ||
			!codingRetryStatus(settlement.HTTPStatus) || responsePresent || settlement.ResponseDigestKind != "none" ||
			settlement.ProviderGenerationID != nil || settlement.ReceiptProvider != nil || selected ||
			settlement.UsageAvailable || settlement.CostAvailable || settlement.TimedOut {
			return errors.New("coding retry settlement is invalid")
		}
	case "provider_failure":
		if settlement.TerminalErrorCode == nil ||
			(*settlement.TerminalErrorCode != "provider_http" && *settlement.TerminalErrorCode != "provider_response_invalid") ||
			!responsePresent || settlement.ResponseDigestKind != "canonical_json_v1" || settlement.TimedOut {
			return errors.New("coding provider failure settlement is invalid")
		}
		if *settlement.TerminalErrorCode == "provider_http" && settlement.HTTPStatus < 400 {
			return errors.New("coding provider HTTP failure status is invalid")
		}
		if *settlement.TerminalErrorCode == "provider_response_invalid" &&
			(settlement.HTTPStatus < 200 || settlement.HTTPStatus >= 300 || !selected) {
			return errors.New("coding invalid-response settlement is invalid")
		}
		if selected {
			if settlement.ReceiptProvider == nil || *settlement.ReceiptProvider != codingReceiptProvider ||
				!settlement.UsageAvailable || !settlement.CostAvailable {
				return errors.New("coding selected-provider failure lacks accounting")
			}
		} else if settlement.ReceiptProvider != nil || settlement.ProviderGenerationID != nil ||
			settlement.UsageAvailable || settlement.CostAvailable {
			return errors.New("coding unselected-provider failure has accounting")
		}
	default:
		return errors.New("coding settlement outcome is invalid")
	}
	return nil
}

func codingProofHeadersUnique(header http.Header) bool {
	for _, name := range []string{
		"Authorization", "X-Ditto-Grant", "X-Ditto-Generation",
		"X-Ditto-Nonce", "X-Ditto-Requested-At", "X-Ditto-Proof",
	} {
		if len(header.Values(name)) != 1 {
			return false
		}
	}
	return true
}

func codingRetryStatus(status int) bool {
	switch status {
	case 404, 408, 429, 500, 502, 503, 504:
		return true
	default:
		return false
	}
}

func zeroCodingBytes(value []byte) {
	for index := range value {
		value[index] = 0
	}
}

func codingDatabaseNow(ctx context.Context, queries *postgres.Queries) (time.Time, error) {
	if queries == nil {
		return time.Time{}, errors.New("coding database clock is unavailable")
	}
	value, err := queries.GetCodingDatabaseNow(ctx)
	if err != nil || !value.Valid || value.Time.IsZero() {
		return time.Time{}, errors.New("coding database clock is unavailable")
	}
	return value.Time.UTC(), nil
}
