package main

import (
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

func TestGeneratedV13PairsReachTwinPostPass(t *testing.T) {
	p, _ := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	a, err := gen.GenerateDataset(11, p, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	var scores []protocol.CaseScore
	evidence := map[string]scorer.TwinEvidence{}
	for _, c := range a.MemoryCases {
		evidence[c.ID] = memoryTwinEvidence(13, gen.StagedCase{Case: c.MemoryCase, V10Provenance: c.V10Provenance}, protocol.RunResponse{FinalText: "constant answer"}, nil)
		scores = append(scores, protocol.CaseScore{CaseID: c.ID, Kind: protocol.KindMemory, Score: 1, Correct: true})
	}
	_, summary := scorer.ApplyV13TwinPostPass(scores, evidence, scorer.TwinPostPassConfig{}, 13)
	if summary.CounterfactualPairs != 19 || summary.TwinGroups != 31 {
		t.Fatalf("expected 13 program + 6 quantity pairs and 25 absence + 6 as-of pairs: %+v", summary)
	}
	if summary.CasesAffected != 100 {
		t.Fatalf("constant default affected %d/250 cases, want exact 40%% envelope", summary.CasesAffected)
	}
	mix, err := gen.AuditMix(a)
	if err != nil {
		t.Fatal(err)
	}
	if mix.GateExposedWeight != float64(summary.CasesAffected) {
		t.Fatalf("audit/scorer exposure disagree: %.0f / %d", mix.GateExposedWeight, summary.CasesAffected)
	}
}
