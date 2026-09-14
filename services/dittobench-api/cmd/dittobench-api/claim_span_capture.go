package main

// Bench v13 CLAIM-SPAN CAPTURE: the broker-side record that feeds the scorer's
// claim-span provenance gate and causal answer_in_prompt gate
// (internal/scoregates text_provenance.go / causal_dependence.go; issues
// #1833, #1849). For every successful chat completion the relay forwards, it
// books on the attributed wire case:
//
//   - the value tokens of every HARNESS-AUTHORED request span -- system and
//     developer messages, the user template (the case's own question is
//     subtracted by the scorer, which knows it), an assistant prefill, tool-role
//     messages, and the Anthropic top-level `system` -- noting which of them no
//     earlier completion of the same case had already produced;
//   - the value tokens of every MODEL-EMITTED completion span -- message
//     content (including JSON-mode structured output), tool_call arguments (a
//     `final_answer` tool delivery), and Anthropic text / tool_use blocks.
//
// It also books the value tokens of every tool_endpoint result the validator
// served the case (handleTool), which the causal gate exempts.
//
// Hashes only. Like the Bench v12 answer-IO capture it never stores prompt text,
// completion text, or the answer key (which lives with the scorer and was never
// in the broker); the ledger is scoregates.ClaimSpanLedger so the relay and the
// gate share one token rule. Capture is bounded per case; a case that hits a
// bound is marked truncated and the gate fails OPEN for it.
//
// Attribution is exact or absent, in the same evidence order as the trace
// context and the catalog capture: an exclusive case window, then a verified
// case claim naming an in-flight case (the case-scoped inference_base_url path
// `/run/<case_id>/...` the scorer mints for v13, or an X-Ditto-Case-Id header),
// then a sole in-flight /run. Under concurrent /run with several cases in
// flight and no claim, the completion is booked nowhere and every case in
// flight at admission is marked incomplete with an unattributed-call count:
// the v13 contract makes attributable calls the harness's obligation, so the
// scorer charges those cases to the harness (claim_provenance_unattributed_call;
// fail CLOSED under enforce) rather than failing open. Capture bounds and
// unreadable bodies are the relay's own gaps and still fail open. Every case a
// v13 /run registers gets a ledger at registration, so a case whose harness
// made NO model call settles as no_model_completion instead of vanishing.
// Everything here is a no-op below bench_version 13, so v9..v12 sessions
// allocate nothing and stay byte-identical.

import (
	"encoding/json"
	"net/url"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	// claimSpanMaxCompletionsPerCase bounds the completions booked per case. A
	// real case makes a handful; beyond the ceiling the ledger is truncated
	// (fail open) and counts keep increasing.
	claimSpanMaxCompletionsPerCase = 256
	// claimSpanMaxToolResultBytes bounds one captured tool_endpoint result body.
	claimSpanMaxToolResultBytes = 1 << 20
)

// brokerClaimSpanLedger is one wire case's claim-span evidence under construction.
type brokerClaimSpanLedger struct {
	ledger              *scoregates.ClaimSpanLedger
	unattributedOverlap int
	unparseableRequests int
}

// claimSpanCaptureEnabled reports whether this session records claim-span
// evidence. Gated on the shared floor so v12 and earlier sessions allocate nothing.
func claimSpanCaptureEnabled(session *brokerSession) bool {
	return session.benchVersion >= protocol.BenchVersionV13
}

// claimSpanCaptureEnabledFor is the unlocked broker-level read handleTool uses
// before allocating a response recorder: false for an unknown session and for
// every session below bench_version 13, so v10..v12 tool responses stream
// through untouched.
func (b *inferenceBroker) claimSpanCaptureEnabledFor(sessionID string) bool {
	if sessionID == "" {
		return false
	}
	b.mu.RLock()
	session := b.sessions[sessionID]
	b.mu.RUnlock()
	return session != nil && claimSpanCaptureEnabled(session)
}

// claimSpanCasePathPrefix is the path segment of the case-scoped inference
// base URL the scorer mints for v13 /run requests: `<gateway>/run/<case_id>`.
// The harness's OpenAI-compatible client appends `/chat/completions` (or
// `/v1/chat/completions`), so the broker sees `/run/<case_id>/chat/completions`
// and reads <case_id> as the case claim -- the same advisory claim as
// X-Ditto-Case-Id, verified only against the cases in flight and never an
// admission input.
const claimSpanCasePathPrefix = "/run/"

// splitClaimSpanCasePath strips a leading `/run/<case_id>` from a broker route
// path, returning the claimed case id (unescaped) and the remainder. ok=false
// when the path carries no case segment; the remainder is then the input.
func splitClaimSpanCasePath(rest string) (caseID, remainder string, ok bool) {
	if !strings.HasPrefix(rest, claimSpanCasePathPrefix) {
		return "", rest, false
	}
	parts := strings.SplitN(strings.TrimPrefix(rest, claimSpanCasePathPrefix), "/", 2)
	if len(parts) != 2 || parts[0] == "" {
		return "", rest, false
	}
	caseID, err := url.PathUnescape(parts[0])
	if err != nil || strings.TrimSpace(caseID) == "" {
		return "", rest, false
	}
	return caseID, "/" + strings.TrimLeft(parts[1], "/"), true
}

// v13CaseInferenceBaseURL mints the case-scoped inference base URL sent in a
// v13 /run request: the harness-facing gateway with the case named in the path.
// Empty below bench_version 13 (the wire field stays omitted) and when the run
// has no sandbox gateway (the direct-harness development path).
func v13CaseInferenceBaseURL(benchVersion int, gateway, caseID string) string {
	if benchVersion < protocol.BenchVersionV13 || gateway == "" || caseID == "" {
		return ""
	}
	return strings.TrimRight(gateway, "/") + claimSpanCasePathPrefix + url.PathEscape(caseID)
}

// ensureClaimSpanLedgerLocked returns the case's ledger, creating it. Caller
// holds session.mu and has checked claimSpanCaptureEnabled.
func ensureClaimSpanLedgerLocked(session *brokerSession, caseID string) *brokerClaimSpanLedger {
	if session.claimSpanCases == nil {
		session.claimSpanCases = make(map[string]*brokerClaimSpanLedger)
	}
	ledger := session.claimSpanCases[caseID]
	if ledger == nil {
		ledger = &brokerClaimSpanLedger{ledger: scoregates.NewClaimSpanLedger()}
		session.claimSpanCases[caseID] = ledger
	}
	return ledger
}

// claimSpanAttributedCaseLocked resolves the one case a chat completion serves:
// an exclusive case window, then a harness claim that names an in-flight case,
// then a sole in-flight case. ok=false means the completion cannot be attributed
// to exactly one case. Caller holds session.mu.
func claimSpanAttributedCaseLocked(session *brokerSession, caseGeneration uint64, claimed string) (string, bool) {
	if caseGeneration != 0 && session.activeCaseGeneration == caseGeneration && session.activeCaseID != "" {
		return session.activeCaseID, true
	}
	claimed = strings.TrimSpace(claimed)
	if claimed != "" && session.runCases[claimed] > 0 {
		return claimed, true
	}
	if len(session.runCases) == 1 {
		for caseID := range session.runCases {
			return caseID, true
		}
	}
	return "", false
}

// claimSpanAttribution is the admission-time attribution of one chat
// completion, resolved before the upstream call so the in-flight set that
// existed when the harness SENT the request decides the booking.
type claimSpanAttribution struct {
	enabled bool
	caseID  string
	exact   bool
	// inFlight is the set of /run cases in flight at admission; when the
	// completion is unattributable and SUCCEEDS, each is marked incomplete.
	inFlight []string
}

// beginClaimSpanCompletionLocked resolves attribution for one admitted chat
// request. Caller holds session.mu.
func beginClaimSpanCompletionLocked(session *brokerSession, caseGeneration uint64, claimed string) claimSpanAttribution {
	if !claimSpanCaptureEnabled(session) {
		return claimSpanAttribution{}
	}
	caseID, ok := claimSpanAttributedCaseLocked(session, caseGeneration, claimed)
	if ok {
		return claimSpanAttribution{enabled: true, caseID: caseID, exact: true}
	}
	attribution := claimSpanAttribution{enabled: true}
	for inFlight := range session.runCases {
		attribution.inFlight = append(attribution.inFlight, inFlight)
	}
	return attribution
}

// recordClaimSpanCompletionLocked books one SUCCESSFUL chat completion's
// harness-authored and model-emitted spans on the attributed case. requestBody
// is the normalized model INPUT the harness sent; responseBody is the model
// COMPLETION. Caller holds session.mu.
func recordClaimSpanCompletionLocked(session *brokerSession, attribution claimSpanAttribution, requestBody, responseBody []byte) {
	if !attribution.enabled || !claimSpanCaptureEnabled(session) {
		return
	}
	session.claimSpanCompletions++
	if !attribution.exact {
		session.claimSpanUnattributed++
		for _, inFlight := range attribution.inFlight {
			ensureClaimSpanLedgerLocked(session, inFlight).unattributedOverlap++
		}
		// The completion still joins the session-wide set: a later
		// assistant-role span quoting it is model-derived.
		if completion, ok := modelCompletionSpans(responseBody); ok {
			recordClaimSpanSessionCompletionLocked(session, completion)
		}
		return
	}
	ledger := ensureClaimSpanLedgerLocked(session, attribution.caseID)
	if ledger.ledger.Completions >= claimSpanMaxCompletionsPerCase {
		ledger.ledger.Truncated = true
		ledger.ledger.Completions++
		return
	}
	harness, requestOK := harnessAuthoredSpans(requestBody)
	if !requestOK {
		// Missing harness spans can only REDUCE the causal gate's flags, so an
		// unparseable request leaves the ledger settled and is only counted.
		ledger.unparseableRequests++
	}
	completion, responseOK := modelCompletionSpans(responseBody)
	if !responseOK {
		// A completion whose spans cannot be read may have carried the served
		// value: the provenance verdict cannot be trusted, so fail open.
		ledger.ledger.Truncated = true
	}
	// Assistant-role spans are tested against every completion the model made
	// earlier anywhere in the session, so carried model turns and model-written
	// summaries from other cases are model-derived, not harness-first.
	ledger.ledger.RecordRequest(harness, completion, session.claimSpanSessionCompletion)
	recordClaimSpanSessionCompletionLocked(session, completion)
}

// recordClaimSpanSessionCompletionLocked books one completion's spans on the
// session-wide completion set, whether or not the completion was attributed to
// a case. The set exempts assistant-role prompt spans from the causal gate; it
// never satisfies the claim-span gate, which reads the per-case ledger. Bounded
// by the shared per-side ceiling. Caller holds session.mu.
func recordClaimSpanSessionCompletionLocked(session *brokerSession, completion []string) {
	if session.claimSpanSessionCompletion == nil {
		session.claimSpanSessionCompletion = make(scoregates.TokenSet)
	}
	for _, span := range completion {
		if len(session.claimSpanSessionCompletion) >= scoregates.MaxValueTokensPerSide {
			return
		}
		session.claimSpanSessionCompletion.AddSpan(span)
	}
}

// recordClaimSpanToolResult books one tool_endpoint result body the validator
// served a case (handleTool). Only a 200 ToolExecResponse with a result is
// booked; errors carry no value. No-op below bench_version 13.
func (b *inferenceBroker) recordClaimSpanToolResult(sessionID, caseID string, body []byte) {
	if sessionID == "" || caseID == "" || len(body) == 0 {
		return
	}
	var response protocol.ToolExecResponse
	if json.Unmarshal(body, &response) != nil || response.Result == "" {
		return
	}
	b.mu.RLock()
	session := b.sessions[sessionID]
	b.mu.RUnlock()
	if session == nil {
		return
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !claimSpanCaptureEnabled(session) {
		return
	}
	ensureClaimSpanLedgerLocked(session, caseID).ledger.RecordToolResult(response.Result)
}

// claimSpanEvidence is the settled per-case read the scorer consumes after /run
// returned. Ledger is a private copy. Complete is false when a completion made
// while the case was in flight could not be attributed to exactly one case
// (UnattributedCalls > 0, charged to the harness) or a capture bound was hit
// (Truncated, charged to the relay); the scorer reads which.
type claimSpanEvidence struct {
	Ledger            *scoregates.ClaimSpanLedger
	Complete          bool
	UnattributedCalls int
	Truncated         bool
}

// sessionClaimSpanEvidence returns the trusted claim-span ledger the broker
// recorded for one case. ok=false when no v13 capture exists for the case (a
// pre-v13 session, or a case the session never registered through beginRunCase
// and never saw a completion or tool result for), which the scorer reports as
// unavailable and treats as unsettled. A registered case that made NO model
// call returns an empty, complete ledger (Completions == 0): that is a settled
// fact the scorer reports as no_model_completion. Every attributed completion
// is booked under the session lock before its response is released to the
// harness, so a read after /run returned sees every completion that informed
// the response.
func (b *inferenceBroker) sessionClaimSpanEvidence(id, caseID string) (claimSpanEvidence, bool) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return claimSpanEvidence{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !claimSpanCaptureEnabled(session) {
		return claimSpanEvidence{}, false
	}
	ledger := session.claimSpanCases[caseID]
	if ledger == nil {
		return claimSpanEvidence{}, false
	}
	copied := &scoregates.ClaimSpanLedger{
		Completion:   ledger.ledger.Completion.Clone(),
		HarnessFirst: ledger.ledger.HarnessFirst.Clone(),
		ToolResult:   ledger.ledger.ToolResult.Clone(),
		Completions:  ledger.ledger.Completions,
		ToolResults:  ledger.ledger.ToolResults,
		Truncated:    ledger.ledger.Truncated,
	}
	return claimSpanEvidence{
		Ledger:            copied,
		Complete:          ledger.unattributedOverlap == 0 && !ledger.ledger.Truncated,
		UnattributedCalls: ledger.unattributedOverlap,
		Truncated:         ledger.ledger.Truncated,
	}, true
}

// sessionClaimSpanTotals are the run-wide completion counts for the report
// summary: how many v13 completions the relay saw and how many it could not
// attribute to exactly one case.
type sessionClaimSpanTotals struct {
	Completions  uint64
	Unattributed uint64
}

func (b *inferenceBroker) sessionClaimSpanTotals(id string) (sessionClaimSpanTotals, bool) {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return sessionClaimSpanTotals{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !claimSpanCaptureEnabled(session) {
		return sessionClaimSpanTotals{}, false
	}
	return sessionClaimSpanTotals{Completions: session.claimSpanCompletions, Unattributed: session.claimSpanUnattributed}, true
}

// contentSpans renders an OpenAI/Anthropic content field -- a string, or an
// array of typed blocks -- as plain-text spans. Text blocks contribute their
// text; tool_use blocks their JSON input; tool_result blocks their nested
// content. Unknown block shapes contribute nothing.
func contentSpans(raw json.RawMessage) []string {
	if len(raw) == 0 {
		return nil
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		if s == "" {
			return nil
		}
		return []string{s}
	}
	var blocks []struct {
		Type    string          `json:"type"`
		Text    string          `json:"text"`
		Input   json.RawMessage `json:"input"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(raw, &blocks) != nil {
		return nil
	}
	var out []string
	for _, block := range blocks {
		if block.Text != "" {
			out = append(out, block.Text)
		}
		if len(block.Input) > 0 && block.Type == "tool_use" {
			out = append(out, string(block.Input))
		}
		if len(block.Content) > 0 {
			out = append(out, contentSpans(block.Content)...)
		}
	}
	return out
}

// harnessAuthoredSpans extracts every span the HARNESS placed in a chat request
// with its role: all messages (the scorer subtracts the case's own question and
// the validator's system prompt, which the harness did not author), plus the
// Anthropic top-level system field. A message under the `assistant` role is
// marked so the ledger tests it against the session's completions (carried
// model turns are model-derived; a fabricated prefill is not). ok=false when
// the body is not a chat request in either shape.
func harnessAuthoredSpans(requestBody []byte) ([]scoregates.RequestSpan, bool) {
	var req struct {
		System   json.RawMessage `json:"system"`
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(requestBody, &req) != nil || (len(req.Messages) == 0 && len(req.System) == 0) {
		return nil, false
	}
	var out []scoregates.RequestSpan
	for _, span := range contentSpans(req.System) {
		out = append(out, scoregates.RequestSpan{Text: span})
	}
	for _, msg := range req.Messages {
		assistant := strings.EqualFold(strings.TrimSpace(msg.Role), "assistant")
		for _, span := range contentSpans(msg.Content) {
			out = append(out, scoregates.RequestSpan{Text: span, Assistant: assistant})
		}
	}
	return out, true
}

// modelCompletionSpans extracts every span the MODEL emitted in a chat
// completion: OpenAI choice message content, tool_call function arguments, a
// legacy function_call's arguments; Anthropic top-level content blocks (text and
// tool_use input). ok=false when the body is neither shape.
func modelCompletionSpans(responseBody []byte) ([]string, bool) {
	var resp struct {
		Choices []struct {
			Message struct {
				Content   json.RawMessage `json:"content"`
				ToolCalls []struct {
					Function struct {
						Arguments string `json:"arguments"`
					} `json:"function"`
				} `json:"tool_calls"`
				FunctionCall *struct {
					Arguments string `json:"arguments"`
				} `json:"function_call"`
			} `json:"message"`
		} `json:"choices"`
		Content json.RawMessage `json:"content"`
	}
	if json.Unmarshal(responseBody, &resp) != nil || (len(resp.Choices) == 0 && len(resp.Content) == 0) {
		return nil, false
	}
	var out []string
	for _, choice := range resp.Choices {
		out = append(out, contentSpans(choice.Message.Content)...)
		for _, call := range choice.Message.ToolCalls {
			if call.Function.Arguments != "" {
				out = append(out, call.Function.Arguments)
			}
		}
		if choice.Message.FunctionCall != nil && choice.Message.FunctionCall.Arguments != "" {
			out = append(out, choice.Message.FunctionCall.Arguments)
		}
	}
	out = append(out, contentSpans(resp.Content)...)
	return out, true
}
