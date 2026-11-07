package scorer

import (
	"testing"

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
