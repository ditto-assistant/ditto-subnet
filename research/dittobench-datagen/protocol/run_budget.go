package protocol

// Bench v18 per-run cost budget.
//
// A canonical scoring run stops admitting cases once its priced cost reaches
// the run's budget. The budget is metered with the same locked public rates
// the priced-cost factor uses (details.priced_cost), so the two can never
// disagree about what a run cost. Every case not admitted scores 0 and carries
// CaseScore.BudgetExhausted. The budget covers the whole run: datagen half and
// generator slice, the priced-cost window.

// RunBudgetMinBenchVersion is the first contract whose canonical run carries a
// cost budget. It is a floor.
const RunBudgetMinBenchVersion = 18

// RunBudgetBenchVersion reports whether bench version v budgets its run.
func RunBudgetBenchVersion(v int) bool { return v >= RunBudgetMinBenchVersion }

// RunBudgetSummary is the details.run_budget block.
type RunBudgetSummary struct {
	ContractID string `json:"contract_id"`
	// BudgetNanoUSD is the run's budget: the per-350-case budget scaled to the
	// run's case count.
	BudgetNanoUSD uint64  `json:"budget_nano_usd"`
	BudgetUSD     float64 `json:"budget_usd"`
	// Cases is every case the run would administer (datagen and slice).
	Cases int `json:"cases"`
	// Exhausted is true when the run stopped admitting cases.
	Exhausted bool `json:"exhausted"`
	// AdmittedCases is how many cases started before the budget was reached;
	// UnrunCases = Cases - AdmittedCases, split by phase.
	AdmittedCases    int `json:"admitted_cases"`
	UnrunCases       int `json:"unrun_cases"`
	UnrunToolCases   int `json:"unrun_tool_cases"`
	UnrunMemoryCases int `json:"unrun_memory_cases"`
	UnrunSliceCases  int `json:"unrun_slice_cases"`
	// SpentNanoUSDAtCutoff is the metered cost when the first case was refused
	// (0 when the budget was never reached). Cases already running finish, so
	// the run's final priced cost can exceed the budget.
	SpentNanoUSDAtCutoff uint64 `json:"spent_nano_usd_at_cutoff"`
	// Reason is "run_budget_exhausted" when Exhausted.
	Reason string `json:"reason,omitempty"`
}

// RunBudgetExhaustedReason is the reason an unrun case and the summary carry.
const RunBudgetExhaustedReason = "run_budget_exhausted"
