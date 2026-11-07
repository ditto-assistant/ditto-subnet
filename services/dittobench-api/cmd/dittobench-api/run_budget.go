package main

import (
	"sync"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v18 per-run cost budget (protocol/run_budget.go,
// scoregates/run_budget.go).
//
// Before each case starts -- tool, memory and generator slice alike, in the
// run's deterministic dispatch order -- the meter prices the run's broker
// session so far at the locked priced-cost rates. Once that reaches the
// budget, no further case is admitted: each refused case scores 0 and carries
// CaseScore.BudgetExhausted. Cases already running finish, so the run's final
// priced cost can sit a little above the budget. The run itself completes
// normally: a spent budget is the harness's own cost, never an infrastructure
// failure, so nothing here fails the run, marks a case Undelivered, or asks
// for a retry.
//
// The meter reads the broker session counters, which settle as each
// completion returns; the priced-cost factor reads the relay's whole-run
// interval. Both count the same completions at the same rates.

type runBudget struct {
	benchVersion int
	budget       uint64
	cases        int
	meter        func() scoregates.PricedCostUsage

	mu            sync.Mutex
	exhausted     bool
	admitted      int
	spentAtCutoff uint64
	unrunTool     int
	unrunMemory   int
	unrunSlice    int
}

// newRunBudget returns the run's budget, or nil when the contract has none or
// nothing is metered (a practice run without a broker session).
func (s *server) newRunBudget(benchVersion int, sessionID string, cases int) *runBudget {
	budget, ok := scoregates.RunBudgetNanoUSD(benchVersion, cases)
	if !ok || s.broker == nil || sessionID == "" {
		return nil
	}
	if _, ok := s.broker.sessionPricedUsage(sessionID); !ok {
		return nil
	}
	return &runBudget{
		benchVersion: benchVersion, budget: budget, cases: cases,
		meter: func() scoregates.PricedCostUsage {
			session, _ := s.broker.sessionPricedUsage(sessionID)
			return scoregates.PricedCostUsage{
				ChatComplete:         true,
				PromptTokens:         session.promptTokens,
				CompletionTokens:     session.completionTokens,
				EmbeddingTokens:      session.embeddingTokens,
				DecisionCalls:        session.decisionCalls,
				DecisionInputTokens:  session.decisionInputTokens,
				DecisionOutputTokens: session.decisionOutputTokens,
			}
		},
	}
}

// admit reports whether the next case may start. A nil budget admits every
// case.
func (b *runBudget) admit() bool {
	if b == nil {
		return true
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.exhausted {
		return false
	}
	if spent := scoregates.MeteredCostNanoUSD(b.benchVersion, b.meter()); spent >= b.budget {
		b.exhausted, b.spentAtCutoff = true, spent
		return false
	}
	b.admitted++
	return true
}

// refuse records one refused case of a phase.
func (b *runBudget) refuse(kind string) {
	if b == nil {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	switch kind {
	case protocol.KindTool:
		b.unrunTool++
	case "slice":
		b.unrunSlice++
	default:
		b.unrunMemory++
	}
}

// summary is the details.run_budget block (nil without a budget).
func (b *runBudget) summary() *protocol.RunBudgetSummary {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	out := &protocol.RunBudgetSummary{
		ContractID:    scoregates.RunBudgetContractID,
		BudgetNanoUSD: b.budget, BudgetUSD: float64(b.budget) / 1e9,
		Cases: b.cases, Exhausted: b.exhausted, AdmittedCases: b.admitted,
		UnrunToolCases: b.unrunTool, UnrunMemoryCases: b.unrunMemory, UnrunSliceCases: b.unrunSlice,
		SpentNanoUSDAtCutoff: b.spentAtCutoff,
	}
	out.UnrunCases = out.UnrunToolCases + out.UnrunMemoryCases + out.UnrunSliceCases
	if b.exhausted {
		out.Reason = protocol.RunBudgetExhaustedReason
	}
	return out
}

// budgetExhaustedCase is the score of a case the budget refused.
func budgetExhaustedCase(caseID, category, kind string) protocol.CaseScore {
	return protocol.CaseScore{
		CaseID: caseID, Category: category, Kind: kind, Score: 0,
		BudgetExhausted: true,
		Called:          []string{},
		Expected:        []string{},
		Notes:           []string{protocol.RunBudgetExhaustedReason + ": the run's priced cost reached its budget before this case started; scored 0"},
	}
}
