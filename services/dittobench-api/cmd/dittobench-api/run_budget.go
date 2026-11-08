package main

import (
	"context"
	"log"
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
// interval. Both count the same completions at the same rates. A completion
// without provider usage is not in the counters, so the meter is a lower
// bound; the final priced-cost completeness check, not the meter, is what
// refuses an underpriced score. A session that vanishes mid-run (cancel or
// expiry) fails closed: no further case is admitted.
//
// Admission is index-ordered (runBoundedAdmitted): the dispatch loop asks for
// each case, in case order, before its worker starts. The admitted cases are
// therefore always a prefix of the dispatch order, whatever order the
// workers finish in, so every validator scores a comparable prefix.

type runBudget struct {
	benchVersion int
	budget       uint64
	cases        int
	// meter prices the session so far; ok=false means the session is gone.
	meter func() (usage scoregates.PricedCostUsage, ok bool)

	mu            sync.Mutex
	exhausted     bool
	admitted      int
	spentAtCutoff uint64
	meterLost     bool
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
		meter: func() (scoregates.PricedCostUsage, bool) {
			session, ok := s.broker.sessionPricedUsage(sessionID)
			if !ok {
				return scoregates.PricedCostUsage{}, false
			}
			return scoregates.PricedCostUsage{
				ChatComplete:         session.usageUnavailable == 0 && session.decisionUsageMissing == 0,
				PromptTokens:         session.promptTokens,
				CompletionTokens:     session.completionTokens,
				EmbeddingTokens:      session.embeddingTokens,
				DecisionCalls:        session.decisionCalls,
				DecisionInputTokens:  session.decisionInputTokens,
				DecisionOutputTokens: session.decisionOutputTokens,
			}, true
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
	usage, ok := b.meter()
	if !ok {
		// The broker session is gone: fail closed rather than price zero.
		if !b.meterLost {
			log.Printf("run budget: broker session vanished after %d admitted cases; refusing the rest", b.admitted)
		}
		b.exhausted, b.meterLost = true, true
		return false
	}
	if spent := scoregates.MeteredCostNanoUSD(b.benchVersion, usage); spent >= b.budget {
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

// admitter is the admission function runBoundedAdmitted asks, or nil when
// there is no budget (every case is admitted).
func (b *runBudget) admitter() func() bool {
	if b == nil {
		return nil
	}
	return b.admit
}

// runBoundedAdmitted is runBounded with index-ordered admission: the
// dispatch loop (one goroutine, cases in increasing order, after each slot is
// acquired) asks admit before case i's worker starts. runBudget refusals are
// sticky, so the admitted cases are a prefix of the dispatch order however
// the workers are scheduled. A refused case still runs fn with
// admitted=false, inside the same bound, to record its score and release its
// dependents. A nil admit admits everything: runBounded exactly.
func runBoundedAdmitted(ctx context.Context, n, concurrency int, admit func() bool, fn func(i int, admitted bool)) {
	var decide func(int) bool
	if admit != nil {
		decide = func(int) bool { return admit() }
	}
	runBoundedDispatch(ctx, n, concurrency, decide, fn)
}
