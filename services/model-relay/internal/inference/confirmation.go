package inference

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// confirmationDecline mirrors ConfirmationInferenceDecline (a StrEnum whose
// value is what the 429 detail interpolates).
type confirmationDecline string

const (
	confirmationDeclineUnattributed         confirmationDecline = "unattributed"
	confirmationDeclineLeaseExpired         confirmationDecline = "lease_expired"
	confirmationDeclineGrantRevoked         confirmationDecline = "grant_revoked"
	confirmationDeclineBudgetExhausted      confirmationDecline = "budget_exhausted"
	confirmationDeclineTokenBudgetExhausted confirmationDecline = "token_budget_exhausted"
	confirmationDeclineCostBudgetExhausted  confirmationDecline = "cost_budget_exhausted"
	confirmationDeclineModelNotPermitted    confirmationDecline = "model_not_permitted"
	confirmationDeclineNonceReplayed        confirmationDecline = "nonce_replayed"
)

// checkConfirmationHeaders mirrors _confirmation_headers: any missing header
// (including a non-Bearer Authorization) is the uniform 401 "missing
// confirmation proof"; a stale requested_at is 409 BEFORE the body is read.
// Returns the bearer (Authorization sans "Bearer ").
func (d *Deps) checkConfirmationHeaders(h *proxyHeaders) (string, *httpError) {
	if h.anyMissing() || !strings.HasPrefix(h.authorization, "Bearer ") {
		return "", httpErrorf(401, "missing confirmation proof")
	}
	if absDuration(d.now().Sub(h.requestedAt)) > proxyMaxAge {
		return "", httpErrorf(409, "confirmation request is stale")
	}
	return strings.TrimPrefix(h.authorization, "Bearer "), nil
}

// verifyConfirmationProof mirrors _confirmation_proof_headers: generation
// mismatch or any decode/verification failure is the uniform 401 "invalid
// confirmation proof". The signed bytes are the exact ordinary _proxy_message.
func verifyConfirmationProof(grant *postgres.ConfirmationInferenceGrant, h *proxyHeaders, body []byte) *httpError {
	invalid := httpErrorf(401, "invalid confirmation proof")
	if grant.BrokerPublicKey == "" || int64(grant.Generation) != h.generation {
		return invalid
	}
	key, err := decodeBrokerPublicKey(grant.BrokerPublicKey)
	if err != nil {
		return invalid
	}
	proof, err := decodeProof(h.proof)
	if err != nil {
		return invalid
	}
	message := proxyMessage(h.grant, h.generation, h.nonce, h.requestedAt, body)
	if !ed25519.Verify(key, message, proof) {
		return invalid
	}
	return nil
}

// pythonJSONEqual compares a decoded JSON value (UseNumber form) against an
// expected literal with Python == semantics — including numeric/bool
// cross-equality (in Python, 1 == True and 0 == False), which plain
// reflect.DeepEqual would miss.
func pythonJSONEqual(actual, expected any) bool {
	switch want := expected.(type) {
	case string:
		got, ok := actual.(string)
		return ok && got == want
	case bool:
		if got, ok := actual.(bool); ok {
			return got == want
		}
		if n, ok := asNumber(actual); ok {
			target := 0.0
			if want {
				target = 1.0
			}
			return numFloat(n) == target
		}
		return false
	case []any:
		got, ok := actual.([]any)
		if !ok || len(got) != len(want) {
			return false
		}
		for i := range want {
			if !pythonJSONEqual(got[i], want[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		got, ok := actual.(map[string]any)
		if !ok || len(got) != len(want) {
			return false
		}
		for key, wantValue := range want {
			gotValue, present := got[key]
			if !present || !pythonJSONEqual(gotValue, wantValue) {
				return false
			}
		}
		return true
	case nil:
		return actual == nil
	}
	return false
}

// confirmationReaderRouteProvider is the frozen profile's reader routing
// identity. It is not an OpenRouter slug: the reader uses the scoring-lane
// throughput aggregate (every ZDR provider except CoreWeave).
const confirmationReaderRouteProvider = "throughput"

// confirmationChatProviderPreferences is the frozen route pin the caller must
// echo verbatim (dict equality) and the base of what goes upstream. The
// reader matches the scoring LLM relay: sort by throughput, ignore CoreWeave,
// allow OpenRouter fallbacks, deny data collection. The official judge stays
// Azure-pinned. The relay adds zdr=true after the echo check.
func confirmationChatProviderPreferences(lane, routeProvider string) map[string]any {
	if lane == "reader" {
		return map[string]any{
			"sort":            "throughput",
			"ignore":          []any{"coreweave"},
			"allow_fallbacks": true,
			"data_collection": "deny",
		}
	}
	return map[string]any{
		"only":               []any{routeProvider},
		"order":              []any{routeProvider},
		"allow_fallbacks":    false,
		"require_parameters": true,
		"data_collection":    "deny",
	}
}

// confirmationProviderPreferences is the judge/Azure pin. Tests that need the
// reader aggregate should call confirmationChatProviderPreferences.
func confirmationProviderPreferences(routeProvider string) map[string]any {
	return confirmationChatProviderPreferences("judge", routeProvider)
}

// confirmationInstrumentBenchVersion is the LongMem confirmation profile's
// instrument epoch. The reader model is openai/gpt-oss-20b, which OpenRouter
// hard-400s unless the scoring-lane reasoning contract is applied: nested
// reasoning.effort with exclude=true, and no sibling reasoning_effort alias.
const confirmationInstrumentBenchVersion int32 = 9

// lockedConfirmationChatPayload mirrors _locked_confirmation_chat_payload:
// exact provider-pin equality, schema validation of the remainder, the
// grant-locked model, and the forced upstream shape (zdr added, usage
// included, n=1, stream false). It also applies the same gpt-oss reasoning
// contract and inert-field stripping as the ordinary scoring lane. The reader
// uses the scoring-lane throughput aggregate; forwarding scoring-lane aliases
// would still 400 gpt-oss-20b. The official judge stays Azure-pinned.
func lockedConfirmationChatPayload(decoded any, grant *postgres.ConfirmationInferenceGrant, maxOutputTokens int) (map[string]any, int, *httpError) {
	payload, isObject := decoded.(map[string]any)
	if !isObject {
		return nil, 0, httpErrorf(400, "invalid confirmation request")
	}
	expectedProvider := confirmationChatProviderPreferences(grant.Lane, grant.RouteProvider)
	if !pythonJSONEqual(payload["provider"], expectedProvider) {
		return nil, 0, httpErrorf(403, "confirmation route is not permitted")
	}
	withoutProvider := make(map[string]any, len(payload))
	for key, value := range payload {
		if key != "provider" {
			withoutProvider[key] = value
		}
	}
	if herr := validateRequestSchema(withoutProvider); herr != nil {
		return nil, 0, herr
	}
	if model, ok := withoutProvider["model"].(string); !ok || model != grant.Model {
		return nil, 0, httpErrorf(403, "confirmation model is not permitted")
	}
	maxTokens, herr := outputTokenLimit(withoutProvider, maxOutputTokens)
	if herr != nil {
		return nil, 0, herr
	}
	upstream := make(map[string]any, len(withoutProvider)+4)
	for key, value := range withoutProvider {
		upstream[key] = value
	}
	for field := range droppedRequestFields {
		delete(upstream, field)
	}
	for _, field := range []string{
		"best_of", "reasoning_effort", "include_reasoning", "service_tier", "prompt_cache_key",
	} {
		delete(upstream, field)
	}
	if messages, ok := upstream["messages"].([]any); ok {
		upstream["messages"] = sanitizeUpstreamMessages(messages)
	}
	upstream["model"] = grant.Model
	if grant.Lane == "judge" {
		upstream["max_completion_tokens"] = maxTokens
		delete(upstream, "max_tokens")
	} else {
		upstream["max_tokens"] = maxTokens
		delete(upstream, "max_completion_tokens")
	}
	upstream["n"] = 1
	upstream["stream"] = false
	reasoning, herr := benchmarkReasoningForRequest(
		withoutProvider, grant.Model, confirmationInstrumentBenchVersion,
	)
	if herr != nil {
		return nil, 0, herr
	}
	if reasoning == nil {
		delete(upstream, "reasoning")
	} else {
		upstream["reasoning"] = reasoning
	}
	pinned := make(map[string]any, len(expectedProvider)+1)
	for key, value := range expectedProvider {
		pinned[key] = value
	}
	pinned["zdr"] = true
	upstream["provider"] = pinned
	upstream["usage"] = map[string]any{"include": true}
	return upstream, maxTokens, nil
}

// beginConfirmationParams are the inputs of
// begin_confirmation_inference_request.
type beginConfirmationParams struct {
	grantID             uuid.UUID
	nonce               uuid.UUID
	bearer              string
	model               string
	tokenReservation    int64
	maxChargeableTokens int64
	now                 time.Time
}

// beginConfirmationResult is the successful admission: the LOCKED grant row
// (with the in-transaction mutations applied to the in-memory copy) and the
// inserted request row.
type beginConfirmationResult struct {
	grant   postgres.ConfirmationInferenceGrant
	request postgres.ConfirmationInferenceRequest
}

// beginConfirmationInferenceRequest reproduces ditto/db/queries/
// confirmation_inference.py::begin_confirmation_inference_request
// statement-for-statement inside the caller's transaction. The caller (the
// endpoint) must ROLL BACK on a decline — the mutations the decline paths
// make (revocation/exhaustion status writes, provisional-row deletes) are
// discarded, matching the Python endpoint where the decline raises inside the
// session.begin() block.
//
// Lock order: confirmation_bundle_tickets (rank 1) ->
// confirmation_inference_grants (rank 2) -> confirmation_inference_requests
// (rank 3, via the ON CONFLICT insert — no savepoint needed, PR #712).
func beginConfirmationInferenceRequest(ctx context.Context, q *postgres.Queries, p beginConfirmationParams) (*beginConfirmationResult, *confirmationDecline, error) {
	decline := func(d confirmationDecline) (*beginConfirmationResult, *confirmationDecline, error) {
		return nil, &d, nil
	}
	// Unlocked snapshot, only to learn the owning ticket PK.
	snapshot, err := q.GetConfirmationInferenceGrant(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return decline(confirmationDeclineUnattributed)
		}
		return nil, nil, err
	}
	// Ticket FOR UPDATE (rank 1). A missing ticket is legal here; the
	// liveness gate below fails closed.
	var ticket *postgres.ConfirmationBundleTicket
	ticketRow, err := q.GetConfirmationBundleTicketForUpdate(ctx, snapshot.TicketID)
	if err == nil {
		ticket = &ticketRow
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, nil, err
	}
	// Grant FOR UPDATE (rank 2). Every gate below uses ONLY this row's
	// values.
	grant, err := q.GetConfirmationInferenceGrantForUpdate(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return decline(confirmationDeclineUnattributed)
		}
		return nil, nil, err
	}
	// THE authentication gate.
	if grant.BearerDigest == "" || !constantTimeEqual(grant.BearerDigest, bearerDigest(p.bearer)) {
		return decline(confirmationDeclineUnattributed)
	}
	if grant.Status == "revoked" {
		return decline(confirmationDeclineGrantRevoked)
	}
	if grant.Status == "exhausted" {
		return decline(confirmationDeclineBudgetExhausted)
	}
	// Ticket liveness. Failure revokes the grant (a write the endpoint's
	// decline rollback discards).
	if ticket == nil || ticket.Status != "issued" || !ticket.Deadline.Time.After(p.now) {
		if err := q.RevokeConfirmationInferenceGrant(ctx, postgres.RevokeConfirmationInferenceGrantParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return nil, nil, err
		}
		return decline(confirmationDeclineLeaseExpired)
	}
	if p.model != grant.Model {
		return decline(confirmationDeclineModelNotPermitted)
	}
	// Replay-guarding INSERT (rank 3): ON CONFLICT DO NOTHING RETURNING —
	// zero rows means the nonce was already used. The provisional row is kept
	// constraint-valid (max clamps) so replay detection stays ahead of
	// malformed-reservation classification; a malformed fresh request deletes
	// it below.
	request, err := q.InsertConfirmationInferenceRequest(ctx, postgres.InsertConfirmationInferenceRequestParams{
		GrantID:             pgUUID(p.grantID),
		Nonce:               pgUUID(p.nonce),
		Generation:          grant.Generation,
		Model:               p.model,
		ReservedTokens:      max64(1, p.tokenReservation),
		MaxChargeableTokens: max64(1, max64(p.tokenReservation, p.maxChargeableTokens)),
		StartedAt:           pgTime(p.now),
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return decline(confirmationDeclineNonceReplayed)
		}
		return nil, nil, err
	}
	deleteRequest := func() error {
		return q.DeleteConfirmationInferenceRequest(ctx, postgres.DeleteConfirmationInferenceRequestParams{
			GrantID: pgUUID(p.grantID),
			Nonce:   pgUUID(p.nonce),
		})
	}
	markExhausted := func() error {
		return q.MarkConfirmationInferenceGrantExhausted(ctx, postgres.MarkConfirmationInferenceGrantExhaustedParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		})
	}
	if int64(grant.RequestCount) >= int64(grant.RequestBudget) {
		if err := markExhausted(); err != nil {
			return nil, nil, err
		}
		if err := deleteRequest(); err != nil {
			return nil, nil, err
		}
		return decline(confirmationDeclineBudgetExhausted)
	}
	if p.tokenReservation < 1 || p.maxChargeableTokens < p.tokenReservation {
		if err := deleteRequest(); err != nil {
			return nil, nil, err
		}
		return decline(confirmationDeclineUnattributed)
	}
	if grant.PromptTokens+grant.CompletionTokens >= grant.TokenBudget {
		if err := markExhausted(); err != nil {
			return nil, nil, err
		}
		if err := deleteRequest(); err != nil {
			return nil, nil, err
		}
		return decline(confirmationDeclineTokenBudgetExhausted)
	}
	// Python chained comparison: cost >= budget AND budget > 0.
	if grant.CostMicrousd >= grant.CostBudgetMicrousd && grant.CostBudgetMicrousd > 0 {
		if err := markExhausted(); err != nil {
			return nil, nil, err
		}
		if err := deleteRequest(); err != nil {
			return nil, nil, err
		}
		return decline(confirmationDeclineCostBudgetExhausted)
	}
	if err := q.IncrementConfirmationGrantAdmission(ctx, postgres.IncrementConfirmationGrantAdmissionParams{
		Now:     pgTime(p.now),
		GrantID: pgUUID(p.grantID),
	}); err != nil {
		return nil, nil, err
	}
	grant.RequestCount++
	grant.ActiveRequests++
	grant.UpdatedAt = pgTime(p.now)
	return &beginConfirmationResult{grant: grant, request: request}, nil, nil
}

// finishConfirmationParams are the inputs of
// finish_confirmation_inference_request.
type finishConfirmationParams struct {
	grantID          uuid.UUID
	nonce            uuid.UUID
	generation       int64
	status           string // "completed" | "failed"
	promptTokens     int64
	completionTokens int64
	costMicrousd     int64
	upstreamProvider pgtype.Text
	now              time.Time
}

// finishConfirmationInferenceRequest reproduces ditto/db/queries/
// confirmation_inference.py::finish_confirmation_inference_request inside the
// caller's (settle) transaction. Lock order: grant FOR UPDATE -> request FOR
// UPDATE (no ticket lock on this path). Returns delivered.
func finishConfirmationInferenceRequest(ctx context.Context, q *postgres.Queries, p finishConfirmationParams) (bool, error) {
	grant, err := q.GetConfirmationInferenceGrantForUpdate(ctx, pgUUID(p.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	request, err := q.GetConfirmationInferenceRequestForUpdate(ctx, postgres.GetConfirmationInferenceRequestForUpdateParams{
		GrantID: pgUUID(p.grantID),
		Nonce:   pgUUID(p.nonce),
	})
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return false, nil
		}
		return false, err
	}
	if request.Status != "started" || int64(request.Generation) != p.generation {
		return false, nil
	}
	providerMatches := !p.upstreamProvider.Valid ||
		(grant.Lane != "judge" && len(p.upstreamProvider.String) >= 1 && len(p.upstreamProvider.String) <= 120) ||
		p.upstreamProvider.String == grant.ReceiptProvider
	costFits := grant.CostMicrousd+p.costMicrousd <= grant.CostBudgetMicrousd
	// Overflow-safe form of prompt+completion <= max_chargeable (Python
	// compares with arbitrary-precision ints).
	withinCeiling := p.promptTokens >= 0 && p.completionTokens >= 0 &&
		p.promptTokens <= request.MaxChargeableTokens-p.completionTokens
	usageValid := withinCeiling && p.costMicrousd >= 0 && providerMatches && costFits
	delivered := p.status == "completed" && usageValid

	promptTokens := p.promptTokens
	completionTokens := p.completionTokens
	costMicrousd := p.costMicrousd
	status := p.status
	if !delivered {
		promptTokens = 0
		completionTokens = 0
		costMicrousd = 0
		status = "failed"
	}
	if err := q.SettleConfirmationInferenceRequest(ctx, postgres.SettleConfirmationInferenceRequestParams{
		Status:           status,
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		CostMicrousd:     costMicrousd,
		UpstreamProvider: p.upstreamProvider,
		CompletedAt:      pgTime(p.now),
		GrantID:          pgUUID(p.grantID),
		Nonce:            pgUUID(p.nonce),
	}); err != nil {
		return false, err
	}
	updated, err := q.ApplyConfirmationGrantSettlement(ctx, postgres.ApplyConfirmationGrantSettlementParams{
		ActiveRequests:   max32(0, grant.ActiveRequests-1),
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		CostMicrousd:     costMicrousd,
		Now:              pgTime(p.now),
		GrantID:          pgUUID(p.grantID),
	})
	if err != nil {
		return false, err
	}
	if int64(updated.RequestCount) >= int64(updated.RequestBudget) ||
		updated.PromptTokens+updated.CompletionTokens >= updated.TokenBudget ||
		(updated.CostBudgetMicrousd > 0 && updated.CostMicrousd >= updated.CostBudgetMicrousd) {
		if err := q.MarkConfirmationInferenceGrantExhausted(ctx, postgres.MarkConfirmationInferenceGrantExhaustedParams{
			Now:     pgTime(p.now),
			GrantID: pgUUID(p.grantID),
		}); err != nil {
			return false, err
		}
	}
	return delivered, nil
}

// settleConfirmation runs the confirmation transaction B. It must run exactly
// once for every admitted request, even when the client disconnected (the
// context is detached from the request) or the provider path failed.
func (d *Deps) settleConfirmation(parent context.Context, p finishConfirmationParams) (bool, error) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 30*time.Second)
	defer cancel()
	p.now = d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	delivered, err := finishConfirmationInferenceRequest(ctx, d.Queries.WithTx(tx), p)
	if err != nil {
		return false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return delivered, nil
}

// roundHalfEven mirrors Python round(): banker's rounding to an int.
func roundHalfEven(v float64) int64 { return int64(math.RoundToEven(v)) }

// pythonFloatRepr renders a float the way Python's repr (and therefore
// json.dumps) does: shortest round-trip digits, fixed notation for scientific
// exponents in [-4, 16), ".0" appended to integral fixed forms, exponential
// otherwise with a sign and at least two exponent digits.
func pythonFloatRepr(v float64) string {
	scientific := strconv.FormatFloat(v, 'e', -1, 64)
	mantissa, expPart, _ := strings.Cut(scientific, "e")
	exp, err := strconv.Atoi(expPart)
	if err == nil && (exp >= 16 || exp < -4) {
		return mantissa + "e" + expPart
	}
	fixed := strconv.FormatFloat(v, 'f', -1, 64)
	if !strings.Contains(fixed, ".") {
		fixed += ".0"
	}
	return fixed
}

// confirmationChatUsage is publicChatUsage plus the trusted "cost" key the
// Python endpoint appends after total_tokens (USD, Python float repr).
type confirmationChatUsage struct {
	PromptTokens     int64           `json:"prompt_tokens"`
	CompletionTokens int64           `json:"completion_tokens"`
	TotalTokens      int64           `json:"total_tokens"`
	Cost             json.RawMessage `json:"cost"`
}

// confirmationChatResponse is publicChatResponse plus the "provider" key the
// Python endpoint appends after usage.
type confirmationChatResponse struct {
	ID       string                `json:"id"`
	Object   string                `json:"object"`
	Created  json.RawMessage       `json:"created"`
	Model    string                `json:"model"`
	Choices  []publicChatChoice    `json:"choices"`
	Usage    confirmationChatUsage `json:"usage"`
	Provider string                `json:"provider"`
}

// confirmationOutcome carries what the confirmation settle needs.
type confirmationOutcome struct {
	status           string // "completed" | "failed"
	promptTokens     int64
	completionTokens int64
	costMicrousd     int64
	upstreamProvider pgtype.Text
}

// handleConfirmationChatCompletions is POST /api/v1/inference/confirmation/
// chat/completions: one reader or judge call under its ticket-purpose
// capability (PR #699). No route observation, no recovery phases — the route
// is frozen by the grant.
func (d *Deps) handleConfirmationChatCompletions(w http.ResponseWriter, r *http.Request) {
	headers, ok := parseProxyHeaders(r)
	if !ok {
		relayhttp.WriteValidationError(w, r)
		return
	}
	cfg := d.Cfg.Inference
	if !cfg.Enabled || cfg.OpenRouterAPIKey == "" {
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "confirmation proxy is disabled", nil)
		return
	}
	bearer, herr := d.checkConfirmationHeaders(headers)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, cfg.RequestBodyBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if int64(len(body)) > cfg.RequestBodyBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "confirmation request is too large", nil)
		return
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil || dec.More() {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid JSON request", nil)
		return
	}

	// Transaction A: proof + admission. Declines and every pre-admission
	// failure ROLL BACK. The context is DETACHED from client cancellation for
	// the same reasons as the ordinary lane (see handleChatCompletions).
	ctx := context.WithoutCancel(r.Context())
	now := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("confirmation chat: begin admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen := true
	defer func() {
		if admissionOpen {
			_ = tx.Rollback(ctx)
		}
	}()
	q := d.Queries.WithTx(tx)

	grantSnapshot, err := q.GetConfirmationInferenceGrant(ctx, pgUUID(headers.grant))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		d.Logger.Error("confirmation chat: grant snapshot", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err != nil || (grantSnapshot.Lane != "reader" && grantSnapshot.Lane != "judge") {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid confirmation proof", nil)
		return
	}
	if herr := verifyConfirmationProof(&grantSnapshot, headers, body); herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	upstreamPayload, maxTokens, herr := lockedConfirmationChatPayload(decoded, &grantSnapshot, cfg.MaxOutputTokens)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	result, decline, err := beginConfirmationInferenceRequest(ctx, q, beginConfirmationParams{
		grantID:             headers.grant,
		nonce:               headers.nonce,
		bearer:              bearer,
		model:               grantSnapshot.Model,
		tokenReservation:    int64(maxTokens) + estimatedTokens(body),
		maxChargeableTokens: maxChargeableTokens(body, int64(maxTokens)),
		now:                 now,
	})
	if err != nil {
		d.Logger.Error("confirmation chat: admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if decline != nil {
		// The decline rolls the admission transaction back (deferred
		// rollback), exactly like the Python raise inside session.begin().
		d.traceDeclined(r, headers, traces.LaneConfirmation, traces.KindChat, body, now,
			traceConfirmationGrant(&grantSnapshot), string(*decline))
		relayhttp.WriteHTTPError(w, r, http.StatusTooManyRequests,
			"confirmation inference declined: "+string(*decline), nil)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("confirmation chat: commit admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen = false

	generation := int64(result.grant.Generation)
	expectedProvider := result.grant.ReceiptProvider
	expectedModel := result.grant.Model

	outcome := &confirmationOutcome{status: "failed"}
	settled := false
	var deliverable bool
	var settleErr error
	settle := func() {
		if settled {
			return
		}
		settled = true
		deliverable, settleErr = d.settleConfirmation(ctx, finishConfirmationParams{
			grantID:          headers.grant,
			nonce:            headers.nonce,
			generation:       generation,
			status:           outcome.status,
			promptTokens:     outcome.promptTokens,
			completionTokens: outcome.completionTokens,
			costMicrousd:     max64(0, outcome.costMicrousd),
			upstreamProvider: outcome.upstreamProvider,
		})
		if settleErr != nil {
			d.Logger.Error("confirmation chat: settle", slog.String("error", settleErr.Error()))
		}
	}
	// The settle must run even on a panic in the provider path (the Python
	// finally). The deferred copy is a no-op when the explicit call ran.
	defer settle()

	var raw []byte
	var upstreamRes *providerHTTPResult
	var upstreamErr *providerCallError
	upstreamStarted := time.Now()
	providerFailure := func() *httpError {
		// Retry explicit provider backpressure in place. Every attempt keeps the
		// same frozen provider route and request payload, so this does not widen
		// the capability or permit a fallback provider. The shared retry loop is
		// bounded to seven attempts / 80 seconds for receipt-free reader 429s.
		// The judge and every other status retain the pre-existing three-attempt
		// behavior; ambiguous reader responses always fail closed.
		backpressureMaxAttempts := providerMaxAttempts
		var maxElapsed time.Duration
		if grantSnapshot.Lane == "reader" {
			backpressureMaxAttempts = confirmationReaderBackpressureMaxAttempts
			maxElapsed = confirmationReaderBackpressureMaxElapsed
		}
		result, callErr := postProviderWithRetryPolicy(ctx, d.Upstream, cfg.UpstreamURL, upstreamPayload,
			openrouterHeaders(cfg.OpenRouterAPIKey, true), cfg.ResponseBodyBytes, cfg.TimeoutSeconds,
			providerRetryPolicy{
				retryBackpressure:              true,
				retryPreProviderNotFoundModel:  expectedModel,
				backpressureMaxAttempts:        backpressureMaxAttempts,
				requireReceiptFreeBackpressure: grantSnapshot.Lane == "reader",
				receiptFreeExpectedModel:       expectedModel,
				receiptFreeExpectedProvider:    result.grant.RouteProvider,
				maxElapsed:                     maxElapsed,
			}, d.sleep())
		upstreamRes, upstreamErr = result, callErr
		if callErr != nil {
			d.Logger.Warn("confirmation provider transport failed",
				slog.String("lane", grantSnapshot.Lane),
				slog.Int("upstream_attempts", callErr.attempts),
				slog.Bool("timed_out", callErr.timedOut))
			// _ProviderCallError is UNCAUGHT in the Python endpoint (only
			// ValueError is handled): it escapes as an internal server error
			// after the finally settles. Reproduced deliberately.
			return &httpError{status: 500, message: "internal server error"}
		}
		if result.status >= 400 {
			d.Logger.Warn("confirmation provider rejected request",
				slog.String("lane", grantSnapshot.Lane),
				slog.Int("upstream_status", result.status),
				slog.Int("upstream_attempts", result.attempts),
				slog.Bool("backpressure", providerIsBackpressure(result.status, result.header)))
			return httpErrorf(502, "confirmation provider unavailable")
		}
		if result.bodyOverLimit {
			return httpErrorf(502, "confirmation response is too large")
		}
		decodedResponse, decodeOk := decodeJSONNumbers(result.body)
		if !decodeOk {
			return httpErrorf(502, "invalid provider response")
		}
		decodedMap, isMap := decodedResponse.(map[string]any)
		if !isMap {
			return httpErrorf(502, "provider identity mismatch")
		}
		if m, ok := decodedMap["model"].(string); !ok || m != expectedModel {
			return httpErrorf(502, "provider identity mismatch")
		}
		receiptProvider, herr := upstreamProviderIdentity(decodedMap)
		if herr != nil {
			return herr
		}
		if receiptProvider != "" {
			outcome.upstreamProvider = textValue(receiptProvider)
		}
		prompt, completion, usageOk := boundedUsage(decodedMap)
		// Python: `_bounded_provider_cost(decoded) or -1` — an invalid cost
		// AND a zero cost both collapse to -1 and are refused below.
		costMicrousd := boundedProviderCost(decodedMap)
		if costMicrousd == 0 {
			costMicrousd = -1
		}
		providerMatches := receiptProvider != "" && (grantSnapshot.Lane != "judge" ||
			receiptProvider == expectedProvider)
		if !providerMatches || !usageOk || costMicrousd < 0 {
			outcome.costMicrousd = costMicrousd
			return httpErrorf(502, "provider identity mismatch")
		}
		outcome.promptTokens = prompt
		outcome.completionTokens = completion
		outcome.costMicrousd = costMicrousd
		trusted, herr := publicProviderResponse(decodedMap, result.body)
		if herr != nil {
			return herr
		}
		encoded, encodeErr := compactJSON(confirmationChatResponse{
			ID:      trusted.ID,
			Object:  trusted.Object,
			Created: trusted.Created,
			Model:   trusted.Model,
			Choices: trusted.Choices,
			Usage: confirmationChatUsage{
				PromptTokens:     trusted.Usage.PromptTokens,
				CompletionTokens: trusted.Usage.CompletionTokens,
				TotalTokens:      trusted.Usage.TotalTokens,
				Cost:             json.RawMessage(pythonFloatRepr(float64(costMicrousd) / 1_000_000)),
			},
			Provider: receiptProvider,
		})
		if encodeErr != nil {
			return httpErrorf(502, "invalid provider response")
		}
		raw = encoded
		outcome.status = "completed"
		return nil
	}()
	settle()
	d.traceConfirmationSettled(confirmationTrace{
		r: r, headers: headers, kind: traces.KindChat, body: body, receivedAt: now, grant: &result.grant,
		payload: upstreamPayload, outcome: outcome, result: upstreamRes, route: "openrouter", callErr: upstreamErr,
		raw: raw, started: upstreamStarted, finished: time.Now(), deliverable: deliverable,
		failure: providerFailure, settleErr: settleErr,
		reserved: result.request.ReservedTokens, chargeable: result.request.MaxChargeableTokens, admittedAt: now,
	})
	if settleErr != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if providerFailure != nil {
		if providerFailure.status == 500 {
			relayhttp.WriteInternalError(w, r)
			return
		}
		writeHTTPError(w, r, providerFailure)
		return
	}
	if !deliverable || raw == nil {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "confirmation grant is no longer live", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

// handleConfirmationEmbeddings is POST /api/v1/inference/confirmation/
// embeddings: the frozen LongMem embedding space under a separate capability.
func (d *Deps) handleConfirmationEmbeddings(w http.ResponseWriter, r *http.Request) {
	headers, ok := parseProxyHeaders(r)
	if !ok {
		relayhttp.WriteValidationError(w, r)
		return
	}
	cfg := d.Cfg.Inference
	if !cfg.Enabled || cfg.OpenRouterAPIKey == "" {
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "confirmation proxy is disabled", nil)
		return
	}
	bearer, herr := d.checkConfirmationHeaders(headers)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, cfg.EmbeddingRequestBodyBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if int64(len(body)) > cfg.EmbeddingRequestBodyBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "embedding request is too large", nil)
		return
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil || dec.More() {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid JSON request", nil)
		return
	}
	inputs, herr := d.validatedEmbeddingPayload(decoded, cfg.EmbeddingModel, cfg.EmbeddingDimensions)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}

	ctx := context.WithoutCancel(r.Context())
	now := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("confirmation embeddings: begin admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen := true
	defer func() {
		if admissionOpen {
			_ = tx.Rollback(ctx)
		}
	}()
	q := d.Queries.WithTx(tx)

	grantSnapshot, err := q.GetConfirmationInferenceGrant(ctx, pgUUID(headers.grant))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		d.Logger.Error("confirmation embeddings: grant snapshot", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err != nil ||
		grantSnapshot.Lane != "embedding" ||
		grantSnapshot.Model != cfg.EmbeddingModel ||
		!strings.EqualFold(grantSnapshot.Provider, cfg.EmbeddingProvider) {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid confirmation proof", nil)
		return
	}
	if herr := verifyConfirmationProof(&grantSnapshot, headers, body); herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	result, decline, err := beginConfirmationInferenceRequest(ctx, q, beginConfirmationParams{
		grantID:             headers.grant,
		nonce:               headers.nonce,
		bearer:              bearer,
		model:               grantSnapshot.Model,
		tokenReservation:    estimatedTokens(body),
		maxChargeableTokens: maxChargeableTokens(body, 0),
		now:                 now,
	})
	if err != nil {
		d.Logger.Error("confirmation embeddings: admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if decline != nil {
		d.traceDeclined(r, headers, traces.LaneConfirmation, traces.KindEmbedding, body, now,
			traceConfirmationGrant(&grantSnapshot), string(*decline))
		relayhttp.WriteHTTPError(w, r, http.StatusTooManyRequests,
			"confirmation embedding declined: "+string(*decline), nil)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("confirmation embeddings: commit admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen = false

	generation := int64(result.grant.Generation)
	// The embedding settle attributes the frozen receipt provider whether the
	// call succeeded or not (the Python endpoint passes expected_provider
	// unconditionally).
	outcome := &confirmationOutcome{
		status:           "failed",
		upstreamProvider: textValue(result.grant.ReceiptProvider),
	}
	settled := false
	var deliverable bool
	var settleErr error
	settle := func() {
		if settled {
			return
		}
		settled = true
		// Catalog price: $0.004 / 1M input tokens, banker's rounding;
		// provider-reported cost is never trusted.
		deliverable, settleErr = d.settleConfirmation(ctx, finishConfirmationParams{
			grantID:          headers.grant,
			nonce:            headers.nonce,
			generation:       generation,
			status:           outcome.status,
			promptTokens:     outcome.promptTokens,
			completionTokens: 0,
			costMicrousd:     roundHalfEven(float64(outcome.promptTokens) * 0.004),
			upstreamProvider: outcome.upstreamProvider,
		})
		if settleErr != nil {
			d.Logger.Error("confirmation embeddings: settle", slog.String("error", settleErr.Error()))
		}
	}
	defer settle()

	var raw []byte
	var embRes *embeddingProviderResult
	var embErr *providerCallError
	upstreamStarted := time.Now()
	providerFailure := func() *httpError {
		providerResult, callErr := postEmbeddingProvider(ctx, d.Upstream, cfg, inputs, d.sleep())
		embRes, embErr = providerResult, callErr
		if callErr != nil {
			// Uncaught _ProviderCallError in the Python endpoint (only
			// ValueError is handled): internal server error after the settle.
			return &httpError{status: 500, message: "internal server error"}
		}
		upstream := providerResult.result
		if upstream.status >= 400 {
			return httpErrorf(502, "embedding provider unavailable")
		}
		// The Python route has no response-size gate on this lane; the Go
		// client still buffers at most EmbeddingResponseBodyBytes, so an
		// over-limit body fails JSON decoding exactly like a torn response.
		decodedResponse, decodeOk := decodeJSONNumbers(upstream.body)
		if !decodeOk || upstream.bodyOverLimit {
			return httpErrorf(502, "invalid provider response")
		}
		if providerResult.direct {
			converted, herr := perplexityEmbeddingResponse(decodedResponse)
			if herr != nil {
				return herr
			}
			decodedResponse = converted
		}
		public, promptTokens, herr := publicEmbeddingResponseFrom(decodedResponse,
			cfg.EmbeddingModel, cfg.EmbeddingDimensions, len(inputs))
		if herr != nil {
			return herr
		}
		encoded, encodeErr := compactJSON(public)
		if encodeErr != nil {
			return httpErrorf(502, "invalid provider response")
		}
		outcome.promptTokens = promptTokens
		raw = encoded
		outcome.status = "completed"
		return nil
	}()
	settle()
	{
		var upstream *providerHTTPResult
		route := "openrouter"
		if embRes != nil {
			upstream = embRes.result
			if embRes.direct {
				route = "direct"
			}
		}
		d.traceConfirmationSettled(confirmationTrace{
			r: r, headers: headers, kind: traces.KindEmbedding, body: body, receivedAt: now, grant: &result.grant,
			payload: map[string]any{"model": cfg.EmbeddingModel, "input": inputs}, outcome: outcome,
			result: upstream, route: route, callErr: embErr, raw: raw, started: upstreamStarted, finished: time.Now(),
			deliverable: deliverable, failure: providerFailure, settleErr: settleErr,
			reserved: result.request.ReservedTokens, chargeable: result.request.MaxChargeableTokens, admittedAt: now,
		})
	}
	if settleErr != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if providerFailure != nil {
		if providerFailure.status == 500 {
			relayhttp.WriteInternalError(w, r)
			return
		}
		writeHTTPError(w, r, providerFailure)
		return
	}
	if !deliverable || raw == nil {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "confirmation grant is no longer live", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}
