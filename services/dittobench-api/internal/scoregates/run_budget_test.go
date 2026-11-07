package scoregates

import "testing"

func TestRunBudgetIsTheFloorCostOfTheCurve(t *testing.T) {
	if _, ok := RunBudgetNanoUSD(BenchVersionV17, 350); ok {
		t.Fatal("v17 has no run budget")
	}
	for cases, want := range map[int]uint64{350: 1_000_000_000, 175: 500_000_000, 700: 2_000_000_000} {
		got, ok := RunBudgetNanoUSD(BenchVersionV18, cases)
		if !ok || got != want {
			t.Fatalf("v18 budget for %d cases = %d (%v), want %d", cases, got, ok, want)
		}
	}
	if _, ok := RunBudgetNanoUSD(BenchVersionV18, 0); ok {
		t.Fatal("an empty run has no budget")
	}
	// At exactly the budget the priced factor is on its floor.
	budget, _ := RunBudgetNanoUSD(BenchVersionV18, 350)
	usage := PricedCostUsage{ChatComplete: true, Source: PricedCostSourceRelay, PromptTokens: budget/70 + 1}
	summary := BuildPricedCost(BenchVersionV18, PricedCostEnforce, 350, usage)
	if summary.FactorBPS != pricedCostFloorForVersion(BenchVersionV18) {
		t.Fatalf("factor at the budget %d, want the floor", summary.FactorBPS)
	}
	if MeteredCostNanoUSD(BenchVersionV18, usage) != summary.CostNanoUSD {
		t.Fatal("meter disagrees with the priced-cost block")
	}
}
