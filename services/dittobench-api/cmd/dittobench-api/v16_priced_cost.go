package main

import (
	"math"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v16 priced inference cost factor (scoregates/priced_cost.go).
//
// The scorer prices the WHOLE run -- datagen half and generator slice -- at
// the contract's locked public per-token rates and multiplies the composite by
// the published curve's factor. The chat counts of a scored run are the
// whole-run relay interval (the same broker accounting relayRunResult audits
// for relay health), read deliberately BEFORE a dueling run narrows its
// accounting record to the datagen half; the datagen/slice boundary capture
// (captureDatagenSessionTotals) is not used. Embedding and decision-lane
// counts are the session's own totals, which only this run's harness spends.
//
// The factor rides in the composite gate: a dueling run carries it inside
// details.dueling.datagen_gate, so the validator and Platform folds that
// recompute the per-seed composite from details.dueling apply it without a
// fold change, and the column statistics a generator is judged by stay raw.

const v16PricedCostPostureEnv = "DITTOBENCH_V16_PRICED_COST_POSTURE"

// v16PricedCostPosture is enforce unless the operator sets the env to
// "shadow" (an emergency switch; see PROTOCOL.md for why the contract does
// not start in shadow).
var v16PricedCostPosture = scoregates.ParsePricedCostPosture(os.Getenv(v16PricedCostPostureEnv))

// brokerPricedUsage is one session's whole-run inference totals.
type brokerPricedUsage struct {
	promptTokens         uint64
	completionTokens     uint64
	usageUnavailable     uint64
	embeddingTokens      uint64
	decisionCalls        uint64
	decisionInputTokens  uint64
	decisionOutputTokens uint64
}

// sessionPricedUsage reads a session's totals. ok is false when the session
// does not exist.
func (b *inferenceBroker) sessionPricedUsage(id string) (brokerPricedUsage, bool) {
	if b == nil || id == "" {
		return brokerPricedUsage{}, false
	}
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return brokerPricedUsage{}, false
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	return brokerPricedUsage{
		promptTokens:         session.promptTokens,
		completionTokens:     session.completionTokens,
		usageUnavailable:     session.usageUnavailable,
		embeddingTokens:      session.embeddingTokens,
		decisionCalls:        session.decisionCompleted,
		decisionInputTokens:  session.decisionInputTokens,
		decisionOutputTokens: session.decisionOutputTokens,
	}, true
}

// relayUsageComplete is the chat-accounting trust bit for pricing: every
// successful completion carried provider usage.
func relayUsageComplete(usage protocol.TokenUsage) bool {
	return usage.AccountingVersion == 2 && usage.Status == "complete" &&
		usage.UsageAvailable == usage.Successes && usage.UsageUnavailable == 0
}

// v16PricedCostFor prices one run. wholeRun is the scored run's whole-run
// relay interval (nil off the scored path). cases is every scored case,
// datagen and slice. nil below bench 16.
func (s *server) v16PricedCostFor(benchVersion int, posture scoregates.PricedCostPosture, wholeRun *protocol.TokenUsage, sessionID string, cases int) *protocol.PricedCostSummary {
	if !protocol.PricedCostBenchVersion(benchVersion) {
		return nil
	}
	var usage scoregates.PricedCostUsage
	session, haveSession := s.broker.sessionPricedUsage(sessionID)
	if haveSession {
		usage.EmbeddingTokens = session.embeddingTokens
		usage.DecisionCalls = session.decisionCalls
		usage.DecisionInputTokens = session.decisionInputTokens
		usage.DecisionOutputTokens = session.decisionOutputTokens
	}
	switch {
	case wholeRun != nil:
		usage.Source = scoregates.PricedCostSourceRelay
		usage.ChatComplete = relayUsageComplete(*wholeRun)
		usage.PromptTokens = wholeRun.PromptTokens
		usage.CompletionTokens = wholeRun.CompletionTokens
	case haveSession:
		usage.Source = scoregates.PricedCostSourceBrokerSession
		usage.ChatComplete = session.usageUnavailable == 0
		usage.PromptTokens = session.promptTokens
		usage.CompletionTokens = session.completionTokens
	default:
		// No broker session (a practice run against a harness that talks to
		// its own provider): nothing was metered, so nothing is priced.
		usage.Source = scoregates.PricedCostSourceBrokerSession
	}
	return scoregates.BuildPricedCost(benchVersion, posture, cases, usage)
}

// applyPricedCostToHalf folds the applied factor into the datagen half's
// composite gate, which DuelingComposite multiplies across both halves.
func applyPricedCostToHalf(half scorer.DatagenHalf, summary *protocol.PricedCostSummary) scorer.DatagenHalf {
	half.Gate = half.Gate * scoregates.PricedCostMultiplier(summary)
	return half
}

// applyPricedCostToReport scales a non-dueling report's composite (a bench 16
// practice run without a slice), quantized to micros like every v9+
// composite, before the v9 base evidence binds it.
func applyPricedCostToReport(report protocol.ScoreReport, summary *protocol.PricedCostSummary) protocol.ScoreReport {
	m := scoregates.PricedCostMultiplier(summary)
	if m == 1 {
		return report
	}
	report.Composite = math.Round(report.Composite*m*1e6) / 1e6
	report.CompositeStderr = math.Round(report.CompositeStderr*m*1e6) / 1e6
	return report
}
