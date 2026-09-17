package main

import (
	"encoding/json"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 per-case inference cost ledger (issue #1850). The broker books
// every SUCCESSFUL chat completion (2xx with a body; provider failures and 5xx
// retries never reach this path) on the /run case it can bind the completion
// to, strongest binding first:
//
//   - a case-scoped capability route (the harness advertised
//     case_scoped_inference_v1 and called /cases/<token>/...): broker-exact;
//   - a VERIFIED harness claim: the completion carried X-Ditto-Case-Id naming
//     a case the broker itself has in flight on this session -- the same
//     verified "claim" path the trace context files (traceContextLocked). This
//     is what makes the ledger non-empty under the default concurrent /run for
//     the starter kit and every harness that sends the header; the booking is
//     labeled verified_claim so calibration can separate self-declared from
//     broker-bound attribution. A claim naming a case that is NOT in flight is
//     never booked onto anything;
//   - a serial /run window: exactly one ordinary case in flight on the session.
//
// Completions that overlap several in-flight cases with no verified claim are
// booked unattributed and reported at run level; nothing is guessed onto a
// case. The ledger is shadow evidence only: the scorer reports the factor per
// case and per run (scoregates.BuildInferenceCost) and never multiplies it
// into a composite in v13.0.

// brokerCaseCost is one attribution bucket of the v13 cost ledger.
// CompletionTokens is the ANSWER output (provider completion_tokens minus the
// provider-reported reasoning tokens); ReasoningTokens carries the remainder.
type brokerCaseCost struct {
	Completions      uint64
	ChoicesTotal     uint64
	CompletionTokens uint64
	ReasoningTokens  uint64
	UsageUnavailable uint64
	Attribution      string
}

// recordInferenceCostLocked books one successful completion. The caller holds
// session.mu. claimedCaseID is the harness's X-Ditto-Case-Id (already bounded
// by the caller; "" when absent). reasoningTokens must already be clamped to
// completionTokens. No-op for bench_version<13 and for confirmation sessions,
// whose reader completions are LongMem instrument traffic, not harness cost.
func recordInferenceCostLocked(session *brokerSession, caseGeneration uint64, claimedCaseID string, responseBody []byte, usageOK bool, completionTokens, reasoningTokens uint64) {
	if session.benchVersion < protocol.BenchVersionV13 || session.confirmationSession {
		return
	}
	choices := decodeChoiceCount(responseBody)
	caseID, attribution := inferenceCostAttributionLocked(session, caseGeneration, claimedCaseID)
	if caseID == "" {
		bookInferenceCost(&session.unattributedCost, scoregates.CostAttributionUnavailable, choices, usageOK, completionTokens, reasoningTokens)
		return
	}
	if session.caseCosts == nil {
		session.caseCosts = make(map[string]brokerCaseCost)
	}
	cost := session.caseCosts[caseID]
	bookInferenceCost(&cost, attribution, choices, usageOK, completionTokens, reasoningTokens)
	session.caseCosts[caseID] = cost
}

func bookInferenceCost(cost *brokerCaseCost, attribution string, choices uint64, usageOK bool, completionTokens, reasoningTokens uint64) {
	cost.Completions++
	cost.ChoicesTotal += choices
	if usageOK {
		if reasoningTokens > completionTokens {
			reasoningTokens = completionTokens
		}
		cost.CompletionTokens += completionTokens - reasoningTokens
		cost.ReasoningTokens += reasoningTokens
	} else {
		cost.UsageUnavailable++
	}
	// A bucket keeps the strongest attribution it has seen (capability >
	// verified claim > serial window) and never regresses to unattributed.
	if cost.Attribution == "" || scoregates.CostAttributionRank(attribution) > scoregates.CostAttributionRank(cost.Attribution) {
		cost.Attribution = attribution
	}
}

// inferenceCostAttributionLocked names the one case a completion belongs to,
// or "" when it cannot be bound exactly. The claim is honored only when it
// names a case the broker has in flight (runCases[claimed] > 0), mirroring the
// trace context's CaseVerified "claim" source.
func inferenceCostAttributionLocked(session *brokerSession, caseGeneration uint64, claimedCaseID string) (string, string) {
	if caseGeneration != 0 {
		if session.activeCaseGeneration == caseGeneration && session.activeCaseID != "" {
			return session.activeCaseID, scoregates.CostAttributionCaseCapability
		}
		for caseID, generation := range session.caseIDs {
			if generation == caseGeneration && caseID != "" {
				return caseID, scoregates.CostAttributionCaseCapability
			}
		}
	}
	if claimedCaseID != "" && session.runCases[claimedCaseID] > 0 {
		return claimedCaseID, scoregates.CostAttributionVerifiedClaim
	}
	if len(session.runCases) == 1 {
		for caseID := range session.runCases {
			return caseID, scoregates.CostAttributionSerialRunCase
		}
	}
	return "", scoregates.CostAttributionUnavailable
}

// boundedHarnessCaseClaim normalizes the harness's X-Ditto-Case-Id exactly as
// traceContextLocked does (trimmed, capped at 128 bytes), so the ledger and
// the trace context verify the same claim string.
func boundedHarnessCaseClaim(raw string) string {
	claimed := strings.TrimSpace(raw)
	if len(claimed) > 128 {
		claimed = claimed[:128]
	}
	return claimed
}

// decodeChoiceCount reads the provider's `choices` array length. A 2xx body
// that does not parse as a chat completion still counts as one choice, so a
// completion is never booked as free.
func decodeChoiceCount(responseBody []byte) uint64 {
	var decoded struct {
		Choices []json.RawMessage `json:"choices"`
	}
	if json.Unmarshal(responseBody, &decoded) != nil || len(decoded.Choices) == 0 {
		return 1
	}
	return uint64(len(decoded.Choices))
}

// sessionInferenceCost reads one case's booking. nil when no v13 session or no
// completion was bound to the case.
func (b *inferenceBroker) sessionInferenceCost(id, caseID string) *scoregates.InferenceCostRecord {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return nil
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.benchVersion < protocol.BenchVersionV13 {
		return nil
	}
	cost, ok := session.caseCosts[caseID]
	if !ok {
		return nil
	}
	return costRecord(cost)
}

// sessionUnattributedInferenceCost reads the run-level unattributed bucket.
func (b *inferenceBroker) sessionUnattributedInferenceCost(id string) scoregates.InferenceCostRecord {
	b.mu.RLock()
	session := b.sessions[id]
	b.mu.RUnlock()
	if session == nil {
		return scoregates.InferenceCostRecord{}
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.benchVersion < protocol.BenchVersionV13 {
		return scoregates.InferenceCostRecord{}
	}
	record := costRecord(session.unattributedCost)
	if record == nil {
		return scoregates.InferenceCostRecord{}
	}
	return *record
}

func costRecord(cost brokerCaseCost) *scoregates.InferenceCostRecord {
	if cost.Completions == 0 {
		return nil
	}
	return &scoregates.InferenceCostRecord{
		Completions:      int(cost.Completions),
		ChoicesTotal:     int(cost.ChoicesTotal),
		OutputTokens:     cost.CompletionTokens,
		ReasoningTokens:  cost.ReasoningTokens,
		UsageUnavailable: int(cost.UsageUnavailable),
		Attribution:      cost.Attribution,
	}
}

// applyV13InferenceCost attaches the shadow cost record to a case score and its
// transcript execution for bench_version >= 13. It never changes Score.
func (s *server) applyV13InferenceCost(benchVersion int, inferenceSessionID string, cs protocol.CaseScore, class scoregates.CostCaseClass, execution *runner.CaseExecution) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV13 {
		return cs
	}
	var record *scoregates.InferenceCostRecord
	if inferenceSessionID != "" && s.broker != nil {
		record = s.broker.sessionInferenceCost(inferenceSessionID, cs.CaseID)
	}
	evidence := scoregates.BuildInferenceCost(benchVersion, class, record)
	cs.InferenceCost = evidence
	if execution != nil {
		execution.InferenceCost = evidence
	}
	return cs
}

// summarizeV13InferenceCost is the run-level shadow summary for the details
// blob. nil before Bench v13.
func (s *server) summarizeV13InferenceCost(benchVersion int, inferenceSessionID string, perCase []protocol.CaseScore) *protocol.InferenceCostSummary {
	if benchVersion < protocol.BenchVersionV13 {
		return nil
	}
	var unattributed scoregates.InferenceCostRecord
	if inferenceSessionID != "" && s.broker != nil {
		unattributed = s.broker.sessionUnattributedInferenceCost(inferenceSessionID)
	}
	return scoregates.SummarizeInferenceCost(benchVersion, perCase, unattributed)
}
