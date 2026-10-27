package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestV14TwinAnswersReadTheHyphenFold is the twin half of #2734: at v14 the
// grader credits "VK-48HJXP6A63" and "VK‑48HJXP6A63" (U+2011) alike, so a
// counterfactual answering with the base answer in the other hyphen must
// still be counterfactual-insensitive. V13 compares the raw answers, as before.
func TestV14TwinAnswersReadTheHyphenFold(t *testing.T) {
	member := func(id, relation string) gen.StagedCase {
		return gen.StagedCase{
			Case:          protocol.MemoryCase{ID: id},
			V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "g1", Relation: relation},
		}
	}
	base, counterfactual := member("base", scorer.RelationBase), member("cf", scorer.RelationCausalCounterfactual)
	baseReply := protocol.RunResponse{Answer: "VK-48HJXP6A63"}
	cfReply := protocol.RunResponse{Answer: "VK‑48HJXP6A63"}
	scores := []protocol.CaseScore{{CaseID: "base", Kind: protocol.KindMemory, Score: 1}, {CaseID: "cf", Kind: protocol.KindMemory, Score: 1}}
	for _, tt := range []struct {
		version     int
		insensitive int
	}{
		{protocol.BenchVersionV13, 0},
		{protocol.BenchVersionV14, 1},
	} {
		evidence := map[string]scorer.TwinEvidence{
			"base": memoryTwinEvidence(tt.version, base, baseReply, nil),
			"cf":   memoryTwinEvidence(tt.version, counterfactual, cfReply, nil),
		}
		_, summary := scorer.ApplyV13TwinPostPass(scores, evidence, scorer.TwinPostPassConfig{}, tt.version)
		if summary.CounterfactualPairs != 1 || summary.CounterfactualInsensitive != tt.insensitive {
			t.Fatalf("v%d: pairs=%d insensitive=%d, want 1/%d", tt.version, summary.CounterfactualPairs, summary.CounterfactualInsensitive, tt.insensitive)
		}
	}

	tool := protocol.ToolCase{ID: "t", TwinGroup: "tg", TwinRelation: "decision_twin"}
	if a, b := toolTwinEvidence(protocol.BenchVersionV14, tool, baseReply, nil), toolTwinEvidence(protocol.BenchVersionV14, tool, cfReply, nil); a.Answer != b.Answer {
		t.Fatalf("v14 tool twin answers differ by hyphen only: %q vs %q", a.Answer, b.Answer)
	}
	if a, b := toolTwinEvidence(protocol.BenchVersionV13, tool, baseReply, nil), toolTwinEvidence(protocol.BenchVersionV13, tool, cfReply, nil); a.Answer == b.Answer {
		t.Fatal("v13 tool twin answers changed")
	}
}

// TestV14TwinAnswersReadTheHyphenJoinedValue: at v14 the grader reads
// "blue-purple" as "blue purple" for a case that knows that value, so a
// counterfactual answering the base value hyphen-joined is still
// counterfactual-insensitive. V13 compares the raw answers.
func TestV14TwinAnswersReadTheHyphenJoinedValue(t *testing.T) {
	base := gen.StagedCase{
		Case:          protocol.MemoryCase{ID: "base", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "blue purple"},
		V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "g1", Relation: scorer.RelationBase},
	}
	counterfactual := gen.StagedCase{
		Case:          protocol.MemoryCase{ID: "cf", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "leafy green", DistractorAnswers: []string{"blue purple"}},
		V10Provenance: &universe.V10CaseProvenance{MetamorphicGroup: "g1", Relation: scorer.RelationCausalCounterfactual},
	}
	scores := []protocol.CaseScore{{CaseID: "base", Kind: protocol.KindMemory, Score: 1}, {CaseID: "cf", Kind: protocol.KindMemory, Score: 1}}
	for _, tt := range []struct {
		version     int
		insensitive int
	}{
		{protocol.BenchVersionV13, 0},
		{protocol.BenchVersionV14, 1},
	} {
		evidence := map[string]scorer.TwinEvidence{
			"base": memoryTwinEvidence(tt.version, base, protocol.RunResponse{Answer: "blue purple"}, nil),
			"cf":   memoryTwinEvidence(tt.version, counterfactual, protocol.RunResponse{Answer: "blue-purple"}, nil),
		}
		_, summary := scorer.ApplyV13TwinPostPass(scores, evidence, scorer.TwinPostPassConfig{}, tt.version)
		if summary.CounterfactualInsensitive != tt.insensitive {
			t.Fatalf("v%d: insensitive=%d, want %d", tt.version, summary.CounterfactualInsensitive, tt.insensitive)
		}
	}
}
