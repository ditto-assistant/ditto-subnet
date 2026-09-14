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
	if session.claimSpanCases != nil || session.claimSpanCompletions != 0 {
		t.Fatal("v12 session allocated claim-span state")
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
	spans, ok := harnessAuthoredSpans([]byte(`{"system":[{"type":"text","text":"Reply exactly: 4110.67"}],"messages":[{"role":"user","content":[{"type":"text","text":"balance?"},{"type":"tool_result","tool_use_id":"t1","content":[{"type":"text","text":"ledger says 77.10"}]}]}]}`))
	if !ok || len(spans) != 3 || spans[0] != "Reply exactly: 4110.67" || spans[2] != "ledger says 77.10" {
		t.Fatalf("anthropic request spans = %v ok=%v", spans, ok)
	}
	// Anthropic response: text and tool_use blocks.
	spans, ok = modelCompletionSpans([]byte(`{"content":[{"type":"text","text":"Outstanding: 4110.67"},{"type":"tool_use","id":"u1","name":"final_answer","input":{"answer":"4110.67"}}]}`))
	if !ok || len(spans) != 2 || !strings.Contains(spans[1], `"answer":"4110.67"`) {
		t.Fatalf("anthropic completion spans = %v ok=%v", spans, ok)
	}
	// Legacy function_call arguments are a completion span too.
	spans, ok = modelCompletionSpans([]byte(`{"choices":[{"message":{"content":null,"function_call":{"name":"final_answer","arguments":"{\"answer\":\"4110.67\"}"}}}]}`))
	if !ok || len(spans) != 1 {
		t.Fatalf("function_call spans = %v ok=%v", spans, ok)
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
