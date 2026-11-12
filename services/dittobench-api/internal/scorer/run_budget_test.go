package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// A budget-refused case counts 0 in the means but never reaches the gates.
func TestBudgetRefusedCasesScoreZeroButSkipTheGates(t *testing.T) {
	run := []protocol.CaseScore{
		{CaseID: "t1", Kind: protocol.KindTool, Score: 1},
		{CaseID: "m1", Kind: protocol.KindMemory, Category: "conversational-chitchat", Score: 1, Correct: true},
	}
	refused := protocol.CaseScore{CaseID: "m2", Kind: protocol.KindMemory, Category: "conversational-chitchat", BudgetExhausted: true}
	withRefused := append(append([]protocol.CaseScore(nil), run...), refused)

	v18 := datagenHalf(withRefused, protocol.RunBudgetMinBenchVersion)
	if v18.MemoryMean != 0.5 {
		t.Fatalf("memory mean %v, want 0.5 (the refused case scores 0)", v18.MemoryMean)
	}
	if want := CompositeGateForVersion(run, protocol.RunBudgetMinBenchVersion); v18.Gate != want {
		t.Fatalf("gate %v read the refused case, want %v", v18.Gate, want)
	}
	if got := len(gatedPopulation(withRefused, protocol.RunBudgetMinBenchVersion-1)); got != len(withRefused) {
		t.Fatal("below v18 the gate population must be unchanged")
	}
}

// The published conversational sanity and the sibling comparisons read the
// gated population too: a refused member is neither a failed slice case nor
// a disagreeing sibling.
func TestBudgetRefusedCasesLeaveThePublishedFactors(t *testing.T) {
	v18 := protocol.RunBudgetMinBenchVersion
	run := []protocol.CaseScore{
		{CaseID: "m1", Kind: protocol.KindMemory, Category: "conversational-chitchat", Score: 1, Correct: true},
		{CaseID: "m2", Kind: protocol.KindMemory, Category: "conversational-chitchat", BudgetExhausted: true},
	}
	report := AggregateForVersion("r", run, v18)
	if report.ConversationalSanity == nil || *report.ConversationalSanity != 1 {
		t.Fatalf("conversational sanity %v read the refused case", report.ConversationalSanity)
	}
	group := persona.AuditTwinPrefix + "g1"
	pair := []protocol.CaseScore{
		{CaseID: "a", TwinGroup: group, AuditHalf: protocol.AuditHalfBase, Correct: true},
		{CaseID: "b", TwinGroup: group, AuditHalf: protocol.AuditHalfTransform, BudgetExhausted: true},
	}
	if ap := AuditPairs(pair); ap.Total() != 0 {
		t.Fatalf("a refused audit half was compared: %+v", ap)
	}
	twins := []protocol.CaseScore{
		{CaseID: "x", TwinGroup: "twin-1", Correct: true},
		{CaseID: "y", TwinGroup: "twin-1", BudgetExhausted: true},
	}
	if _, total := metamorphicGroupCounts(twins); total != 0 {
		t.Fatalf("a refused twin was compared (%d groups)", total)
	}
}

// A twin family that lost a member to the budget is dissolved in the gated
// population: the survivor stays (every other gate still reads it) but no
// family gate counts it as a one-member consistent group. Refused cases never
// enter the median latency.
func TestBudgetRefusedTwinDissolvesItsFamily(t *testing.T) {
	v18 := protocol.RunBudgetMinBenchVersion
	run := []protocol.CaseScore{
		{CaseID: "x", Kind: protocol.KindMemory, TwinGroup: "twin-1", Correct: true, Score: 1, LatencyMs: 900},
		{CaseID: "y", Kind: protocol.KindMemory, TwinGroup: "twin-1", BudgetExhausted: true},
		{CaseID: "z", Kind: protocol.KindMemory, TwinGroup: "twin-2", Correct: true, Score: 1, LatencyMs: 700},
		{CaseID: "w", Kind: protocol.KindMemory, TwinGroup: "twin-2", Correct: true, Score: 1, LatencyMs: 800},
	}
	gated := gatedPopulation(run, v18)
	if len(gated) != 3 || gated[0].CaseID != "x" || gated[0].TwinGroup != "" || gated[1].TwinGroup != "twin-2" {
		t.Fatalf("gated population %+v", gated)
	}
	if _, total := metamorphicGroupCounts(gated); total != 1 {
		t.Fatalf("%d twin groups measured, want only the intact one", total)
	}
	if run[0].TwinGroup != "twin-1" {
		t.Fatal("gatedPopulation mutated the report's cases")
	}
	if report := AggregateForVersion("r", run, v18); report.MedianMs != 800 {
		t.Fatalf("median latency %d read the refused case", report.MedianMs)
	}
}
