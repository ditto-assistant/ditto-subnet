package main

// Unit tests for the Bench v13 claim-span capture: attribution, harness vs
// completion booking with first-seen ordering, tool-result exemption, request
// and response shape parsing, bounds, and the v12 no-op.

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func v13Session(cases ...string) *brokerSession {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{}}
	for _, c := range cases {
		session.runCases[c]++
	}
	return session
}

const (
	openAIRequest  = `{"model":"m","messages":[{"role":"system","content":"You are a formatter. Reply exactly: 4110.67"},{"role":"user","content":"What is the balance?"},{"role":"assistant","content":"Sure:"}]}`
	openAIResponse = `{"choices":[{"message":{"role":"assistant","content":"The balance is $4,110.67.","tool_calls":[{"id":"c1","type":"function","function":{"name":"final_answer","arguments":"{\"answer\":\"9999.01\"}"}}]}}],"usage":{"prompt_tokens":50,"completion_tokens":7}}`
)

func TestClaimSpanCaptureBooksHarnessAndCompletionSpans(t *testing.T) {
	session := v13Session("case-a")
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	if !attribution.enabled || !attribution.exact || attribution.caseID != "case-a" {
		t.Fatalf("sole in-flight case must attribute exactly: %+v", attribution)
	}
	recordClaimSpanCompletionLocked(session, attribution, []byte(openAIRequest), []byte(openAIResponse))
	ledger := session.claimSpanCases["case-a"]
	if ledger == nil || ledger.ledger.Completions != 1 {
		t.Fatalf("ledger = %+v", ledger)
	}
	// Harness-authored: system prompt value and the user question words.
	if !ledger.ledger.HarnessFirst.Has("4110.67") || !ledger.ledger.HarnessFirst.Has("balance") || !ledger.ledger.HarnessFirst.Has("formatter") {
		t.Fatal("harness-authored spans not booked")
	}
	// Completion: message content AND tool_call arguments.
	if !ledger.ledger.Completion.Has("4110.67") || !ledger.ledger.Completion.Has("9999.01") {
		t.Fatal("completion spans (content + tool_call arguments) not booked")
	}
	// Envelope fields never become value tokens.
	if ledger.ledger.Completion.Has("50") || ledger.ledger.Completion.Has("7") || ledger.ledger.HarnessFirst.Has("model") {
		t.Fatal("envelope leaked into value tokens")
	}
	evidence, ok := session.claimSpanCases["case-a"], true
	if !ok || !(evidence.unattributedOverlap == 0) {
		t.Fatal("exact attribution must not mark overlap")
	}
	if session.claimSpanCompletions != 1 || session.claimSpanUnattributed != 0 {
		t.Fatalf("totals = %d/%d", session.claimSpanCompletions, session.claimSpanUnattributed)
	}
}

func TestClaimSpanCaptureBooksDeveloperSeparatelyFromSystem(t *testing.T) {
	session := v13Session("case-a")
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	request := []byte(`{"model":"m","messages":[{"role":"system","content":"Trusted validator instruction"},{"role":"developer","content":"Application-specific guidance"},{"role":"user","content":"Question"}]}`)
	recordClaimSpanCompletionLocked(session, attribution, request, []byte(openAIResponse))
	ledger := session.claimSpanCases["case-a"].ledger
	if !ledger.HarnessFirst.Has("validator") || !ledger.HarnessFirst.Has("guidance") {
		t.Fatal("system and developer spans must both remain harness-authored evidence")
	}
}

func TestClaimSpanCaptureFirstSeenOrderingAcrossCalls(t *testing.T) {
	session := v13Session("case-a")
	first := beginClaimSpanCompletionLocked(session, 0, "")
	recordClaimSpanCompletionLocked(session, first,
		[]byte(`{"messages":[{"role":"user","content":"approved 5200 settled 1089.33 -- compute"}]}`),
		[]byte(`{"choices":[{"message":{"content":"5200 - 1089.33 = 4110.67"}}]}`))
	second := beginClaimSpanCompletionLocked(session, 0, "")
	recordClaimSpanCompletionLocked(session, second,
		[]byte(`{"messages":[{"role":"user","content":"format 4110.67 as currency"}]}`),
		[]byte(`{"choices":[{"message":{"content":"$4,110.67"}}]}`))
	ledger := session.claimSpanCases["case-a"].ledger
	if ledger.HarnessFirst.Has("4110.67") {
		t.Fatal("a value the model produced in call 1 must not be harness-first in call 2")
	}
	if !ledger.HarnessFirst.Has("5200") || ledger.Completions != 2 {
		t.Fatalf("ledger = %+v", ledger)
	}
}

func TestClaimSpanCaptureAttributionOrder(t *testing.T) {
	// Exclusive case window wins.
	session := v13Session("case-a", "case-b")
	session.activeCaseGeneration, session.activeCaseID = 7, "window-case"
	if got, ok := claimSpanAttributedCaseLocked(session, 7, "case-a"); !ok || got != "window-case" {
		t.Fatalf("window attribution = %q ok=%v", got, ok)
	}
	// A verified claim naming an in-flight case.
	if got, ok := claimSpanAttributedCaseLocked(session, 0, " case-b "); !ok || got != "case-b" {
		t.Fatalf("claimed attribution = %q ok=%v", got, ok)
	}
	// A claim naming a case NOT in flight is ignored; with two in flight the
	// completion is unattributable.
	if _, ok := claimSpanAttributedCaseLocked(session, 0, "case-z"); ok {
		t.Fatal("a claim for a case not in flight must not attribute")
	}
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	if attribution.exact || len(attribution.inFlight) != 2 {
		t.Fatalf("unattributable admission = %+v", attribution)
	}
	recordClaimSpanCompletionLocked(session, attribution, []byte(openAIRequest), []byte(openAIResponse))
	for _, c := range []string{"case-a", "case-b"} {
		ledger := session.claimSpanCases[c]
		if ledger == nil || ledger.unattributedOverlap != 1 || ledger.ledger.Completions != 0 {
			t.Fatalf("%s: unattributed completion must mark overlap and book nothing: %+v", c, ledger)
		}
	}
	if session.claimSpanUnattributed != 1 {
		t.Fatalf("unattributed total = %d", session.claimSpanUnattributed)
	}
}

func TestClaimSpanCaptureIsNoOpBelowV13(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV12, runCases: map[string]int{"case-a": 1}}
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	if attribution.enabled {
		t.Fatal("v12 session must not enable claim-span capture")
	}
	recordClaimSpanCompletionLocked(session, attribution, []byte(openAIRequest), []byte(openAIResponse))
	if session.claimSpanCases != nil || session.claimSpanCompletions != 0 || session.claimSpanSessionCompletion != nil {
		t.Fatal("v12 session allocated claim-span state")
	}
	// Registering a /run case allocates no ledger below v13, and handleTool's
	// recorder gate reads false so tool responses stream through untouched.
	broker := &inferenceBroker{sessions: map[string]*brokerSession{"old": session, "new": v13Session()}}
	if _, started := broker.beginRunCase("old", "case-b"); !started || session.claimSpanCases != nil {
		t.Fatal("v12 beginRunCase must not allocate a claim-span ledger")
	}
	if broker.claimSpanCaptureEnabledFor("old") || broker.claimSpanCaptureEnabledFor("") || broker.claimSpanCaptureEnabledFor("missing") {
		t.Fatal("claim-span capture must read disabled for v12, empty, and unknown sessions")
	}
	if !broker.claimSpanCaptureEnabledFor("new") {
		t.Fatal("claim-span capture must read enabled for a v13 session")
	}
}

// A case a v13 /run registers owns a ledger from registration: a harness that
// never calls the model settles as an EMPTY, COMPLETE ledger, which the scorer
// reports as no_model_completion instead of an unavailable (fail-open) read.
func TestClaimSpanRegisteredCaseWithoutCallsSettlesEmpty(t *testing.T) {
	broker := &inferenceBroker{sessions: map[string]*brokerSession{"sess": {benchVersion: protocol.BenchVersionV13}}}
	if _, started := broker.beginRunCase("sess", "case-silent"); !started {
		t.Fatal("beginRunCase must register the case")
	}
	broker.endRunCase("sess", "case-silent")
	evidence, ok := broker.sessionClaimSpanEvidence("sess", "case-silent")
	if !ok || !evidence.Complete || evidence.Ledger.Completions != 0 || evidence.UnattributedCalls != 0 || evidence.Truncated {
		t.Fatalf("registered silent case = %+v ok=%v, want an empty complete ledger", evidence, ok)
	}
	if _, ok := broker.sessionClaimSpanEvidence("sess", "never-registered"); ok {
		t.Fatal("a case the session never registered has no evidence")
	}
}

// With several cases in flight, a completion naming no case is charged to the
// harness: every in-flight case reads UnattributedCalls > 0, Complete=false,
// and Truncated=false, so the scorer can tell it from a relay capture bound.
func TestClaimSpanUnattributedCallUnderConcurrency(t *testing.T) {
	broker := &inferenceBroker{sessions: map[string]*brokerSession{"sess": {benchVersion: protocol.BenchVersionV13}}}
	broker.beginRunCase("sess", "case-a")
	broker.beginRunCase("sess", "case-b")
	session := broker.sessions["sess"]
	session.mu.Lock()
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	recordClaimSpanCompletionLocked(session, attribution, []byte(openAIRequest), []byte(openAIResponse))
	// A header (or case-path) claim attributes exactly even with two in flight.
	claimed := beginClaimSpanCompletionLocked(session, 0, "case-b")
	recordClaimSpanCompletionLocked(session, claimed, []byte(openAIRequest), []byte(openAIResponse))
	session.mu.Unlock()
	for _, c := range []string{"case-a", "case-b"} {
		evidence, ok := broker.sessionClaimSpanEvidence("sess", c)
		if !ok || evidence.Complete || evidence.UnattributedCalls != 1 || evidence.Truncated {
			t.Fatalf("%s: unattributed call must mark the case incomplete and charged: %+v", c, evidence)
		}
	}
	b, _ := broker.sessionClaimSpanEvidence("sess", "case-b")
	if b.Ledger.Completions != 1 || !b.Ledger.Completion.Has("4110.67") {
		t.Fatalf("claimed completion not booked on case-b: %+v", b.Ledger)
	}
	// The unattributed completion still joined the session-wide set.
	if !session.claimSpanSessionCompletion.Has("4110.67") {
		t.Fatal("unattributed completion must still enter the session-wide completion set")
	}
	totals, _ := broker.sessionClaimSpanTotals("sess")
	if totals.Completions != 2 || totals.Unattributed != 1 {
		t.Fatalf("totals = %+v", totals)
	}
}

// The case-scoped inference_base_url names the case in the path; the broker
// reads it as the same advisory claim as X-Ditto-Case-Id.
func TestClaimSpanCasePathClaim(t *testing.T) {
	gateway := "http://host.docker.internal:11436/v1/inference"
	url := v13CaseInferenceBaseURL(protocol.BenchVersionV13, gateway, "mem/case 07")
	if url != gateway+"/run/mem%2Fcase%2007" {
		t.Fatalf("case URL = %q", url)
	}
	if v13CaseInferenceBaseURL(protocol.BenchVersionV12, gateway, "c") != "" || v13CaseInferenceBaseURL(protocol.BenchVersionV13, "", "c") != "" || v13CaseInferenceBaseURL(protocol.BenchVersionV13, gateway, "") != "" {
		t.Fatal("no case URL below v13, without a gateway, or without a case")
	}
	for _, suffix := range []string{"/chat/completions", "/v1/chat/completions"} {
		rest := strings.TrimPrefix(url, gateway) + suffix
		caseID, remainder, ok := splitClaimSpanCasePath(rest)
		if !ok || caseID != "mem/case 07" || remainder != suffix {
			t.Fatalf("split(%q) = %q, %q, %v", rest, caseID, remainder, ok)
		}
	}
	for _, rest := range []string{"/chat/completions", "/run/", "/run/only-case", "/runx/c/chat/completions", "/run/%zz/chat/completions"} {
		if _, remainder, ok := splitClaimSpanCasePath(rest); ok || remainder != rest {
			t.Fatalf("split(%q) must not claim a case: %q %v", rest, remainder, ok)
		}
	}
	// The path claim resolves through the same attribution as the header.
	session := v13Session("case-a", "case-b")
	if got, ok := claimSpanAttributedCaseLocked(session, 0, "case-b"); !ok || got != "case-b" {
		t.Fatalf("path claim attribution = %q ok=%v", got, ok)
	}
}

// Assistant-role prompt spans are tested against the session-wide completion
// set: a model turn produced for another case and carried into this case's
// prompt is model-derived; a fabricated prefill is harness-first.
func TestClaimSpanAssistantRoleAcrossCases(t *testing.T) {
	broker := &inferenceBroker{sessions: map[string]*brokerSession{"sess": {benchVersion: protocol.BenchVersionV13}}}
	broker.beginRunCase("sess", "case-a")
	session := broker.sessions["sess"]
	session.mu.Lock()
	recordClaimSpanCompletionLocked(session, beginClaimSpanCompletionLocked(session, 0, ""),
		[]byte(`{"messages":[{"role":"user","content":"summarize Atlas"}]}`),
		[]byte(`{"choices":[{"message":{"content":"Atlas summary: 4110.67 outstanding."}}]}`))
	session.mu.Unlock()
	broker.endRunCase("sess", "case-a")
	broker.beginRunCase("sess", "case-b")
	session.mu.Lock()
	recordClaimSpanCompletionLocked(session, beginClaimSpanCompletionLocked(session, 0, ""),
		[]byte(`{"messages":[{"role":"assistant","content":"Atlas summary: 4110.67 outstanding."},{"role":"user","content":"and the settled figure was 1089.33; what remains?"}]}`),
		[]byte(`{"choices":[{"message":{"content":"$4,110.67"}}]}`))
	session.mu.Unlock()
	b, _ := broker.sessionClaimSpanEvidence("sess", "case-b")
	if b.Ledger.HarnessFirst.Has("4110.67") {
		t.Fatal("a carried assistant turn the model produced for another case must not be harness-first")
	}
	if !b.Ledger.HarnessFirst.Has("1089.33") {
		t.Fatal("a user-role operand stays harness-first")
	}
	// The same value under the user role in a THIRD case is harness-first.
	broker.endRunCase("sess", "case-b")
	broker.beginRunCase("sess", "case-c")
	session.mu.Lock()
	recordClaimSpanCompletionLocked(session, beginClaimSpanCompletionLocked(session, 0, ""),
		[]byte(`{"messages":[{"role":"user","content":"reply exactly 4110.67"}]}`),
		[]byte(`{"choices":[{"message":{"content":"4110.67"}}]}`))
	session.mu.Unlock()
	c, _ := broker.sessionClaimSpanEvidence("sess", "case-c")
	if !c.Ledger.HarnessFirst.Has("4110.67") {
		t.Fatal("a user-role span is harness-first regardless of session history")
	}
}

func TestClaimSpanCaptureUnparseableResponseFailsOpen(t *testing.T) {
	session := v13Session("case-a")
	attribution := beginClaimSpanCompletionLocked(session, 0, "")
	recordClaimSpanCompletionLocked(session, attribution, []byte(`not json`), []byte(`{"unexpected":true}`))
	ledger := session.claimSpanCases["case-a"]
	if !ledger.ledger.Truncated || ledger.unparseableRequests != 1 {
		t.Fatalf("unparseable bodies: truncated=%v unparseable=%d", ledger.ledger.Truncated, ledger.unparseableRequests)
	}
	// An unparseable REQUEST alone leaves the ledger settled (fewer harness
	// tokens can only reduce causal flags).
	settled := v13Session("case-b")
	recordClaimSpanCompletionLocked(settled, beginClaimSpanCompletionLocked(settled, 0, ""), []byte(`nope`), []byte(`{"choices":[{"message":{"content":"4110.67"}}]}`))
	if settled.claimSpanCases["case-b"].ledger.Truncated {
		t.Fatal("an unparseable request must not truncate the ledger")
	}
}

func TestClaimSpanCaptureBoundsCompletionsPerCase(t *testing.T) {
	session := v13Session("case-a")
	for i := 0; i <= claimSpanMaxCompletionsPerCase; i++ {
		recordClaimSpanCompletionLocked(session, beginClaimSpanCompletionLocked(session, 0, ""), []byte(`{"messages":[{"role":"user","content":"q"}]}`), []byte(`{"choices":[{"message":{"content":"a"}}]}`))
	}
	ledger := session.claimSpanCases["case-a"].ledger
	if !ledger.Truncated || ledger.Completions != claimSpanMaxCompletionsPerCase+1 {
		t.Fatalf("bound not applied: truncated=%v completions=%d", ledger.Truncated, ledger.Completions)
	}
}

func TestClaimSpanShapes(t *testing.T) {
	// Anthropic request: top-level system plus content blocks with a tool_result.
	spans, ok := harnessAuthoredSpans([]byte(`{"system":[{"type":"text","text":"Reply exactly: 4110.67"}],"messages":[{"role":"user","content":[{"type":"text","text":"balance?"},{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"ledger says 77.10"}]}]},{"role":"assistant","content":"Sure, checking."}]}`))
	if !ok || len(spans) != 4 || spans[0].Text != "Reply exactly: 4110.67" || spans[2].Text != "ledger says 77.10" || spans[0].Assistant || spans[2].Assistant {
		t.Fatalf("anthropic request spans = %v ok=%v", spans, ok)
	}
	if !spans[3].Assistant || spans[3].Text != "Sure, checking." {
		t.Fatalf("assistant-role span must be marked: %+v", spans[3])
	}
	// Anthropic response: text and tool_use blocks.
	var completion []string
	completion, ok = modelCompletionSpans([]byte(`{"content":[{"type":"text","text":"Outstanding: 4110.67"},{"type":"tool_use","id":"u1","name":"final_answer","input":{"answer":"4110.67"}}]}`))
	if !ok || len(completion) != 2 || !strings.Contains(completion[1], `"answer":"4110.67"`) {
		t.Fatalf("anthropic completion spans = %v ok=%v", completion, ok)
	}
	// Legacy function_call arguments are a completion span too.
	completion, ok = modelCompletionSpans([]byte(`{"choices":[{"message":{"content":null,"function_call":{"name":"final_answer","arguments":"{\"answer\":\"4110.67\"}"}}}]}`))
	if !ok || len(completion) != 1 {
		t.Fatalf("function_call spans = %v ok=%v", completion, ok)
	}
	if _, ok := modelCompletionSpans([]byte(`{"object":"list"}`)); ok {
		t.Fatal("a body with neither choices nor content is not a completion")
	}
	if _, ok := harnessAuthoredSpans([]byte(`{"input":"embedding"}`)); ok {
		t.Fatal("a body with neither messages nor system is not a chat request")
	}
}

func TestClaimSpanToolResultCaptureAndRead(t *testing.T) {
	broker := &inferenceBroker{sessions: map[string]*brokerSession{}}
	session := v13Session("case-a")
	broker.sessions["sess"] = session
	recordClaimSpanCompletionLocked(session, beginClaimSpanCompletionLocked(session, 0, ""),
		[]byte(`{"messages":[{"role":"user","content":"Tool result: outstanding balance 4110.67 USD"}]}`),
		[]byte(`{"choices":[{"message":{"content":"$4,110.67"}}]}`))
	broker.recordClaimSpanToolResult("sess", "case-a", []byte(`{"result":"outstanding balance 4110.67 USD"}`))
	// Errors and empty results carry no value.
	broker.recordClaimSpanToolResult("sess", "case-a", []byte(`{"error":"transient upstream error (503); retry"}`))
	broker.recordClaimSpanToolResult("sess", "case-a", nil)
	evidence, ok := broker.sessionClaimSpanEvidence("sess", "case-a")
	if !ok || !evidence.Complete || evidence.Ledger.ToolResults != 1 || !evidence.Ledger.ToolResult.Has("4110.67") {
		t.Fatalf("evidence = %+v ok=%v", evidence, ok)
	}
	residual := scoregates.ResidualHarnessTokens(evidence.Ledger.HarnessFirst, evidence.Ledger.ToolResult)
	if residual.Has("4110.67") {
		t.Fatal("a served tool result must exempt its value from the causal gate")
	}
	// The read is a private copy.
	evidence.Ledger.Completion.Add("tampered")
	again, _ := broker.sessionClaimSpanEvidence("sess", "case-a")
	if again.Ledger.Completion.Has("tampered") {
		t.Fatal("sessionClaimSpanEvidence must return a copy")
	}
	if _, ok := broker.sessionClaimSpanEvidence("sess", "unknown"); ok {
		t.Fatal("an unknown case has no evidence")
	}
	if _, ok := broker.sessionClaimSpanEvidence("missing", "case-a"); ok {
		t.Fatal("an unknown session has no evidence")
	}
	totals, ok := broker.sessionClaimSpanTotals("sess")
	if !ok || totals.Completions != 1 || totals.Unattributed != 0 {
		t.Fatalf("totals = %+v ok=%v", totals, ok)
	}
	// A v12 session exposes nothing.
	broker.sessions["old"] = &brokerSession{benchVersion: protocol.BenchVersionV12}
	if _, ok := broker.sessionClaimSpanEvidence("old", "case-a"); ok {
		t.Fatal("v12 session must expose no claim-span evidence")
	}
	broker.recordClaimSpanToolResult("old", "case-a", []byte(`{"result":"x 4110.67"}`))
	if broker.sessions["old"].claimSpanCases != nil {
		t.Fatal("v12 tool result must not allocate a ledger")
	}
}

func TestToolResultRecorderMirrorsBoundedPrefix(t *testing.T) {
	rec := httptest.NewRecorder()
	recorder := &toolResultRecorder{ResponseWriter: rec, limit: 8}
	recorder.WriteHeader(http.StatusOK)
	if _, err := recorder.Write([]byte("0123456789")); err != nil {
		t.Fatal(err)
	}
	if _, err := recorder.Write([]byte("abc")); err != nil {
		t.Fatal(err)
	}
	if recorder.status != http.StatusOK || recorder.body.String() != "01234567" {
		t.Fatalf("recorder status=%d body=%q", recorder.status, recorder.body.String())
	}
	// The harness still receives every byte.
	if rec.Body.String() != "0123456789abc" {
		t.Fatalf("passthrough body = %q", rec.Body.String())
	}
}
