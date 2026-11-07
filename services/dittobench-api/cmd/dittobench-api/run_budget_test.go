package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestRunBudgetAdmitsUntilThePricedCostReachesTheBudget(t *testing.T) {
	var prompt uint64
	b := &runBudget{benchVersion: protocol.BenchVersionV18, cases: 4,
		meter: func() scoregates.PricedCostUsage {
			return scoregates.PricedCostUsage{ChatComplete: true, PromptTokens: prompt}
		}}
	b.budget, _ = scoregates.RunBudgetNanoUSD(protocol.BenchVersionV18, 4)
	perCaseTokens := b.budget / 70 / 3 // three cases spend the whole budget
	admitted := 0
	for i := 0; i < 4; i++ {
		if b.admit() {
			admitted++
			prompt += perCaseTokens + 1
		} else {
			b.refuse(protocol.KindMemory)
		}
	}
	got := b.summary()
	if admitted != 3 || !got.Exhausted || got.AdmittedCases != 3 || got.UnrunCases != 1 || got.UnrunMemoryCases != 1 ||
		got.Reason != protocol.RunBudgetExhaustedReason || got.SpentNanoUSDAtCutoff < b.budget {
		t.Fatalf("admitted %d, summary %+v", admitted, got)
	}
	// Once exhausted it stays exhausted, whatever the meter says.
	prompt = 0
	if b.admit() {
		t.Fatal("budget re-opened")
	}
	var none *runBudget
	if !none.admit() || none.summary() != nil {
		t.Fatal("a nil budget must admit everything and report nothing")
	}
}

func TestBudgetExhaustedCaseIsNeverAnInfrastructureFailure(t *testing.T) {
	cs := budgetExhaustedCase("c1", "world-trip-current", protocol.KindMemory)
	if !cs.BudgetExhausted || cs.Undelivered || cs.ValidatorFault || cs.Score != 0 {
		t.Fatalf("%+v", cs)
	}
}

func TestNoRunBudgetBelowV18OrWithoutASession(t *testing.T) {
	s := &server{}
	if s.newRunBudget(protocol.BenchVersionV17, "session", 350) != nil || s.newRunBudget(protocol.BenchVersionV18, "", 350) != nil {
		t.Fatal("budget without a contract or a session")
	}
}
