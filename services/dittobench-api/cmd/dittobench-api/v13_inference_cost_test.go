package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

const twoChoiceCompletion = `{"choices":[{"message":{"content":"a"}},{"message":{"content":"b"}}],"usage":{"prompt_tokens":10,"completion_tokens":40}}`

const reasoningCompletion = `{"choices":[{"message":{"content":"a"}}],"usage":{"prompt_tokens":10,"completion_tokens":1500,"completion_tokens_details":{"reasoning_tokens":1200}}}`

func TestRecordInferenceCostLockedAttributesSerialAndCapabilityBoundCompletions(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13}
	// Serial /run: exactly one case in flight binds the completion exactly.
	session.runCases = map[string]int{"case-a": 1}
	recordInferenceCostLocked(session, 0, "", []byte(twoChoiceCompletion), true, 40, 0)
	recordInferenceCostLocked(session, 0, "", []byte(`{"choices":[{"message":{"content":"c"}}]}`), false, 0, 0)
	got := session.caseCosts["case-a"]
	if got.Completions != 2 || got.ChoicesTotal != 3 || got.CompletionTokens != 40 || got.UsageUnavailable != 1 || got.Attribution != scoregates.CostAttributionSerialRunCase {
		t.Fatalf("serial booking = %+v", got)
	}
	// Concurrent /run without a claim: two cases in flight -> unattributed,
	// nothing guessed.
	session.runCases["case-b"] = 1
	recordInferenceCostLocked(session, 0, "", []byte(twoChoiceCompletion), true, 40, 0)
	if session.caseCosts["case-a"].Completions != 2 || session.caseCosts["case-b"].Completions != 0 {
		t.Fatalf("overlapping completion was guessed onto a case: %+v", session.caseCosts)
	}
	if session.unattributedCost.Completions != 1 || session.unattributedCost.ChoicesTotal != 2 || session.unattributedCost.CompletionTokens != 40 {
		t.Fatalf("unattributed bucket = %+v", session.unattributedCost)
	}
	// Case-scoped capability route: the generation names the case even while
	// several /run cases overlap, and it outranks a serial attribution.
	session.caseIDs = map[string]uint64{"case-b": 7}
	recordInferenceCostLocked(session, 7, "", []byte(twoChoiceCompletion), true, 40, 0)
	if got := session.caseCosts["case-b"]; got.Completions != 1 || got.ChoicesTotal != 2 || got.Attribution != scoregates.CostAttributionCaseCapability {
		t.Fatalf("capability booking = %+v", got)
	}
	// An unparseable 2xx body is still one completion with one choice.
	session.runCases = map[string]int{"case-c": 1}
	recordInferenceCostLocked(session, 0, "", []byte(`not json`), false, 0, 0)
	if got := session.caseCosts["case-c"]; got.Completions != 1 || got.ChoicesTotal != 1 {
		t.Fatalf("unparseable body booking = %+v", got)
	}
}

// The verified-claim path is what keeps the ledger non-empty under the default
// concurrent /run: a harness that names its case on the completion
// (X-Ditto-Case-Id) is booked on that case when -- and only when -- the broker
// has the case in flight, mirroring the trace context's verified "claim".
func TestRecordInferenceCostLockedBooksVerifiedClaimsOnly(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{"case-a": 1, "case-b": 1}}
	// Verified: the claim names an in-flight case while two overlap.
	recordInferenceCostLocked(session, 0, "case-b", []byte(twoChoiceCompletion), true, 40, 0)
	got := session.caseCosts["case-b"]
	if got.Completions != 1 || got.ChoicesTotal != 2 || got.CompletionTokens != 40 || got.Attribution != scoregates.CostAttributionVerifiedClaim {
		t.Fatalf("verified claim booking = %+v", got)
	}
	if session.caseCosts["case-a"].Completions != 0 || session.unattributedCost.Completions != 0 {
		t.Fatalf("verified claim leaked elsewhere: cases=%+v unattributed=%+v", session.caseCosts, session.unattributedCost)
	}
	// Unverified: the claim names a case that is not in flight -> unattributed,
	// and no bucket is created for the claimed id.
	recordInferenceCostLocked(session, 0, "case-z", []byte(twoChoiceCompletion), true, 40, 0)
	if _, ok := session.caseCosts["case-z"]; ok {
		t.Fatalf("unverified claim created a bucket: %+v", session.caseCosts)
	}
	if session.unattributedCost.Completions != 1 || session.unattributedCost.CompletionTokens != 40 {
		t.Fatalf("unverified claim was not booked unattributed: %+v", session.unattributedCost)
	}
	// A completed case is no longer in flight, so a stale claim is unverified.
	session.runCases["case-b"] = 0
	recordInferenceCostLocked(session, 0, "case-b", []byte(twoChoiceCompletion), true, 40, 0)
	if session.caseCosts["case-b"].Completions != 1 || session.unattributedCost.Completions != 2 {
		t.Fatalf("stale claim was honored: cases=%+v unattributed=%+v", session.caseCosts, session.unattributedCost)
	}
	// The capability route outranks a verified claim that names another
	// in-flight case, and a bucket keeps its strongest attribution.
	session.runCases = map[string]int{"case-a": 1, "case-b": 1}
	session.caseIDs = map[string]uint64{"case-a": 9}
	recordInferenceCostLocked(session, 9, "case-b", []byte(twoChoiceCompletion), true, 40, 0)
	if got := session.caseCosts["case-a"]; got.Completions != 1 || got.Attribution != scoregates.CostAttributionCaseCapability {
		t.Fatalf("capability did not outrank the claim: %+v", got)
	}
	recordInferenceCostLocked(session, 0, "case-a", []byte(twoChoiceCompletion), true, 40, 0)
	if got := session.caseCosts["case-a"]; got.Completions != 2 || got.Attribution != scoregates.CostAttributionCaseCapability {
		t.Fatalf("bucket regressed from capability to claim: %+v", got)
	}
	// A serial window is outranked by a verified claim on the same bucket.
	session = &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{"case-s": 1}}
	recordInferenceCostLocked(session, 0, "", []byte(twoChoiceCompletion), true, 40, 0)
	recordInferenceCostLocked(session, 0, "case-s", []byte(twoChoiceCompletion), true, 40, 0)
	if got := session.caseCosts["case-s"]; got.Completions != 2 || got.Attribution != scoregates.CostAttributionVerifiedClaim {
		t.Fatalf("serial bucket did not upgrade to the verified claim: %+v", got)
	}
	if boundedHarnessCaseClaim("  case-x  ") != "case-x" || len(boundedHarnessCaseClaim(string(make([]byte, 300)))) != 128 {
		t.Fatal("claim bounding drifted from traceContextLocked")
	}
}

// Reasoning tokens are folded into completion_tokens by the provider on the
// agent-selected reasoning route; the ledger books only the answer output
// against the budget and carries the reasoning share separately.
func TestRecordInferenceCostLockedSeparatesReasoningTokens(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13, runCases: map[string]int{"case-a": 1}}
	recordInferenceCostLocked(session, 0, "", []byte(reasoningCompletion), true, 1500, 1200)
	got := session.caseCosts["case-a"]
	if got.CompletionTokens != 300 || got.ReasoningTokens != 1200 {
		t.Fatalf("reasoning split = %+v", got)
	}
	// A malformed provider count (reasoning > completion) is clamped, never
	// underflowed.
	recordInferenceCostLocked(session, 0, "", []byte(reasoningCompletion), true, 100, 500)
	got = session.caseCosts["case-a"]
	if got.CompletionTokens != 300 || got.ReasoningTokens != 1300 {
		t.Fatalf("clamp = %+v", got)
	}
	record := costRecord(got)
	if record.OutputTokens != 300 || record.ReasoningTokens != 1300 {
		t.Fatalf("record = %+v", record)
	}
	evidence := scoregates.BuildInferenceCost(protocol.BenchVersionV13, scoregates.CostClassMemory, record)
	if evidence.OutputTokens != 300 || evidence.ReasoningTokens != 1300 || evidence.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("an honest reasoning step read as over budget: %+v", evidence)
	}
}

func TestSessionInferenceCostReaders(t *testing.T) {
	broker := newInferenceBroker(4)
	id := "v13-cost-session"
	broker.sessions[id] = &brokerSession{id: id, benchVersion: protocol.BenchVersionV13,
		caseCosts:        map[string]brokerCaseCost{"case-a": {Completions: 3, ChoicesTotal: 9, CompletionTokens: 2000, Attribution: scoregates.CostAttributionSerialRunCase}},
		unattributedCost: brokerCaseCost{Completions: 2, ChoicesTotal: 2, CompletionTokens: 100, Attribution: scoregates.CostAttributionUnavailable},
	}
	record := broker.sessionInferenceCost(id, "case-a")
	if record == nil || record.Completions != 3 || record.ChoicesTotal != 9 || record.OutputTokens != 2000 || record.Attribution != scoregates.CostAttributionSerialRunCase {
		t.Fatalf("record = %+v", record)
	}
	if broker.sessionInferenceCost(id, "case-missing") != nil || broker.sessionInferenceCost("no-session", "case-a") != nil {
		t.Fatal("missing case or session produced a record")
	}
	if got := broker.sessionUnattributedInferenceCost(id); got.Completions != 2 || got.OutputTokens != 100 {
		t.Fatalf("unattributed = %+v", got)
	}
	s := &server{broker: broker}
	var execution runner.CaseExecution
	cs := s.applyV13InferenceCost(protocol.BenchVersionV13, id, protocol.CaseScore{CaseID: "case-a", Kind: protocol.KindMemory, Score: 0.75}, scoregates.CostClassMemory, &execution)
	wantFactor, _ := scoregates.CostFactorBPS(scoregates.CostClassMemory, 2000)
	if cs.InferenceCost == nil || !cs.InferenceCost.Attributed || cs.InferenceCost.FactorBPS != wantFactor || wantFactor >= scoregates.BasisPointScale || cs.Score != 0.75 {
		t.Fatalf("shadow evidence moved or missed the score: %+v (want factor %d)", cs, wantFactor)
	}
	if execution.InferenceCost != cs.InferenceCost {
		t.Fatal("transcript execution does not carry the same cost record")
	}
	unbound := s.applyV13InferenceCost(protocol.BenchVersionV13, id, protocol.CaseScore{CaseID: "case-z", Kind: protocol.KindTool}, scoregates.CostClassToolChain, nil)
	if unbound.InferenceCost == nil || unbound.InferenceCost.Attributed || unbound.InferenceCost.FactorBPS != scoregates.BasisPointScale {
		t.Fatalf("unbound case = %+v", unbound.InferenceCost)
	}
	if v12 := s.applyV13InferenceCost(protocol.BenchVersionV12, id, protocol.CaseScore{CaseID: "case-a"}, scoregates.CostClassMemory, &execution); v12.InferenceCost != nil {
		t.Fatalf("v12 case carries v13 cost evidence: %+v", v12)
	}
	summary := s.summarizeV13InferenceCost(protocol.BenchVersionV13, id, []protocol.CaseScore{cs, unbound})
	if summary == nil || summary.Applied || summary.Posture != "shadow" || summary.Cases != 2 || summary.AttributedCases != 1 || summary.UnattributedCompletions != 2 {
		t.Fatalf("summary = %+v", summary)
	}
	if summary.AttributedShare != 0.5 || summary.CasesByAttribution[scoregates.CostAttributionSerialRunCase] != 1 || summary.CasesByAttribution[scoregates.CostAttributionUnavailable] != 1 {
		t.Fatalf("attribution rails = %+v", summary)
	}
	if s.summarizeV13InferenceCost(protocol.BenchVersionV12, id, nil) != nil {
		t.Fatal("v12 produced a cost summary")
	}
}

func TestTwinEvidenceBuildersAreVersionGated(t *testing.T) {
	// A case that is BOTH a metamorphic program member and an as-of twin: the
	// two identities are different groups and must be carried separately, or
	// the twin lookup files the case under its program group (its real twin is
	// orphaned, its program siblings are mis-collected as twin members).
	staged := gen.StagedCase{
		Case:          protocol.MemoryCase{ID: "m1", TwinGroup: "tg", TwinRelation: protocol.TwinRelationAsOf},
		V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "mg", Relation: scorer.RelationCausalCounterfactual},
	}
	resp := protocol.RunResponse{Answer: "  1200 ", FinalText: "The balance is 1200."}
	if ev := memoryTwinEvidence(protocol.BenchVersionV12, staged, resp, nil); ev.Paired() || ev != (scorer.TwinEvidence{}) {
		t.Fatalf("v12 produced twin evidence: %+v", ev)
	}
	ev := memoryTwinEvidence(protocol.BenchVersionV13, staged, resp, nil)
	if ev.MetamorphicGroup != "mg" || ev.Relation != scorer.RelationCausalCounterfactual {
		t.Fatalf("metamorphic identity = %+v", ev)
	}
	if ev.TwinGroup != "tg" || ev.TwinRelation != protocol.TwinRelationAsOf {
		t.Fatalf("twin identity = %+v", ev)
	}
	if ev.Answer != "1200" || ev.Decision != scorer.DecisionAnswer || !ev.Paired() {
		t.Fatalf("memory evidence = %+v", ev)
	}
	// A plain v13 case with neither provenance nor twin relation stays out of
	// the evidence map.
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, gen.StagedCase{Case: protocol.MemoryCase{ID: "plain"}}, resp, nil); ev.Paired() {
		t.Fatalf("unpaired case produced evidence: %+v", ev)
	}
	// Program-only member: metamorphic identity set, twin identity empty.
	programOnly := gen.StagedCase{Case: protocol.MemoryCase{ID: "m0"}, V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "mg", Relation: scorer.RelationBase}}
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, programOnly, resp, nil); ev.MetamorphicGroup != "mg" || ev.TwinGroup != "" || ev.TwinRelation != "" {
		t.Fatalf("program-only evidence = %+v", ev)
	}
	// Twin-only member: twin identity set, metamorphic identity empty.
	twinOnly := gen.StagedCase{Case: protocol.MemoryCase{ID: "m2", TwinGroup: "tg", TwinRelation: protocol.TwinRelationDecision}}
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, twinOnly, protocol.RunResponse{Abstain: true}, nil); ev.TwinGroup != "tg" || ev.MetamorphicGroup != "" || ev.Decision != scorer.DecisionAbstain {
		t.Fatalf("twin-only evidence = %+v", ev)
	}
	// A v5+ phrasing twin (TwinGroup without a v13 TwinRelation) and a
	// provenance without a relation are both unpaired.
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, gen.StagedCase{Case: protocol.MemoryCase{ID: "m3", TwinGroup: "phrasing"}, V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "mg"}}, resp, nil); ev.Paired() {
		t.Fatalf("half-specified identities produced evidence: %+v", ev)
	}

	// Tool twins pair through the grader-only ToolCase.TwinGroup, never the
	// family Category.
	tool := protocol.ToolCase{ID: "t1", Category: "restraint_triplet", TwinGroup: "pair-7", TwinRelation: protocol.TwinRelationDecision}
	if ev := toolTwinEvidence(protocol.BenchVersionV12, tool, resp, nil); ev.Paired() {
		t.Fatalf("v12 tool produced twin evidence: %+v", ev)
	}
	act := []protocol.ObservedToolCall{{Name: "gmail_send"}}
	ev = toolTwinEvidence(protocol.BenchVersionV13, tool, resp, act)
	if ev.TwinGroup != "pair-7" || ev.TwinRelation != protocol.TwinRelationDecision || ev.Decision != scorer.DecisionAct || ev.MetamorphicGroup != "" {
		t.Fatalf("tool evidence = %+v", ev)
	}
	if ev.TwinGroup == tool.Category {
		t.Fatal("tool twin keyed on the family Category")
	}
	if ev := toolTwinEvidence(protocol.BenchVersionV13, protocol.ToolCase{ID: "t2", Category: "restraint_triplet", TwinRelation: protocol.TwinRelationDecision}, resp, act); ev.Paired() {
		t.Fatalf("relation without a TwinGroup produced evidence: %+v", ev)
	}
	if ev := toolTwinEvidence(protocol.BenchVersionV13, protocol.ToolCase{ID: "t3", Category: "web_search", TwinGroup: "pair-8"}, resp, act); ev.Paired() {
		t.Fatalf("TwinGroup without a relation produced evidence: %+v", ev)
	}
	if toolCostClass(protocol.ToolCase{ExpectedTools: []protocol.ToolSpec{{Name: "a"}, {Name: "b"}}}) != scoregates.CostClassToolChain || toolCostClass(protocol.ToolCase{}) != scoregates.CostClassSingleTool || memoryCostClass() != scoregates.CostClassMemory {
		t.Fatal("cost class helpers drifted")
	}
}
