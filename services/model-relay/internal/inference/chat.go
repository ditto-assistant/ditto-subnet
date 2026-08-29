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
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// lockedGrantModel mirrors _locked_grant_model: the model is a property of
// the ticket, never of the request.
func (d *Deps) lockedGrantModel(grant *postgres.InferenceGrant, requested string) (string, *httpError) {
	if grant.BenchVersion < 7 {
		for _, m := range d.Cfg.Inference.AllowedModels {
			if m == requested {
				return requested, nil
			}
		}
		return "", httpErrorf(403, "model is not permitted")
	}
	models := allowedModels(grant)
	if len(models) == 0 {
		return "", httpErrorf(409, "inference route unavailable")
	}
	locked := models[0]
	if requested != locked {
		d.Logger.Warn("v7 inference model mismatch — serving the locked model",
			slog.String("grant_id", uuid.UUID(grant.GrantID.Bytes).String()),
			slog.String("requested", requested),
			slog.String("locked", locked))
	}
	return locked, nil
}

// verifyProxyProof runs the Ed25519 verification of the six-header proof
// against the grant's stored broker key. Any failure is the uniform 401
// "invalid inference proof".
func verifyProxyProof(grant *postgres.InferenceGrant, h *proxyHeaders, body []byte) bool {
	if !grant.BrokerPublicKey.Valid {
		return false
	}
	key, err := decodeBrokerPublicKey(grant.BrokerPublicKey.String)
	if err != nil {
		return false
	}
	proof, err := decodeProof(h.proof)
	if err != nil {
		return false
	}
	message := proxyMessage(h.grant, h.generation, h.nonce, h.requestedAt, body)
	return ed25519.Verify(key, message, proof)
}

// settleOutcome carries everything transaction B (finish + optional route
// observation) needs.
type settleOutcome struct {
	status             string // "completed" | "failed"
	promptTokens       int64
	completionTokens   int64
	costMicrousd       int64
	usageAvailable     bool
	upstreamProvider   pgtype.Text
	timedOut           bool
	elapsed            time.Duration
	upstreamAttempts   int
	openrouterAttempts int
	fallbackPhase      int
	terminalErrorCode  pgtype.Text
	routeObservable    bool
	recordObservation  bool // chat only; embeddings never observe routes
}

// settleRequest runs transaction B. It must run exactly once for every
// admitted request, even when the client disconnected (the context is
// detached from the request).
func (d *Deps) settleRequest(parent context.Context, h *proxyHeaders, o *settleOutcome) (bool, error) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 30*time.Second)
	defer cancel()
	finishedAt := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := d.Queries.WithTx(tx)
	latencyMs := int32(math.Round(o.elapsed.Seconds() * 1000))
	if latencyMs < 0 {
		latencyMs = 0
	}
	deliverable, err := finishInferenceRequest(ctx, q, finishParams{
		grantID:            h.grant,
		nonce:              h.nonce,
		generation:         h.generation,
		status:             o.status,
		promptTokens:       o.promptTokens,
		completionTokens:   o.completionTokens,
		costMicrousd:       o.costMicrousd,
		usageAvailable:     o.usageAvailable,
		now:                finishedAt,
		upstreamProvider:   o.upstreamProvider,
		timedOut:           o.timedOut,
		latencyMs:          latencyMs,
		upstreamAttempts:   int32(clampInt(o.upstreamAttempts, 0, 1<<30)),
		openrouterAttempts: int32(clampInt(o.openrouterAttempts, 0, 1<<30)),
		fallbackPhase:      int32(o.fallbackPhase),
		terminalErrorCode:  o.terminalErrorCode,
	})
	if err != nil {
		return false, err
	}
	if o.recordObservation && o.routeObservable {
		observed, gerr := q.GetInferenceGrant(ctx, pgUUID(h.grant))
		if gerr != nil && !isNoRows(gerr) {
			return false, gerr
		}
		if gerr == nil {
			success := o.status == "completed" && o.usageAvailable
			completion := int64(0)
			cost := int64(0)
			if o.usageAvailable {
				completion = o.completionTokens
				cost = o.costMicrousd
			}
			if oerr := recordRouteObservation(ctx, q, &observed, success,
				o.elapsed.Seconds()*1000, completion, cost, o.timedOut, finishedAt); oerr != nil {
				return false, oerr
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return false, err
	}
	return deliverable, nil
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func isNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }

// handleChatCompletions is POST /api/v1/inference/chat/completions: exactly
// one NON-streaming chat completion proxied to OpenRouter under atomic
// ticket budgets. There is no SSE/chunked pass-through anywhere; stream:true
// is refused legibly.
func (d *Deps) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	headers, ok := parseProxyHeaders(r)
	if !ok {
		relayhttp.WriteValidationError(w, r)
		return
	}
	cfg := d.Cfg.Inference
	if !cfg.Enabled || cfg.OpenRouterAPIKey == "" {
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "inference proxy is disabled", nil)
		return
	}
	if headers.anyMissing() {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "missing inference proof", nil)
		return
	}
	if !strings.HasPrefix(headers.authorization, "Bearer ") {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid inference proof", nil)
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, cfg.RequestBodyBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if int64(len(body)) > cfg.RequestBodyBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "inference request is too large", nil)
		return
	}
	if absDuration(d.now().Sub(headers.requestedAt)) > proxyMaxAge {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference request is stale", nil)
		return
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil || dec.More() {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid JSON request", nil)
		return
	}
	payload, isObject := decoded.(map[string]any)
	if !isObject {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "inference request must be a JSON object", nil)
		return
	}
	if herr := validateRequestSchema(payload); herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	requestedModel, isString := payload["model"].(string)
	if !isString {
		relayhttp.WriteHTTPError(w, r, http.StatusForbidden, "model is not permitted", nil)
		return
	}
	maxTokens, herr := outputTokenLimit(payload, cfg.MaxOutputTokens)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	// Both hosted lanes use the same background-refreshed Backroom policy.
	// Resolve is an in-memory atomic load; it performs no SQL on this path.
	admissionCfg := applySettings(cfg, d.Settings.Resolve())

	// Transaction A: admission. Declines and every pre-admission failure
	// ROLL BACK (nothing they wrote survives).
	//
	// The context is DETACHED from client cancellation (values, including the
	// request id, are kept). The Python relay never observes a client
	// disconnect: uvicorn does not cancel the endpoint task, so the upstream
	// call completes and settles REAL usage. Threading r.Context() here would
	// turn every broker abort into context.Canceled inside postOnce, which
	// classifyTransportError reads as a provider transport failure — charging
	// the full reservation (usage_available=false) AND recording a FAILED
	// observation that cools down the shared route fleet-wide. Each provider
	// attempt still carries its own bounded timeout (postOnce).
	ctx := context.WithoutCancel(r.Context())
	now := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("chat: begin admission", slog.String("error", err.Error()))
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

	grantSnapshot, err := q.GetInferenceGrant(ctx, pgUUID(headers.grant))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		d.Logger.Error("chat: grant snapshot", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err != nil || !grantSnapshot.BrokerPublicKey.Valid || int64(grantSnapshot.Generation) != headers.generation {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid inference proof", nil)
		return
	}
	if !verifyProxyProof(&grantSnapshot, headers, body) {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid inference proof", nil)
		return
	}
	model, herr := d.lockedGrantModel(&grantSnapshot, requestedModel)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	// V9 reasoning is validated BEFORE reserving capacity, so invalid or
	// conflicting aliases fail without spending the grant.
	if _, herr := benchmarkReasoningForRequest(payload, model, grantSnapshot.BenchVersion); herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	result, decline, err := beginInferenceRequest(ctx, tx, q, beginParams{
		grantID:             headers.grant,
		nonce:               headers.nonce,
		bearer:              strings.TrimPrefix(headers.authorization, "Bearer "),
		model:               model,
		tokenReservation:    int64(maxTokens) + estimatedTokens(body),
		maxChargeableTokens: maxChargeableTokens(body, int64(maxTokens)),
		now:                 now,
		kind:                kindChat,
		timeoutSeconds:      admissionCfg.TimeoutSeconds,
		limits:              limitsForKind(admissionCfg, kindChat),
	})
	if err != nil {
		d.Logger.Error("chat: admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if decline != nil {
		// The decline rolls the admission transaction back (deferred
		// rollback): stale reclamation and status writes are discarded.
		d.traceDeclined(r, headers, traces.LaneInference, traces.KindChat, body, now,
			traceInferenceGrant(&grantSnapshot, model), decline.String())
		relayhttp.WriteDecline(w, r, *decline, relayhttp.LaneInference)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("chat: commit admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen = false

	reservedGrant := result.grant
	upstreamPayload, herr := lockedUpstreamPayload(payload, model, maxTokens, reservedGrant.BenchVersion)
	if herr != nil {
		// Unreachable (reasoning already validated), kept for parity with
		// the Python call order.
		writeHTTPError(w, r, herr)
		return
	}
	attachPlatformMiddleOut(upstreamPayload, len(body))
	if !reservedGrant.RouteProvider.Valid || reservedGrant.RouteProvider.String == "" {
		// KNOWN PYTHON QUIRK reproduced deliberately: this path leaks the
		// just-made reservation until the 2x-timeout stale sweep reclaims
		// it — no finish call here.
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference route unavailable", nil)
		return
	}

	outcome := &settleOutcome{
		status:            "failed",
		recordObservation: true,
	}
	var promptPrice, completionPrice *float64
	if reservedGrant.RoutePromptPricePerToken.Valid {
		promptPrice = &reservedGrant.RoutePromptPricePerToken.Float64
	}
	if reservedGrant.RouteCompletionPricePerToken.Valid {
		completionPrice = &reservedGrant.RouteCompletionPricePerToken.Float64
	}
	quantization := ""
	if reservedGrant.RouteQuantization.Valid {
		quantization = reservedGrant.RouteQuantization.String
	}

	started := time.Now()
	settled := false
	var deliverable bool
	var settleErr error
	settle := func() {
		if settled {
			return
		}
		settled = true
		outcome.elapsed = time.Since(started)
		deliverable, settleErr = d.settleRequest(ctx, headers, outcome)
		if settleErr != nil {
			d.Logger.Error("chat: settle", slog.String("error", settleErr.Error()))
		}
	}
	// The settle must run even on a panic in the provider path (the Python
	// finally). The deferred copy is a no-op when the explicit call ran.
	defer settle()

	recovered, exhausted := completeChatWithRecovery(ctx, d.Upstream, cfg, upstreamPayload, model,
		reservedGrant.RouteProvider.String, quantization, promptPrice, completionPrice, d.sleep())
	if exhausted != nil {
		if status, overloaded := receiptFreeOverload(
			exhausted.phases, model, reservedGrant.RouteProvider.String,
		); overloaded {
			_ = d.openProviderCircuit(ctx, now, status, exhausted.terminalErrorCode)
		}
	} else {
		_ = d.closeProviderCircuit(ctx, now)
	}
	var raw []byte
	var providerFailure *httpError
	if exhausted != nil {
		outcome.upstreamAttempts = exhausted.upstreamAttempts
		outcome.openrouterAttempts = exhausted.openrouterAttempts
		outcome.fallbackPhase = exhausted.fallbackPhase
		outcome.terminalErrorCode = textValue(exhausted.terminalErrorCode)
		outcome.timedOut = exhausted.timedOut
		if exhausted.upstreamProvider != "" {
			outcome.upstreamProvider = textValue(exhausted.upstreamProvider)
		}
		outcome.routeObservable = exhausted.routeObservable
		providerFailure = chatProviderFailure(exhausted)
	} else {
		raw = recovered.raw
		outcome.status = "completed"
		outcome.usageAvailable = true
		outcome.promptTokens = recovered.promptTokens
		outcome.completionTokens = recovered.completionTokens
		outcome.costMicrousd = recovered.costMicrousd
		outcome.upstreamProvider = textValue(recovered.upstreamProvider)
		outcome.upstreamAttempts = recovered.upstreamAttempts
		outcome.openrouterAttempts = recovered.openrouterAttempts
		outcome.fallbackPhase = recovered.fallbackPhase
		outcome.routeObservable = true
	}
	settle()
	d.traceChatSettled(chatTrace{
		r: r, headers: headers, body: body, receivedAt: now, grant: &reservedGrant, model: model,
		locked: upstreamPayload, outcome: outcome, recovered: recovered, exhausted: exhausted, raw: raw,
		started: started, deliverable: deliverable, failure: providerFailure, settleErr: settleErr,
		reserved: result.request.ReservedTokens, chargeable: result.request.MaxChargeableTokens, admittedAt: now,
	})
	if settleErr != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if providerFailure != nil {
		writeHTTPError(w, r, providerFailure)
		return
	}
	if !deliverable || raw == nil {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference grant is no longer live", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func chatProviderFailure(exhausted *chatProviderExhausted) *httpError {
	if exhausted.timedOut {
		return httpErrorf(504, "inference provider timed out")
	}
	failure := httpErrorf(502, "inference provider unavailable")
	if exhausted.terminalErrorCode == structuredOutputInvalidCode {
		// Keep the public failure contract unchanged. The authenticated scorer
		// broker uses this private classification to leave response-level repair
		// to the miner instead of discarding the whole ticket.
		failure.headers = map[string]string{
			minerRecoverableFailureHeader: minerRecoverableStructuredOutput,
		}
	}
	return failure
}
