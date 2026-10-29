package inference

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/ditto-assistant/model-relay/internal/metrics"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// The dark decision-model lane: POST /api/v1/inference/decisions.
//
// The wire is OpenRouter's System One body (TypeSafe Jev's shape), forwarded
// nearly verbatim to POST https://openrouter.ai/api/v1/systemone with the
// existing OPENROUTER_API_KEY. The harness may ask whatever it wants: every
// question type the provider supports (choice, score, noul, box, ...), any
// number of questions, any state or assets. The provider's own limits apply.
// The relay's controls are deliberately few:
//
//   - the operator's decision_lane switch (off by default; off answers the
//     same 404 an unmounted path does, so a dark relay is indistinguishable
//     from one without the route);
//   - the grant-stamped model pin: "model" is forced to it, as chat forces
//     the ticket model, and a response served by any other version is an
//     infrastructure failure (502), never a delivered answer;
//   - metering: receipted usage.input_tokens x the grant-stamped list price,
//     booked as ledger cost (output is free);
//   - the lane's own request and token walls and concurrency/RPM rails,
//     reusing 4102/4104/4103/4107 with no new decline codes.
//
// A provider outage answers the same 502/503/504 shapes as the chat and
// embedding lanes, which the scorer already maps to the no-fault
// model_relay_unavailable. The lane carries no score.

// decisionRefusedFields are top-level System One fields the relay owns:
// OpenRouter routing (provider) and our account's observability grouping
// (session_id, user, trace). Everything else passes through.
var decisionRefusedFields = map[string]bool{
	"provider":   true,
	"session_id": true,
	"user":       true,
	"trace":      true,
}

// decisionTemplateTokens is the per-request and per-question headroom over
// the body size when bounding untrusted provider usage: Jev wraps every
// question in its own prompt template, so input_tokens can exceed the body
// byte count on small requests.
const (
	decisionTemplateTokens    = 1024
	decisionPerQuestionTokens = 256
)

// lockedDecisionPayload validates the thin System One envelope and returns
// the payload to send upstream with the model forced to the pin.
func lockedDecisionPayload(decoded any, pinnedModel string) (map[string]any, int, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return nil, 0, httpErrorf(400, "decision request must be a JSON object")
	}
	for key := range payload {
		if decisionRefusedFields[key] {
			return nil, 0, httpErrorf(400, "decision request field %q is set by the relay", key)
		}
	}
	if _, ok := payload["state"]; !ok {
		return nil, 0, httpErrorf(400, "decision request requires state")
	}
	questions, ok := payload["questions"].(map[string]any)
	if !ok || len(questions) == 0 {
		return nil, 0, httpErrorf(400, "decision request requires a non-empty questions object")
	}
	for _, raw := range questions {
		question, ok := raw.(map[string]any)
		if !ok {
			return nil, 0, httpErrorf(400, "each decision question must be an object")
		}
		if kind, ok := question["type"].(string); !ok || kind == "" {
			return nil, 0, httpErrorf(400, "each decision question requires a type")
		}
	}
	locked := make(map[string]any, len(payload)+1)
	for key, value := range payload {
		locked[key] = value
	}
	locked["model"] = pinnedModel
	return locked, len(questions), nil
}

// servedModelMatchesPin accepts the pinned id itself or a dated snapshot of
// it (OpenRouter answers typesafe/jev-1.13 with typesafe/jev-1.13-20260917).
// Anything else, including a different minor version, is a mismatch.
func servedModelMatchesPin(served, pin string) bool {
	if served == pin {
		return true
	}
	suffix, ok := strings.CutPrefix(served, pin+"-")
	if !ok || suffix == "" {
		return false
	}
	for _, c := range suffix {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// decisionLedgerCost is receipted input tokens x the stamped price in
// micro-USD per million input tokens, banker's rounded.
func decisionLedgerCost(inputTokens, microusdPerMtok int64) int64 {
	return int64(math.RoundToEven(float64(inputTokens) * float64(microusdPerMtok) / 1_000_000))
}

// publicDecisionResponse checks the provider answer and returns the public
// body {model, answers, usage{input_tokens, output_tokens}} plus input
// tokens. answers is passed through verbatim.
func publicDecisionResponse(decoded any, pin string) ([]byte, int64, string, *httpError) {
	body, ok := decoded.(map[string]any)
	if !ok {
		return nil, 0, "invalid_provider_response", httpErrorf(502, "invalid provider response")
	}
	served, _ := body["model"].(string)
	if !servedModelMatchesPin(served, pin) {
		return nil, 0, "decision_model_mismatch", httpErrorf(502, "decision provider served an unpinned model")
	}
	answers, ok := body["answers"].(map[string]any)
	if !ok {
		return nil, 0, "invalid_provider_response", httpErrorf(502, "invalid provider response")
	}
	usage, _ := body["usage"].(map[string]any)
	inputTokens, ok := nonNegativeInt(usage["input_tokens"])
	if !ok {
		return nil, 0, "invalid_provider_response", httpErrorf(502, "invalid provider response")
	}
	outputTokens, ok := nonNegativeInt(usage["output_tokens"])
	if !ok {
		outputTokens = 0
	}
	encoded, err := compactJSON(map[string]any{
		"model":   served,
		"answers": answers,
		"usage":   map[string]any{"input_tokens": inputTokens, "output_tokens": outputTokens},
	})
	if err != nil {
		return nil, 0, "invalid_provider_response", httpErrorf(502, "invalid provider response")
	}
	return encoded, inputTokens, "", nil
}

func nonNegativeInt(v any) (int64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := n.Int64()
	if err != nil || i < 0 {
		return 0, false
	}
	return i, true
}

// providerErrorMessage extracts OpenRouter's {error:{message}} for a
// miner-owned 400, bounded so a provider cannot echo a large body back.
func providerErrorMessage(body []byte) string {
	var envelope struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &envelope) != nil {
		return ""
	}
	message := strings.TrimSpace(envelope.Error.Message)
	if len(message) > 512 {
		message = message[:512]
	}
	return message
}

// handleDecisions is POST /api/v1/inference/decisions.
func (d *Deps) handleDecisions(w http.ResponseWriter, r *http.Request) {
	cfg := d.Cfg.Inference
	admissionCfg := applySettings(cfg, d.Settings.Resolve())
	if !cfg.Enabled || cfg.OpenRouterAPIKey == "" || !admissionCfg.DecisionEnabled {
		// Byte-identical to the router's answer for an unmounted path.
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "Not Found", nil)
		return
	}
	headers, ok := parseProxyHeaders(r)
	if !ok {
		relayhttp.WriteValidationError(w, r)
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
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "decision request is too large", nil)
		return
	}
	if absDuration(d.now().Sub(headers.requestedAt)) > proxyMaxAge {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "decision request is stale", nil)
		return
	}
	decoded, ok := decodeJSONNumbers(body)
	if !ok {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid JSON request", nil)
		return
	}

	// Detached from client cancellation, as on the other lanes: an abort must
	// not cancel the provider call mid-flight and skip settlement.
	ctx := context.WithoutCancel(r.Context())
	now := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("decisions: begin admission", slog.String("error", err.Error()))
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
		d.Logger.Error("decisions: grant snapshot", slog.String("error", err.Error()))
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
	// A grant minted with the lane off carries no pin; admission answers it
	// 4107 after authentication, like any other unpermitted model.
	pin := grantSnapshot.DecisionModel.String
	payload, questionCount, herr := lockedDecisionPayload(decoded, pin)
	if herr != nil {
		writeHTTPError(w, r, herr)
		return
	}
	result, decline, err := beginInferenceRequest(ctx, tx, q, beginParams{
		grantID:             headers.grant,
		nonce:               headers.nonce,
		bearer:              strings.TrimPrefix(headers.authorization, "Bearer "),
		model:               pin,
		tokenReservation:    estimatedTokens(body),
		maxChargeableTokens: maxChargeableTokens(body, decisionTemplateTokens+int64(questionCount)*decisionPerQuestionTokens),
		now:                 now,
		kind:                kindDecision,
		timeoutSeconds:      admissionCfg.TimeoutSeconds,
		limits:              limitsForKind(admissionCfg, kindDecision),
	})
	if err != nil {
		d.Logger.Error("decisions: admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if decline != nil {
		metrics.DecisionRequests.WithLabelValues("declined_" + decline.String()).Inc()
		d.traceDeclined(r, headers, traces.LaneInference, traces.KindDecision, body, now,
			traceInferenceGrant(&grantSnapshot, pin), decline.String())
		relayhttp.WriteDecline(w, r, *decline, relayhttp.LaneDecision)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("decisions: commit admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen = false

	outcome := &settleOutcome{
		status:            "failed",
		upstreamProvider:  textValue("openrouter"),
		recordObservation: false, // never observe chat routes from this lane
	}
	price := result.grant.DecisionInputMicrousdPerMtok
	started := time.Now()
	var deliverable bool
	var settleErr error
	settled := false
	settle := func() {
		if settled {
			return
		}
		settled = true
		outcome.elapsed = time.Since(started)
		// Output is free; cost is ledger cost from the stamped price and the
		// receipted input tokens only. The provider's own cost is not trusted.
		outcome.completionTokens = 0
		outcome.costMicrousd = decisionLedgerCost(outcome.promptTokens, price)
		outcome.usageAvailable = outcome.status == "completed"
		deliverable, settleErr = d.settleRequest(ctx, headers, outcome)
		if settleErr != nil {
			d.Logger.Error("decisions: settle", slog.String("error", settleErr.Error()))
		}
	}
	defer settle()

	var raw []byte
	var requestFailure *httpError
	upstream, callErr := postProviderWithRetry(ctx, d.Upstream, cfg.DecisionUpstreamURL, payload,
		openrouterHeaders(cfg.OpenRouterAPIKey, false), cfg.ResponseBodyBytes, cfg.TimeoutSeconds, false, "", d.sleep())
	if callErr != nil {
		outcome.upstreamAttempts = callErr.attempts
		outcome.timedOut = callErr.timedOut
		if callErr.timedOut {
			outcome.terminalErrorCode = textValue("decision_provider_timeout")
			requestFailure = httpErrorf(504, "decision provider timed out")
		} else {
			outcome.terminalErrorCode = textValue("decision_provider_transport")
			requestFailure = httpErrorf(502, "decision provider unavailable")
		}
	} else {
		outcome.upstreamAttempts = upstream.attempts
		requestFailure = func() *httpError {
			switch {
			case upstream.bodyOverLimit:
				outcome.terminalErrorCode = textValue("decision_response_too_large")
				return httpErrorf(502, "decision response is too large")
			case providerIsBackpressure(upstream.status, upstream.header):
				outcome.terminalErrorCode = textValue("decision_provider_backpressure_" + strconv.Itoa(upstream.status))
				failure := httpErrorf(503, "decision provider is temporarily at capacity")
				failure.headers = map[string]string{
					"Retry-After": strconv.Itoa(providerRetryAfterSeconds(upstream.header)),
				}
				return failure
			case upstream.status == 400 || upstream.status == 422:
				// The provider refused the harness's own questions: miner-owned,
				// uncharged, and legible so the harness can repair the call.
				outcome.terminalErrorCode = textValue("decision_provider_rejected_" + strconv.Itoa(upstream.status))
				message := "decision provider rejected the request"
				if detail := providerErrorMessage(upstream.body); detail != "" {
					message += ": " + detail
				}
				return httpErrorf(400, "%s", message)
			case upstream.status >= 400:
				outcome.terminalErrorCode = textValue("decision_provider_http_" + strconv.Itoa(upstream.status))
				return httpErrorf(502, "decision provider unavailable")
			}
			decodedResponse, decodeOK := decodeJSONNumbers(upstream.body)
			if !decodeOK {
				outcome.terminalErrorCode = textValue("invalid_provider_response")
				return httpErrorf(502, "invalid provider response")
			}
			public, inputTokens, code, herr := publicDecisionResponse(decodedResponse, pin)
			if herr != nil {
				outcome.terminalErrorCode = textValue(code)
				return herr
			}
			outcome.promptTokens = inputTokens
			outcome.status = "completed"
			raw = public
			return nil
		}()
	}
	settle()
	metrics.DecisionRequests.WithLabelValues(decisionOutcomeLabel(outcome, requestFailure)).Inc()
	d.traceDecisionSettled(decisionTrace{
		r: r, headers: headers, body: body, receivedAt: now, grant: &result.grant, model: pin,
		locked: payload, outcome: outcome, upstream: upstream, callErr: callErr, raw: raw, started: started,
		deliverable: deliverable, failure: requestFailure, settleErr: settleErr,
		reserved: result.request.ReservedTokens, chargeable: result.request.MaxChargeableTokens,
	})
	if settleErr != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if requestFailure != nil {
		writeHTTPError(w, r, requestFailure)
		return
	}
	if !deliverable || raw == nil {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "decision grant is no longer live", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}

func decisionOutcomeLabel(outcome *settleOutcome, failure *httpError) string {
	if failure == nil {
		return "completed"
	}
	if outcome.terminalErrorCode.String == "decision_model_mismatch" {
		return "model_mismatch"
	}
	if failure.status == 400 {
		return "provider_rejected"
	}
	return "provider_unavailable"
}
