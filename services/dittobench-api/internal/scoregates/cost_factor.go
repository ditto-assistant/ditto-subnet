package scoregates

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 per-case inference cost factor (issue #1850).
//
// Extra completions were free beyond the tie-break efficiency fold, so a
// voting / re-ask / planner stack (adversary N4) lost nothing. Counting
// requests alone misses `n` sampling (one request, five choices) and
// single-completion self-consistency (five answers in one long completion), so
// the factor is defined over OUTPUT TOKENS of successful completions, with the
// sampled choices recorded alongside:
//
//	cost_factor = clamp(1 - alpha * max(0, tokens_out - budget_c), 0.6, 1)
//
// budget_c is published per case class and sized so plan -> call -> observe ->
// answer plus one LLM tool-router completion sits inside it: at least three
// completion-equivalents for a memory or single-tool case and five for a tool
// chain. alpha is fixed per class at (1 - floor) / budget_c, so the factor
// reaches its floor exactly when a case spends twice its budget.
//
// The factor is SHADOW ONLY in v13.0: it is computed here as a pure function,
// reported per case and per run by the scorer, and never multiplied into a
// composite. It is also kept out of the signed score-gate Evidence root until
// the enforce decision (#1521 must first show honest ReAct and LLM-router loops
// unaffected), so the v13 evidence bytes the Platform validates do not change
// shape for a rule that cannot yet move a score. Provider failures and 5xx
// retries are excluded by construction: the broker books only 2xx completions.

// CostCaseClass names the budget class of a case.
type CostCaseClass string

const (
	// CostClassMemory: a memory case (retrieve, reason, answer).
	CostClassMemory CostCaseClass = "memory"
	// CostClassSingleTool: a tool case expecting at most one tool execution.
	CostClassSingleTool CostCaseClass = "single_tool"
	// CostClassToolChain: a tool case expecting two or more executions.
	CostClassToolChain CostCaseClass = "tool_chain"
)

const (
	// CostFactorFloorBPS is the deepest the factor can fall (0.6).
	CostFactorFloorBPS = 6000
	// CostCompletionEquivalentTokens is the published output-token size of one
	// completion-equivalent: one plan/call/observe/answer step of a ReAct loop
	// on the locked model, with headroom for a verbose tool-call payload.
	CostCompletionEquivalentTokens uint64 = 512
	// Published per-class budgets in completion-equivalents.
	CostBudgetCompletionsMemory     = 3
	CostBudgetCompletionsSingleTool = 3
	CostBudgetCompletionsToolChain  = 5
)

// Attribution values for InferenceCostEvidence.Attribution.
const (
	// CostAttributionCaseCapability: the harness routed the completions through
	// its case-scoped inference capability, so the binding is exact.
	CostAttributionCaseCapability = "case_capability"
	// CostAttributionSerialRunCase: exactly one /run case was in flight on the
	// session when the completion was booked, so the binding is exact.
	CostAttributionSerialRunCase = "serial_run_case"
	// CostAttributionUnavailable: the completions overlapped several in-flight
	// cases (concurrent /run) or no broker session existed; nothing is guessed
	// onto the case and the run summary carries the unattributed totals.
	CostAttributionUnavailable = "unattributed"
)

// CostCaseClassFor derives the budget class from a case kind and its expected
// tool execution count.
func CostCaseClassFor(kind string, expectedTools int) CostCaseClass {
	if kind != protocol.KindTool {
		return CostClassMemory
	}
	if expectedTools >= 2 {
		return CostClassToolChain
	}
	return CostClassSingleTool
}

// CostBudgetCompletions returns the published completion-equivalent budget for
// a class; unknown classes fall back to the memory budget.
func CostBudgetCompletions(class CostCaseClass) int {
	switch class {
	case CostClassToolChain:
		return CostBudgetCompletionsToolChain
	case CostClassSingleTool:
		return CostBudgetCompletionsSingleTool
	default:
		return CostBudgetCompletionsMemory
	}
}

// CostBudgetTokens is the class budget in output tokens.
func CostBudgetTokens(class CostCaseClass) uint64 {
	return uint64(CostBudgetCompletions(class)) * CostCompletionEquivalentTokens
}

// CostBudgets publishes every class budget in a fixed order.
func CostBudgets() []protocol.InferenceCostBudget {
	out := make([]protocol.InferenceCostBudget, 0, 3)
	for _, class := range []CostCaseClass{CostClassMemory, CostClassSingleTool, CostClassToolChain} {
		out = append(out, protocol.InferenceCostBudget{
			Class: string(class), Completions: CostBudgetCompletions(class), OutputTokens: CostBudgetTokens(class),
		})
	}
	return out
}

// CostFactorBPS applies the published rule to one case's output tokens: full
// factor at or under budget, linear decay above it, floored at
// CostFactorFloorBPS once the excess reaches the budget. It returns the excess
// alongside so a report can show how far over budget the case ran.
func CostFactorBPS(class CostCaseClass, outputTokens uint64) (factorBPS int, excess uint64) {
	budget := CostBudgetTokens(class)
	if outputTokens <= budget {
		return BasisPointScale, 0
	}
	excess = outputTokens - budget
	if excess >= budget {
		return CostFactorFloorBPS, excess
	}
	// alpha = (1 - floor) / budget; penalty = alpha * excess, in basis points.
	penalty := uint64(BasisPointScale-CostFactorFloorBPS) * excess / budget
	return BasisPointScale - int(penalty), excess
}

// InferenceCostRecord is the trusted broker's raw per-case booking before the
// rule is applied.
type InferenceCostRecord struct {
	Completions      int
	ChoicesTotal     int
	OutputTokens     uint64
	UsageUnavailable int
	Attribution      string
}

// BuildInferenceCost applies the rule to one booking for bench_version >= 13.
// It returns nil for every earlier version (the frozen contracts carry no cost
// record) and a full-factor record with CostAttributionUnavailable when the
// completions could not be bound to the case.
func BuildInferenceCost(benchVersion int, class CostCaseClass, record *InferenceCostRecord) *protocol.InferenceCostEvidence {
	if benchVersion < BenchVersionV13 {
		return nil
	}
	evidence := &protocol.InferenceCostEvidence{
		Class:        string(class),
		BudgetTokens: CostBudgetTokens(class),
		FactorBPS:    BasisPointScale,
		Attribution:  CostAttributionUnavailable,
	}
	if record == nil || record.Attribution == "" || record.Attribution == CostAttributionUnavailable {
		return evidence
	}
	evidence.Attributed = true
	evidence.Attribution = record.Attribution
	evidence.Completions = record.Completions
	evidence.ChoicesTotal = record.ChoicesTotal
	evidence.OutputTokens = record.OutputTokens
	evidence.UsageUnavailable = record.UsageUnavailable
	evidence.FactorBPS, evidence.ExcessTokens = CostFactorBPS(class, record.OutputTokens)
	return evidence
}

// SummarizeInferenceCost folds the per-case records into the run-level shadow
// summary. unattributed carries the completions the broker booked on the
// session but could not bind to a case. nil for bench_version < 13.
func SummarizeInferenceCost(benchVersion int, perCase []protocol.CaseScore, unattributed InferenceCostRecord) *protocol.InferenceCostSummary {
	if benchVersion < BenchVersionV13 {
		return nil
	}
	summary := &protocol.InferenceCostSummary{
		Posture:                    "shadow",
		Applied:                    false,
		CompletionEquivalentTokens: CostCompletionEquivalentTokens,
		FloorBPS:                   CostFactorFloorBPS,
		Budgets:                    CostBudgets(),
		UnattributedCompletions:    unattributed.Completions,
		UnattributedChoices:        unattributed.ChoicesTotal,
		UnattributedOutputTokens:   unattributed.OutputTokens,
		MeanFactorBPS:              BasisPointScale,
	}
	factorSum := 0
	for _, cs := range perCase {
		if cs.InferenceCost == nil {
			continue
		}
		summary.Cases++
		factorSum += cs.InferenceCost.FactorBPS
		if cs.InferenceCost.FactorBPS < BasisPointScale {
			summary.CasesBelowFullFactor++
		}
		if !cs.InferenceCost.Attributed {
			continue
		}
		summary.AttributedCases++
		summary.Completions += cs.InferenceCost.Completions
		summary.ChoicesTotal += cs.InferenceCost.ChoicesTotal
		summary.OutputTokens += cs.InferenceCost.OutputTokens
	}
	if summary.Cases > 0 {
		summary.MeanFactorBPS = factorSum / summary.Cases
	}
	return summary
}

// ValidateInferenceCost checks a per-case record is internally consistent (a
// report-side invariant test; nothing here is signed).
func ValidateInferenceCost(e protocol.InferenceCostEvidence) error {
	if e.FactorBPS < CostFactorFloorBPS || e.FactorBPS > BasisPointScale {
		return invalid("inference cost factor %d outside [%d, %d]", e.FactorBPS, CostFactorFloorBPS, BasisPointScale)
	}
	if !e.Attributed && (e.Completions != 0 || e.OutputTokens != 0 || e.FactorBPS != BasisPointScale) {
		return invalid("unattributed inference cost carries case bookings")
	}
	if e.Attributed && e.ChoicesTotal < e.Completions {
		return invalid("inference cost choices %d below completions %d", e.ChoicesTotal, e.Completions)
	}
	if want, _ := CostFactorBPS(CostCaseClass(e.Class), e.OutputTokens); e.Attributed && want != e.FactorBPS {
		return fmt.Errorf("inference cost factor %d does not match the published rule (%d)", e.FactorBPS, want)
	}
	return nil
}
