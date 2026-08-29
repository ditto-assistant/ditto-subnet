package inference

import (
	"bytes"
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

	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// validatedEmbeddingPayload mirrors _validated_embedding_payload: the body
// must be EXACTLY {model, input, dimensions, encoding_format} with the
// pinned identity; unlike chat, a model mismatch is REFUSED, never
// substituted.
func (d *Deps) validatedEmbeddingPayload(decoded any, model string, dimensions int) ([]string, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok || len(payload) != 4 {
		return nil, httpErrorf(400, "invalid embedding request")
	}
	for _, key := range []string{"model", "input", "dimensions", "encoding_format"} {
		if _, present := payload[key]; !present {
			return nil, httpErrorf(400, "invalid embedding request")
		}
	}
	requestedModel, modelIsString := payload["model"].(string)
	if !modelIsString || requestedModel != model {
		d.Logger.Warn("v7 embedding model mismatch — refusing",
			slog.Any("requested", payload["model"]), slog.String("locked", model))
		return nil, httpErrorf(400, "invalid embedding request")
	}
	dimsOk := false
	if n, ok := asNumber(payload["dimensions"]); ok {
		dimsOk = numFloat(n) == float64(dimensions)
	}
	inputs, inputsOk := payload["input"].([]any)
	if !dimsOk || payload["encoding_format"] != "float" ||
		!inputsOk || len(inputs) < 1 || len(inputs) > embeddingMaxInputs {
		return nil, httpErrorf(400, "invalid embedding request")
	}
	out := make([]string, len(inputs))
	for i, raw := range inputs {
		s, ok := raw.(string)
		if !ok || s == "" {
			return nil, httpErrorf(400, "invalid embedding request")
		}
		out[i] = s
	}
	return out, nil
}

// usesHostedEmbeddings mirrors _uses_hosted_embeddings (a lower-bound era
// predicate, not an allowlist).
func usesHostedEmbeddings(benchVersion int32) bool { return benchVersion >= 7 }

// handleEmbeddings is POST /api/v1/inference/embeddings: the reviewed v7+
// embedding contract under separate budgets.
func (d *Deps) handleEmbeddings(w http.ResponseWriter, r *http.Request) {
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
	body, err := io.ReadAll(io.LimitReader(r.Body, cfg.EmbeddingRequestBodyBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if int64(len(body)) > cfg.EmbeddingRequestBodyBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "embedding request is too large", nil)
		return
	}
	if absDuration(d.now().Sub(headers.requestedAt)) > proxyMaxAge {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "embedding request is stale", nil)
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

	// The operator policy is a background-refreshed atomic snapshot, resolved
	// before the admission transaction without SQL on the request path.
	admissionCfg := applySettings(cfg, d.Settings.Resolve())

	// Detached from client cancellation for Python parity: a broker abort
	// must not cancel the provider call mid-flight and charge the full
	// reservation (see the identical detach in handleChatCompletions).
	ctx := context.WithoutCancel(r.Context())
	now := d.now()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("embeddings: begin admission", slog.String("error", err.Error()))
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
		d.Logger.Error("embeddings: grant snapshot", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err != nil ||
		!usesHostedEmbeddings(grantSnapshot.BenchVersion) ||
		!grantSnapshot.BrokerPublicKey.Valid ||
		int64(grantSnapshot.Generation) != headers.generation ||
		grantSnapshot.EmbeddingModel != cfg.EmbeddingModel ||
		grantSnapshot.EmbeddingProfile != cfg.EmbeddingProfile ||
		grantSnapshot.EmbeddingProvider != cfg.EmbeddingProvider ||
		int(grantSnapshot.EmbeddingDimensions) != cfg.EmbeddingDimensions {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid inference proof", nil)
		return
	}
	if !verifyProxyProof(&grantSnapshot, headers, body) {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid inference proof", nil)
		return
	}
	result, decline, err := beginInferenceRequest(ctx, tx, q, beginParams{
		grantID:             headers.grant,
		nonce:               headers.nonce,
		bearer:              strings.TrimPrefix(headers.authorization, "Bearer "),
		model:               cfg.EmbeddingModel,
		tokenReservation:    estimatedTokens(body),
		maxChargeableTokens: maxChargeableTokens(body, 0),
		now:                 now,
		kind:                kindEmbedding,
		timeoutSeconds:      admissionCfg.TimeoutSeconds,
		limits:              limitsForKind(admissionCfg, kindEmbedding),
	})
	if err != nil {
		d.Logger.Error("embeddings: admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if decline != nil {
		d.traceDeclined(r, headers, traces.LaneInference, traces.KindEmbedding, body, now,
			traceInferenceGrant(&grantSnapshot, cfg.EmbeddingModel), decline.String())
		relayhttp.WriteDecline(w, r, *decline, relayhttp.LaneEmbedding)
		return
	}
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("embeddings: commit admission", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	admissionOpen = false

	outcome := &settleOutcome{
		status:            "failed",
		upstreamProvider:  textValue(cfg.EmbeddingProvider),
		recordObservation: false, // NEVER observe routes on the embedding lane
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
		// Embedding settle: completion 0; cost from the catalog price
		// ($0.004 / 1M input tokens, banker's rounding); provider cost is
		// never trusted; usage_available tracks completion status.
		outcome.completionTokens = 0
		outcome.costMicrousd = int64(math.RoundToEven(float64(outcome.promptTokens) * 0.004))
		outcome.usageAvailable = outcome.status == "completed"
		deliverable, settleErr = d.settleRequest(ctx, headers, outcome)
		if settleErr != nil {
			d.Logger.Error("embeddings: settle", slog.String("error", settleErr.Error()))
		}
	}
	defer settle()

	var raw []byte
	var requestFailure *httpError
	providerResult, callErr := postEmbeddingProvider(ctx, d.Upstream, cfg, inputs, d.sleep())
	if providerResult != nil && !providerResult.direct {
		if receiptFreeResultOverload(
			providerResult.result, cfg.EmbeddingModel, cfg.EmbeddingProvider,
		) {
			_ = d.openProviderCircuit(
				ctx,
				now,
				providerResult.result.status,
				"embedding_provider_backpressure_"+strconv.Itoa(providerResult.result.status),
			)
		} else if providerResult.result.status < 400 {
			_ = d.closeProviderCircuit(ctx, now)
		}
	}
	if callErr != nil {
		outcome.upstreamAttempts = callErr.attempts
		outcome.timedOut = callErr.timedOut
		if callErr.timedOut {
			outcome.terminalErrorCode = textValue("embedding_provider_timeout")
			requestFailure = httpErrorf(504, "embedding provider timed out")
		} else {
			outcome.terminalErrorCode = textValue("embedding_provider_transport")
			requestFailure = httpErrorf(502, "embedding provider unavailable")
		}
	} else {
		upstream := providerResult.result
		outcome.upstreamAttempts = providerResult.attempts
		requestFailure = func() *httpError {
			if upstream.bodyOverLimit {
				return httpErrorf(502, "embedding response is too large")
			}
			if upstream.status >= 400 {
				if providerIsBackpressure(upstream.status, upstream.header) {
					outcome.terminalErrorCode = textValue("embedding_provider_backpressure_" + strconv.Itoa(upstream.status))
					failure := httpErrorf(503, "embedding provider is temporarily at capacity")
					failure.headers = map[string]string{
						"Retry-After": strconv.Itoa(providerRetryAfterSeconds(upstream.header)),
					}
					return failure
				}
				outcome.terminalErrorCode = textValue("embedding_provider_http_" + strconv.Itoa(upstream.status))
				return httpErrorf(502, "embedding provider unavailable")
			}
			decodedResponse, decodeOk := decodeJSONNumbers(upstream.body)
			if !decodeOk {
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
	}
	settle()
	d.traceEmbeddingSettled(embeddingTrace{
		r: r, headers: headers, body: body, receivedAt: now, grant: &result.grant, model: cfg.EmbeddingModel,
		inputs: inputs, outcome: outcome, result: providerResult, callErr: callErr, raw: raw, started: started,
		deliverable: deliverable, failure: requestFailure, settleErr: settleErr,
		reserved: result.request.ReservedTokens, chargeable: result.request.MaxChargeableTokens, admittedAt: now,
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
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "embedding grant is no longer live", nil)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(raw)
}
