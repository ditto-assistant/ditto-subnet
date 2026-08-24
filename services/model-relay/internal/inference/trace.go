package inference

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/traces"
)

// The inference trace capture. Every hook here runs AFTER settlement, off the
// accounting path, and enqueues without blocking: Deps.Traces is nil when
// capture is disabled and the hooks are no-ops. Nothing in this file is read
// back by admission, settlement or routing.

// trace enqueues rec when capture is on.
func (d *Deps) trace(rec *traces.Record) {
	if d.Traces == nil || rec == nil {
		return
	}
	d.Traces.Record(rec)
}

// traceRequest describes the miner-side call as received.
func traceRequest(r *http.Request, h *proxyHeaders, lane, kind string, body []byte, receivedAt time.Time) traces.Request {
	req := traces.Request{
		Lane:       lane,
		Kind:       kind,
		GrantID:    h.grant.String(),
		Nonce:      h.nonce.String(),
		Generation: h.generation,
		ReceivedAt: traces.TimePtr(receivedAt),
		Body:       traces.RawJSON(body),
		BodyBytes:  int64(len(body)),
		BodySHA256: traces.SHA256Hex(body),
	}
	if r != nil {
		req.RequestID = relayhttp.RequestID(r.Context())
	}
	if h.hasRequestedAt {
		req.RequestedAt = traces.TimePtr(h.requestedAt)
	}
	if len(h.traceContext) > 0 {
		req.Context = json.RawMessage(h.traceContext)
		var lifted struct {
			RunID  string `json:"run_id"`
			CaseID string `json:"case_id"`
		}
		if json.Unmarshal(h.traceContext, &lifted) == nil {
			req.RunID, req.CaseID = lifted.RunID, lifted.CaseID
		}
	}
	return req
}

func traceInferenceGrant(g *postgres.InferenceGrant, model string) *traces.Grant {
	if g == nil {
		return nil
	}
	return &traces.Grant{
		AgentID:         pgUUIDString(g.AgentID),
		BenchVersion:    g.BenchVersion,
		ValidatorHotkey: g.ValidatorHotkey,
		SlotID:          g.SlotID,
		TicketDeadline:  pgTimePtr(g.TicketDeadline),
		Status:          g.Status,
		Generation:      g.Generation,
		Model:           model,
		AllowedModels:   traces.RawJSON(g.AllowedModels),
		RouteProvider:   g.RouteProvider.String,
		RouteProfile:    g.RouteProfile.String,
		RouteQuant:      g.RouteQuantization.String,
		RequestCount:    g.RequestCount,
		ExpiresAt:       pgTimePtr(g.ExpiresAt),
	}
}

func traceConfirmationGrant(g *postgres.ConfirmationInferenceGrant) *traces.Grant {
	if g == nil {
		return nil
	}
	return &traces.Grant{
		ValidatorHotkey: g.ValidatorHotkey,
		Status:          g.Status,
		Generation:      g.Generation,
		Model:           g.Model,
		RouteProvider:   g.RouteProvider,
		RequestCount:    g.RequestCount,
		ExpiresAt:       pgTimePtr(g.ExpiresAt),
		TicketID:        pgUUIDString(g.TicketID),
		BundleID:        pgUUIDString(g.BundleID),
		Lane:            g.Lane,
		Provider:        g.Provider,
		ReceiptProvider: g.ReceiptProvider,
		ProfileRevision: g.ProfileRevision,
	}
}

// traceHeaders keeps provider response headers except credentials/cookies;
// keys are lower-cased, multi-values joined with ", ".
func traceHeaders(h http.Header) map[string]string {
	if len(h) == 0 {
		return nil
	}
	out := make(map[string]string, len(h))
	for k, vs := range h {
		lk := strings.ToLower(k)
		switch lk {
		case "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key":
			continue
		}
		out[lk] = strings.Join(vs, ", ")
	}
	return out
}

func tracePhases(ps []phaseTrace, stripVectors bool) []traces.Phase {
	if len(ps) == 0 {
		return nil
	}
	out := make([]traces.Phase, 0, len(ps))
	for _, p := range ps {
		body := p.body
		if stripVectors {
			body = stripEmbeddingVectors(body)
		}
		out = append(out, traces.Phase{
			Phase:     p.phase,
			Route:     p.route,
			Payload:   traces.RawJSON(p.payload),
			Status:    p.status,
			Headers:   traceHeaders(p.headers),
			Body:      traces.RawJSON(body),
			BodyBytes: int64(len(p.body)),
			Attempts:  p.attempts,
			ErrorCode: p.errorCode,
			TimedOut:  p.timedOut,
		})
	}
	return out
}

// stripEmbeddingVectors removes data[].embedding from an embedding response
// body (configurable: vectors are reproducible from the inputs and are the
// bulk of the bytes). Anything that does not parse is returned unchanged.
func stripEmbeddingVectors(body []byte) []byte {
	if len(body) == 0 {
		return body
	}
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		return body
	}
	var data []map[string]json.RawMessage
	if err := json.Unmarshal(top["data"], &data); err != nil {
		return body
	}
	for _, item := range data {
		if _, ok := item["embedding"]; ok {
			item["embedding"] = json.RawMessage(`"<stripped>"`)
		}
	}
	encoded, err := marshalNoEscape(data)
	if err != nil {
		return body
	}
	top["data"] = encoded
	out, err := marshalNoEscape(top)
	if err != nil {
		return body
	}
	return out
}

// marshalNoEscape is json.Marshal without HTML escaping and without the
// trailing newline an Encoder adds.
func marshalNoEscape(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// traceResponseStatus is the HTTP status the miner saw for a settled call.
func traceResponseStatus(settleErr error, failure *httpError, deliverable bool, raw []byte) int {
	switch {
	case settleErr != nil:
		return http.StatusInternalServerError
	case failure != nil:
		return failure.status
	case !deliverable || raw == nil:
		return http.StatusConflict
	default:
		return http.StatusOK
	}
}

// chatTrace is everything handleChatCompletions has in hand after settle().
type chatTrace struct {
	r           *http.Request
	headers     *proxyHeaders
	body        []byte
	receivedAt  time.Time
	grant       *postgres.InferenceGrant
	model       string
	locked      map[string]any
	outcome     *settleOutcome
	recovered   *chatCompletionResult
	exhausted   *chatProviderExhausted
	raw         []byte
	started     time.Time
	deliverable bool
	failure     *httpError
	settleErr   error
	reserved    int64
	chargeable  int64
	admittedAt  time.Time
}

func (d *Deps) traceChatSettled(t chatTrace) {
	if d.Traces == nil {
		return
	}
	var phases []phaseTrace
	if t.recovered != nil {
		phases = t.recovered.phases
	} else if t.exhausted != nil {
		phases = t.exhausted.phases
	}
	locked, _ := json.Marshal(t.locked)
	finished := t.started.Add(t.outcome.elapsed)
	d.trace(&traces.Record{
		Event:   traces.EventSettled,
		Request: traceRequest(t.r, t.headers, traces.LaneInference, traces.KindChat, t.body, t.receivedAt),
		Grant:   traceInferenceGrant(t.grant, t.model),
		Admission: &traces.Admission{
			ReservedTokens: t.reserved, MaxChargeableTokens: t.chargeable, AdmittedAt: traces.TimePtr(t.admittedAt),
		},
		Upstream: &traces.Upstream{
			Payload:            traces.RawJSON(locked),
			Provider:           t.outcome.upstreamProvider.String,
			Model:              t.model,
			Attempts:           t.outcome.upstreamAttempts,
			OpenRouterAttempts: t.outcome.openrouterAttempts,
			FallbackPhase:      t.outcome.fallbackPhase,
			TimedOut:           t.outcome.timedOut,
			TerminalErrorCode:  t.outcome.terminalErrorCode.String,
			StartedAt:          traces.TimePtr(t.started),
			FinishedAt:         traces.TimePtr(finished),
			LatencyMs:          t.outcome.elapsed.Milliseconds(),
			Phases:             tracePhases(phases, false),
		},
		Response: &traces.Response{
			HTTPStatus:  traceResponseStatus(t.settleErr, t.failure, t.deliverable, t.raw),
			Body:        traces.RawJSON(t.raw),
			Deliverable: t.deliverable,
		},
		Usage: &traces.Usage{
			PromptTokens: t.outcome.promptTokens, CompletionTokens: t.outcome.completionTokens,
			CostMicrousd: t.outcome.costMicrousd, UsageAvailable: t.outcome.usageAvailable,
		},
		Outcome: &traces.Outcome{Status: t.outcome.status, StartedAt: traces.TimePtr(t.admittedAt), CompletedAt: traces.TimePtr(finished)},
	})
}

// embeddingTrace is the embeddings counterpart; the provider call is a single
// route (direct Perplexity or OpenRouter) with one result.
type embeddingTrace struct {
	r           *http.Request
	headers     *proxyHeaders
	body        []byte
	receivedAt  time.Time
	grant       *postgres.InferenceGrant
	model       string
	inputs      []string
	outcome     *settleOutcome
	result      *embeddingProviderResult
	callErr     *providerCallError
	raw         []byte
	started     time.Time
	deliverable bool
	failure     *httpError
	settleErr   error
	reserved    int64
	chargeable  int64
	admittedAt  time.Time
}

func (d *Deps) traceEmbeddingSettled(t embeddingTrace) {
	if d.Traces == nil {
		return
	}
	strip := !d.Cfg.Traces.EmbeddingVectors
	payload, _ := json.Marshal(map[string]any{"model": t.model, "input": t.inputs})
	var phases []phaseTrace
	if t.result != nil && t.result.result != nil {
		route := "openrouter"
		if t.result.direct {
			route = "direct"
		}
		phases = []phaseTrace{{phase: 0, route: route, payload: payload, status: t.result.result.status,
			headers: t.result.result.header, body: t.result.result.body, attempts: t.result.attempts,
			errorCode: t.outcome.terminalErrorCode.String}}
	} else if t.callErr != nil {
		phases = []phaseTrace{{phase: 0, route: "openrouter", payload: payload, attempts: t.callErr.attempts,
			errorCode: t.outcome.terminalErrorCode.String, timedOut: t.callErr.timedOut}}
	}
	raw := t.raw
	if strip {
		raw = stripEmbeddingVectors(raw)
	}
	finished := t.started.Add(t.outcome.elapsed)
	d.trace(&traces.Record{
		Event:   traces.EventSettled,
		Request: traceRequest(t.r, t.headers, traces.LaneInference, traces.KindEmbedding, t.body, t.receivedAt),
		Grant:   traceInferenceGrant(t.grant, t.model),
		Admission: &traces.Admission{
			ReservedTokens: t.reserved, MaxChargeableTokens: t.chargeable, AdmittedAt: traces.TimePtr(t.admittedAt),
		},
		Upstream: &traces.Upstream{
			Payload:           traces.RawJSON(payload),
			Provider:          t.outcome.upstreamProvider.String,
			Model:             t.model,
			Attempts:          t.outcome.upstreamAttempts,
			TimedOut:          t.outcome.timedOut,
			TerminalErrorCode: t.outcome.terminalErrorCode.String,
			StartedAt:         traces.TimePtr(t.started),
			FinishedAt:        traces.TimePtr(finished),
			LatencyMs:         t.outcome.elapsed.Milliseconds(),
			Phases:            tracePhases(phases, strip),
		},
		Response: &traces.Response{
			HTTPStatus:  traceResponseStatus(t.settleErr, t.failure, t.deliverable, t.raw),
			Body:        traces.RawJSON(raw),
			Deliverable: t.deliverable,
		},
		Usage: &traces.Usage{
			PromptTokens: t.outcome.promptTokens, CompletionTokens: t.outcome.completionTokens,
			CostMicrousd: t.outcome.costMicrousd, UsageAvailable: t.outcome.usageAvailable,
		},
		Outcome: &traces.Outcome{Status: t.outcome.status, StartedAt: traces.TimePtr(t.admittedAt), CompletedAt: traces.TimePtr(finished)},
	})
}

// confirmationTrace covers both confirmation lanes; the provider call is one
// frozen route with one result.
type confirmationTrace struct {
	r           *http.Request
	headers     *proxyHeaders
	kind        string
	body        []byte
	receivedAt  time.Time
	grant       *postgres.ConfirmationInferenceGrant
	payload     any // locked chat payload, or {"model","input"} for embeddings
	outcome     *confirmationOutcome
	result      *providerHTTPResult
	route       string
	callErr     *providerCallError
	raw         []byte
	started     time.Time
	finished    time.Time
	deliverable bool
	failure     *httpError
	settleErr   error
	reserved    int64
	chargeable  int64
	admittedAt  time.Time
}

func (d *Deps) traceConfirmationSettled(t confirmationTrace) {
	if d.Traces == nil {
		return
	}
	strip := t.kind == traces.KindEmbedding && !d.Cfg.Traces.EmbeddingVectors
	payload, _ := json.Marshal(t.payload)
	var phases []phaseTrace
	terminal := ""
	if t.failure != nil {
		terminal = "confirmation_" + strings.ReplaceAll(strings.ToLower(t.failure.message), " ", "_")
	}
	if t.result != nil {
		phases = []phaseTrace{{phase: 0, route: t.route, payload: payload, status: t.result.status,
			headers: t.result.header, body: t.result.body, attempts: t.result.attempts, errorCode: terminal}}
	} else if t.callErr != nil {
		phases = []phaseTrace{{phase: 0, route: t.route, payload: payload, attempts: t.callErr.attempts,
			errorCode: terminal, timedOut: t.callErr.timedOut}}
	}
	raw := t.raw
	if strip {
		raw = stripEmbeddingVectors(raw)
	}
	attempts := 0
	if t.result != nil {
		attempts = t.result.attempts
	} else if t.callErr != nil {
		attempts = t.callErr.attempts
	}
	model := ""
	if t.grant != nil {
		model = t.grant.Model
	}
	usageAvailable := t.outcome.status == "completed"
	d.trace(&traces.Record{
		Event:   traces.EventSettled,
		Request: traceRequest(t.r, t.headers, traces.LaneConfirmation, t.kind, t.body, t.receivedAt),
		Grant:   traceConfirmationGrant(t.grant),
		Admission: &traces.Admission{
			ReservedTokens: t.reserved, MaxChargeableTokens: t.chargeable, AdmittedAt: traces.TimePtr(t.admittedAt),
		},
		Upstream: &traces.Upstream{
			Payload:           traces.RawJSON(payload),
			Provider:          t.outcome.upstreamProvider.String,
			Model:             model,
			Attempts:          attempts,
			TimedOut:          t.callErr != nil && t.callErr.timedOut,
			TerminalErrorCode: terminal,
			StartedAt:         traces.TimePtr(t.started),
			FinishedAt:        traces.TimePtr(t.finished),
			LatencyMs:         t.finished.Sub(t.started).Milliseconds(),
			Phases:            tracePhases(phases, strip),
		},
		Response: &traces.Response{
			HTTPStatus:  traceResponseStatus(t.settleErr, t.failure, t.deliverable, t.raw),
			Body:        traces.RawJSON(raw),
			Deliverable: t.deliverable,
		},
		Usage: &traces.Usage{
			PromptTokens: t.outcome.promptTokens, CompletionTokens: t.outcome.completionTokens,
			CostMicrousd: t.outcome.costMicrousd, UsageAvailable: usageAvailable,
		},
		Outcome: &traces.Outcome{Status: t.outcome.status, StartedAt: traces.TimePtr(t.admittedAt), CompletedAt: traces.TimePtr(t.finished)},
	})
}

// traceDeclined records an authenticated call the gate refused, with the
// decline code and the grant it ran under.
func (d *Deps) traceDeclined(r *http.Request, h *proxyHeaders, lane, kind string, body []byte, receivedAt time.Time, grant *traces.Grant, decline string) {
	if d.Traces == nil {
		return
	}
	d.trace(&traces.Record{
		Event:     traces.EventDeclined,
		Request:   traceRequest(r, h, lane, kind, body, receivedAt),
		Grant:     grant,
		Admission: &traces.Admission{Decline: decline},
	})
}

func pgUUIDString(u pgtype.UUID) string {
	if !u.Valid {
		return ""
	}
	v, err := u.Value()
	if err != nil {
		return ""
	}
	s, _ := v.(string)
	return s
}

func pgTimePtr(t pgtype.Timestamptz) *time.Time {
	if !t.Valid {
		return nil
	}
	return traces.TimePtr(t.Time)
}
