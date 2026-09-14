package main

import (
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 twin evidence assembly (issue #1835). The post-pass in
// internal/scorer needs, per case, the group identities and relations the
// generator assigned plus the harness's asserted answer and decision class;
// none of that is on the CaseScore and none of it crosses the harness wire.
// Both builders return the zero value (!Paired()) for bench_version < 13, so
// the caller's evidence map stays empty and the post-pass is the identity.

// memoryTwinEvidence reads the staged case's generator provenance. A
// metamorphic program member pairs through V10CaseProvenance.MetamorphicGroup
// (the counterfactual member deliberately carries no TwinGroup); a v13
// decision/as-of twin pairs through its own TwinGroup. The two identities are
// carried separately -- a case can be both, and its program group is never its
// twin group.
func memoryTwinEvidence(benchVersion int, sc gen.StagedCase, graded protocol.RunResponse, observed []protocol.ObservedToolCall) scorer.TwinEvidence {
	if benchVersion < protocol.BenchVersionV13 {
		return scorer.TwinEvidence{}
	}
	ev := scorer.TwinEvidence{
		Answer:   scorer.AssertedAnswer(graded),
		Decision: scorer.ClassifyDecision(graded, observed),
	}
	if sc.V10Provenance != nil && sc.V10Provenance.MetamorphicGroup != "" && sc.V10Provenance.Relation != "" {
		ev.MetamorphicGroup = sc.V10Provenance.MetamorphicGroup
		ev.Relation = sc.V10Provenance.Relation
	}
	if sc.Case.TwinRelation != "" && sc.Case.TwinGroup != "" {
		ev.TwinGroup = sc.Case.TwinGroup
		ev.TwinRelation = sc.Case.TwinRelation
	}
	if !ev.Paired() {
		return scorer.TwinEvidence{}
	}
	return ev
}

// toolTwinEvidence pairs a v13 tool decision twin through the grader-only
// ToolCase.TwinGroup (a pair identity the generator assigns, never serialized).
// Category is a family label shared by every case of a family in the run, so
// it can never be the pairing key: keyed on it, a whole family concordant on
// decision class would zero together under R1 and multiply N scores under R2.
// A case with a relation but no group (or the reverse) is unpaired.
func toolTwinEvidence(benchVersion int, c protocol.ToolCase, resp protocol.RunResponse, observed []protocol.ObservedToolCall) scorer.TwinEvidence {
	if benchVersion < protocol.BenchVersionV13 || c.TwinRelation == "" || c.TwinGroup == "" {
		return scorer.TwinEvidence{}
	}
	return scorer.TwinEvidence{
		TwinGroup:    c.TwinGroup,
		TwinRelation: c.TwinRelation,
		Answer:       scorer.AssertedAnswer(resp),
		Decision:     scorer.ClassifyDecision(resp, observed),
	}
}

// toolCostClass / memoryCostClass name the published v13 cost budget class of
// a case (issue #1850).
func toolCostClass(c protocol.ToolCase) scoregates.CostCaseClass {
	return scoregates.CostCaseClassFor(protocol.KindTool, len(c.ExpectedTools))
}

func memoryCostClass() scoregates.CostCaseClass {
	return scoregates.CostClassMemory
}
