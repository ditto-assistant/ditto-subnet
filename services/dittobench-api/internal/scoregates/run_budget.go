package scoregates

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// Bench v18 per-run cost budget (protocol/run_budget.go).
//
// The budget is the cost at which the priced-cost factor reaches its floor:
// the curve's last breakpoint ratio times the reference, scaled from the
// 350-case normalization run to the run's own case count. Spending beyond it
// can no longer lower the factor, so v18 stops admitting cases there instead
// and scores the rest 0. For priced-cost-v2 that is 2.0 x $0.50 = $1.00 per
// 350-case run. Deriving it from the curve means a later cost contract moves
// the budget with it.

// RunBudgetContractID names the v18 budget rule.
const RunBudgetContractID = "run-budget-v1"

// RunBudgetNanoUSD returns the budget for a run of cases cases, or false below
// protocol.RunBudgetMinBenchVersion or for an empty run.
func RunBudgetNanoUSD(benchVersion, cases int) (uint64, bool) {
	if !protocol.RunBudgetBenchVersion(benchVersion) || !protocol.PricedCostBenchVersion(benchVersion) || cases <= 0 {
		return 0, false
	}
	curve := pricedCostCurveForVersion(benchVersion)
	floorRatio := uint64(curve[len(curve)-1].RatioBPS)
	reference := pricedCostReferenceForVersion(benchVersion)
	perNormalizedRun := satMul(reference, floorRatio) / BasisPointScale
	return satMul(perNormalizedRun, uint64(cases)) / PricedCostNormalizationCases, true
}

// MeteredCostNanoUSD prices usage at the contract's locked rates exactly as
// BuildPricedCost does, so the budget meter and the cost factor always agree.
func MeteredCostNanoUSD(benchVersion int, usage PricedCostUsage) uint64 {
	summary := BuildPricedCost(benchVersion, PricedCostEnforce, 1, usage)
	if summary == nil {
		return 0
	}
	return summary.CostNanoUSD
}
