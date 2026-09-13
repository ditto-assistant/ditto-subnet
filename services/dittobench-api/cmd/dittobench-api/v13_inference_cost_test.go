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

func TestRecordInferenceCostLockedAttributesSerialAndCapabilityBoundCompletions(t *testing.T) {
	session := &brokerSession{benchVersion: protocol.BenchVersionV13}
	// Serial /run: exactly one case in flight binds the completion exactly.
	session.runCases = map[string]int{"case-a": 1}
	recordInferenceCostLocked(session, 0, []byte(twoChoiceCompletion), true, 40)
	recordInferenceCostLocked(session, 0, []byte(`{"choices":[{"message":{"content":"c"}}]}`), false, 0)
	got := session.caseCosts["case-a"]
	if got.Completions != 2 || got.ChoicesTotal != 3 || got.CompletionTokens != 40 || got.UsageUnavailable != 1 || got.Attribution != scoregates.CostAttributionSerialRunCase {
		t.Fatalf("serial booking = %+v", got)
	}
	// Concurrent /run: two cases in flight -> unattributed, nothing guessed.
	session.runCases["case-b"] = 1
	recordInferenceCostLocked(session, 0, []byte(twoChoiceCompletion), true, 40)
	if session.caseCosts["case-a"].Completions != 2 || session.caseCosts["case-b"].Completions != 0 {
		t.Fatalf("overlapping completion was guessed onto a case: %+v", session.caseCosts)
	}
	if session.unattributedCost.Completions != 1 || session.unattributedCost.ChoicesTotal != 2 || session.unattributedCost.CompletionTokens != 40 {
		t.Fatalf("unattributed bucket = %+v", session.unattributedCost)
	}
	// Case-scoped capability route: the generation names the case even while
	// several /run cases overlap, and it outranks a serial attribution.
	session.caseIDs = map[string]uint64{"case-b": 7}
	recordInferenceCostLocked(session, 7, []byte(twoChoiceCompletion), true, 40)
	if got := session.caseCosts["case-b"]; got.Completions != 1 || got.ChoicesTotal != 2 || got.Attribution != scoregates.CostAttributionCaseCapability {
		t.Fatalf("capability booking = %+v", got)
	}
	// An unparseable 2xx body is still one completion with one choice.
	session.runCases = map[string]int{"case-c": 1}
	recordInferenceCostLocked(session, 0, []byte(`not json`), false, 0)
	if got := session.caseCosts["case-c"]; got.Completions != 1 || got.ChoicesTotal != 1 {
		t.Fatalf("unparseable body booking = %+v", got)
	}
}

func TestRecordInferenceCostLockedIsVersionGatedAndSkipsConfirmation(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV12} {
		session := &brokerSession{benchVersion: version, runCases: map[string]int{"case-a": 1}}
		recordInferenceCostLocked(session, 0, []byte(twoChoiceCompletion), true, 40)
		if len(session.caseCosts) != 0 || session.unattributedCost.Completions != 0 {
			t.Fatalf("v%d booked a cost: %+v", version, session)
		}
	}
	session := &brokerSession{benchVersion: protocol.BenchVersionV13, confirmationSession: true, runCases: map[string]int{"case-a": 1}}
	recordInferenceCostLocked(session, 0, []byte(twoChoiceCompletion), true, 40)
	if len(session.caseCosts) != 0 || session.unattributedCost.Completions != 0 {
		t.Fatalf("confirmation reader traffic was booked as harness cost: %+v", session)
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
	if s.summarizeV13InferenceCost(protocol.BenchVersionV12, id, nil) != nil {
		t.Fatal("v12 produced a cost summary")
	}
}

func TestTwinEvidenceBuildersAreVersionGated(t *testing.T) {
	staged := gen.StagedCase{
		Case:          protocol.MemoryCase{ID: "m1", TwinGroup: "tg", TwinRelation: protocol.TwinRelationAsOf},
		V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "mg", Relation: scorer.RelationCausalCounterfactual},
	}
	resp := protocol.RunResponse{Answer: "  1200 ", FinalText: "The balance is 1200."}
	if ev := memoryTwinEvidence(protocol.BenchVersionV12, staged, resp, nil); ev.Group != "" {
		t.Fatalf("v12 produced twin evidence: %+v", ev)
	}
	ev := memoryTwinEvidence(protocol.BenchVersionV13, staged, resp, nil)
	if ev.Group != "mg" || ev.Relation != scorer.RelationCausalCounterfactual || ev.TwinRelation != protocol.TwinRelationAsOf || ev.Answer != "1200" || ev.Decision != scorer.DecisionAnswer {
		t.Fatalf("memory evidence = %+v", ev)
	}
	// A plain v13 case with neither provenance nor twin relation stays out of
	// the evidence map.
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, gen.StagedCase{Case: protocol.MemoryCase{ID: "plain"}}, resp, nil); ev.Group != "" {
		t.Fatalf("unpaired case produced evidence: %+v", ev)
	}
	twinOnly := gen.StagedCase{Case: protocol.MemoryCase{ID: "m2", TwinGroup: "tg", TwinRelation: protocol.TwinRelationDecision}}
	if ev := memoryTwinEvidence(protocol.BenchVersionV13, twinOnly, protocol.RunResponse{Abstain: true}, nil); ev.Group != "tg" || ev.Decision != scorer.DecisionAbstain {
		t.Fatalf("twin-only evidence = %+v", ev)
	}
	tool := protocol.ToolCase{ID: "t1", Category: "restraint-pair", TwinRelation: protocol.TwinRelationDecision}
	if ev := toolTwinEvidence(protocol.BenchVersionV12, tool, resp, nil); ev.Group != "" {
		t.Fatalf("v12 tool produced twin evidence: %+v", ev)
	}
	act := []protocol.ObservedToolCall{{Name: "gmail_send"}}
	if ev := toolTwinEvidence(protocol.BenchVersionV13, tool, resp, act); ev.Group != "restraint-pair" || ev.TwinRelation != protocol.TwinRelationDecision || ev.Decision != scorer.DecisionAct {
		t.Fatalf("tool evidence = %+v", ev)
	}
	if ev := toolTwinEvidence(protocol.BenchVersionV13, protocol.ToolCase{ID: "t2", Category: "web_search"}, resp, act); ev.Group != "" {
		t.Fatalf("untwinned tool case produced evidence: %+v", ev)
	}
	if toolCostClass(protocol.ToolCase{ExpectedTools: []protocol.ToolSpec{{Name: "a"}, {Name: "b"}}}) != scoregates.CostClassToolChain || toolCostClass(protocol.ToolCase{}) != scoregates.CostClassSingleTool || memoryCostClass() != scoregates.CostClassMemory {
		t.Fatal("cost class helpers drifted")
	}
}
