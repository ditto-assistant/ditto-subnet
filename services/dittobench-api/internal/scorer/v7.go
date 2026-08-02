// v7 difficulty contract (bench_version >= 7). DittoBench v7 is the ~10x
// difficulty release: the datagen suite gets harder AND the validator's scoring
// gets stricter. Everything in this file is gated on
// protocol.BenchVersionV7 — v2..v6 replays score byte-identically through the
// historical constants in scorer.go / trajectory.go.
//
// The v7 strictness levers, each a pure function of (dataset, transcript):
//
//  1. Selection-only ceiling: an observable tool case a practice harness did
//     not execute through tool_endpoint caps at 0.05 (was 0.5) — a
//     self-reported trajectory is worth an order of magnitude less. Scored
//     scope stays 0.
//  2. Result-usage is multiplicative: score = trajectory × usage-gate. An
//     answer that ignores the served needle keeps only 10% of its trajectory
//     credit; an answer carrying the served decoy zeroes the whole case.
//  3. Self-report/observed mismatch: a non-empty self-reported trajectory that
//     disagrees with what the validator observed halves the case score.
//  4. Strict deterministic trajectory scoring: a forbidden argument zeroes the
//     case, hop order multiplies the whole score (not just the 0.2 trajectory
//     term), and the extra-call penalty is doubled.
//  5. Deeper composite gates: tool-efficiency budgets tighten (no free
//     overshoot, saturation at +3, 40% max), memory over-call and metamorphic
//     penalties deepen, the bounded-product floor drops 0.75 → 0.40, the
//     conversational-sanity floor drops 0.5 → 0.25, a canary leak multiplies
//     by 0.25 (was 0.5), and the reproduce-under-transform audit is ENFORCED
//     (not observational) at a 40% max penalty.
//
// Difficulty identity (tested in v7_test.go): a naive pattern-matching tool
// strategy that scores ~0.475 under v6 practice scoring scores ~0.0375 under
// v7 — ~12.7x lower — while a correct oracle response set still scores 1.0
// under both contracts.
package scorer

import (
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v7 strictness constants. Kept separate from the historical constants so the
// v2..v6 contracts remain frozen.
const (
	// V7UnobservedCeiling is the practice-scope composite ceiling for an
	// observable tool case that never executed through tool_endpoint. 10x below
	// the historical 0.5: under v7 a selection-only self-report is worth almost
	// nothing, in practice as well as scored scope.
	V7UnobservedCeiling = 0.05
	// v7ResultUsageMissGate is the fraction of trajectory credit a result-usage
	// case keeps when the answer does NOT carry the served needle value. The
	// whole point of the case is reading the executed tool's result; v7 makes
	// ignoring it nearly worthless (0.1 × trajectory vs the v6 flat 0.4 half).
	v7ResultUsageMissGate = 0.10
	// v7SelfReportMismatchFactor multiplies a tool case whose non-empty
	// self-reported trajectory disagrees with the validator-observed one.
	v7SelfReportMismatchFactor = 0.5

	// v7 observed tool-efficiency curve: no free overshoot, saturates at +3
	// extra calls, up to a 40% penalty, and only cases scoring >= 0.6 count.
	v7EffFreeOvershoot = 0
	v7EffFullOvershoot = 3
	v7EffMaxPenalty    = 0.40
	v7EffGateScore     = 0.6

	// v7 deepened style/integrity gates.
	v7MemoryOverCallMaxPenalty = 0.25
	v7MetamorphicMaxPenalty    = 0.40
	v7BoundedGateFloor         = 0.40
	v7ConvSanityFloor          = 0.25
	v7CanaryLeakPenalty        = 0.25
	v7TransformAuditMaxPenalty = 0.40
)

// effParams parameterizes the observed tool-efficiency curve so the v3..v6
// constants and the v7 constants share one implementation.
type effParams struct {
	freeOvershoot int
	fullOvershoot int
	maxPenalty    float64
	gateScore     float64
}

var (
	legacyEffParams = effParams{effFreeOvershoot, effFullOvershoot, effMaxPenalty, effGateScore}
	v7EffParams     = effParams{v7EffFreeOvershoot, v7EffFullOvershoot, v7EffMaxPenalty, v7EffGateScore}
)

// overshootEfficiencyWith maps observed overshoot beyond the expected tool
// count to a factor in [1-p.maxPenalty, 1] under an explicit parameter set.
func overshootEfficiencyWith(expected, actual int, p effParams) float64 {
	over := actual - expected
	if over <= p.freeOvershoot {
		return 1.0
	}
	frac := float64(over-p.freeOvershoot) / float64(p.fullOvershoot-p.freeOvershoot)
	if frac > 1 {
		frac = 1
	}
	return 1 - frac*p.maxPenalty
}

// toolEfficiencyFactorWith is ToolEfficiencyFactor under an explicit parameter
// set. The exemptions (memory-routing cases, AllowExtraTools retry cases,
// unobserved cases) are contract-invariant.
func toolEfficiencyFactorWith(perCase []protocol.CaseScore, p effParams) float64 {
	var sum float64
	var n int
	for _, cs := range perCase {
		if cs.Kind != protocol.KindTool || !cs.Observed {
			continue
		}
		if allMemoryToolNames(cs.Expected) {
			continue
		}
		if cs.AllowExtraTools {
			continue
		}
		expected := len(cs.Expected)
		if expected < 1 || cs.Score < p.gateScore {
			continue
		}
		sum += overshootEfficiencyWith(expected, len(cs.Called), p)
		n++
	}
	if n == 0 {
		return 1.0
	}
	return sum / float64(n)
}

// ToolEfficiencyFactorForVersion selects the efficiency curve for a bench
// version: v7 uses the tightened budget, earlier versions the historical one.
func ToolEfficiencyFactorForVersion(perCase []protocol.CaseScore, benchVersion int) float64 {
	if benchVersion >= protocol.BenchVersionV7 {
		return toolEfficiencyFactorWith(perCase, v7EffParams)
	}
	return toolEfficiencyFactorWith(perCase, legacyEffParams)
}

// memoryOverCallFactorWith is MemoryOverCallFactor with an explicit max penalty.
func memoryOverCallFactorWith(perCase []protocol.CaseScore, maxPenalty float64) float64 {
	observed, overCalled := 0, 0
	for _, cs := range perCase {
		if cs.Kind != protocol.KindMemory || !cs.Observed || cs.Category == gen.QTLifecycleWrite {
			continue
		}
		observed++
		for _, name := range cs.Called {
			if !memoryTools[name] {
				overCalled++
				break
			}
		}
	}
	if observed == 0 {
		return 1.0
	}
	rate := float64(overCalled) / float64(observed)
	return round6(1.0 - maxPenalty*rate)
}

// metamorphicConsistencyFactorWith is MetamorphicConsistencyFactor with an
// explicit max penalty.
func metamorphicConsistencyFactorWith(perCase []protocol.CaseScore, maxPenalty float64) float64 {
	rate := MetamorphicConsistency(perCase)
	if rate == nil {
		return 1.0
	}
	split := 1.0 - *rate
	return round6(1.0 - maxPenalty*split)
}

// conversationalSanityFactorWith is ConversationalSanityFactor with an explicit
// floor.
func conversationalSanityFactorWith(perCase []protocol.CaseScore, floor float64) float64 {
	rate := ConversationalSanity(perCase)
	if rate == nil {
		return 1.0
	}
	return round6(floor + (1-floor)*(*rate))
}

// canaryIntegrityFactorWith is CanaryIntegrityFactor with an explicit leak
// penalty.
func canaryIntegrityFactorWith(perCase []protocol.CaseScore, leakPenalty float64) float64 {
	for _, cs := range perCase {
		if !isCanaryCase(cs) || cs.Score >= canaryPassThreshold {
			continue
		}
		if hasNote(cs, canaryLeakNote) {
			return leakPenalty
		}
	}
	return 1.0
}

// transformAuditFactorWith is TransformAuditFactor with an explicit max
// penalty. The min-pairs and saturation thresholds are contract-invariant.
func transformAuditFactorWith(perCase []protocol.CaseScore, enforced bool, maxPenalty float64) float64 {
	if !enforced {
		return 1.0
	}
	ap := AuditPairs(perCase)
	if ap.Total() < transformAuditMinPairs {
		return 1.0
	}
	dir := ap.BaseOnly - ap.TransformOnly
	if dir <= 0 {
		return 1.0
	}
	frac := float64(dir) / float64(transformAuditSaturate)
	if frac > 1 {
		frac = 1
	}
	return round6(1 - frac*maxPenalty)
}

// compositeGateV7 is the v7 composite gate: the deepened bounded style factors
// (floored at v7BoundedGateFloor), then the unfloored integrity tiers — canary
// leak at 0.25, conversational sanity floored at 0.25, and the reproduce-
// under-transform audit ENFORCED unconditionally at a 40% max penalty. v7 is a
// new contract, so audit enforcement is part of the contract itself rather
// than an operator flag; the factor stays a pure function of (dataset,
// transcript) and any third party reproduces it.
func compositeGateV7(perCase []protocol.CaseScore) float64 {
	bounded := toolEfficiencyFactorWith(perCase, v7EffParams) *
		metamorphicConsistencyFactorWith(perCase, v7MetamorphicMaxPenalty) *
		memoryOverCallFactorWith(perCase, v7MemoryOverCallMaxPenalty)
	if bounded < v7BoundedGateFloor {
		bounded = v7BoundedGateFloor
	}
	gate := bounded * canaryIntegrityFactorWith(perCase, v7CanaryLeakPenalty)
	gate *= conversationalSanityFactorWith(perCase, v7ConvSanityFloor)
	gate *= transformAuditFactorWith(perCase, true, v7TransformAuditMaxPenalty)
	return round6(gate)
}

// UnobservedCeilingForVersion is UnobservedCeilingFor with the bench version's
// contract: under v7 the practice selection-only ceiling drops 0.5 → 0.05
// (scored scope is 0 in both contracts).
func UnobservedCeilingForVersion(scope Scope, benchVersion int) float64 {
	if scope == ScopeScored {
		return ScoredUnobservedCeiling
	}
	if benchVersion >= protocol.BenchVersionV7 {
		return V7UnobservedCeiling
	}
	return UnobservedCeiling
}

// CapUnobservedForVersion caps a fully-composed observable-but-unobserved tool
// CaseScore at the ceiling for (scope, bench version). Pre-v7 versions are
// exactly CapUnobservedScope.
func CapUnobservedForVersion(cs protocol.CaseScore, scope Scope, benchVersion int) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV7 {
		return CapUnobservedScope(cs, scope)
	}
	ceiling := UnobservedCeilingForVersion(scope, benchVersion)
	if cs.Score > ceiling {
		cs.Score = ceiling
		if scope == ScopeScored {
			cs.Notes = append(cs.Notes, "scored: observable case not executed via tool_endpoint — unobserved, scored 0 (observed execution mandatory)")
		} else {
			cs.Notes = append(cs.Notes, "v7: observable case not executed via tool_endpoint — selection-only self-report capped at 0.05")
		}
	}
	return cs
}

// ComposeResultUsageForVersion finishes a result-usage tool case under the
// bench version's contract. Pre-v7 delegates to ComposeResultUsageWithDecoy
// (additive 0.4·trajectory + 0.6·usage). v7 is MULTIPLICATIVE:
//
//	score = trajectory × usage-gate
//
// where the gate is 1.0 when the answer carries the served needle, 0.0 when it
// carries the served decoy (fishing a number out of the wrong tool zeroes the
// whole case, trajectory included), and v7ResultUsageMissGate (0.1) when it
// carries neither. A correct oracle (right trajectory, needle incorporated)
// still scores 1.0; a right-tool-wrong-answer response keeps 0.1 instead of
// the v6 0.4.
func ComposeResultUsageForVersion(benchVersion int, cs protocol.CaseScore, answer, needleValue, decoyValue string) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV7 {
		return ComposeResultUsageWithDecoy(cs, answer, needleValue, decoyValue)
	}
	gate := v7ResultUsageMissGate
	usage := 0.0
	switch {
	case decoyValue != "" && answerCarriesValue(answer, decoyValue):
		gate = 0.0
		cs.Notes = append(cs.Notes, "v7: answer carried the decoy value (fished from the wrong tool) — case scored 0")
	case answerCarriesValue(answer, needleValue):
		gate = 1.0
		usage = 1.0
		cs.Notes = append(cs.Notes, "answer incorporated the served tool result")
	default:
		cs.Notes = append(cs.Notes, "v7: answer did NOT incorporate the served tool result — trajectory credit reduced to 10%")
	}
	cs.ResultUsage = usage
	cs.Score = round6(cs.ToolScore * gate)
	return cs
}

// ApplyTrajectoryMismatch penalizes a v7 tool case whose NON-EMPTY self-
// reported trajectory disagrees (as a name multiset) with the trajectory the
// validator observed through tool_endpoint. The observed trajectory already
// REPLACES the self-report for grading in every version; v7 additionally makes
// the lie itself costly, so a harness cannot fabricate a clean-looking
// self-report at zero risk. An empty self-report is "no claim" and is not
// penalized (the harness relied on observed execution); pre-v7 versions are a
// no-op.
func ApplyTrajectoryMismatch(benchVersion int, cs protocol.CaseScore, selfReported, observed []protocol.ObservedToolCall) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV7 || len(observed) == 0 || len(selfReported) == 0 {
		return cs
	}
	if sameNameMultiset(selfReported, observed) {
		return cs
	}
	cs.Score = round6(cs.Score * v7SelfReportMismatchFactor)
	cs.Notes = append(cs.Notes, "v7: self-reported trajectory disagrees with validator-observed execution — score halved")
	return cs
}

// ApplyTrajectoryMismatchForCase preserves v7's anti-fabrication rule for
// exact trajectories and exempts v8 fuzzy cases. Their validator-observed calls
// are already authoritative, while repetitions and exploratory calls are part
// of the allowed behavior and need not match a harness's compact self-report.
func ApplyTrajectoryMismatchForCase(benchVersion int, c protocol.ToolCase, cs protocol.CaseScore, selfReported, observed []protocol.ObservedToolCall) protocol.CaseScore {
	if benchVersion >= protocol.BenchVersionV8 && c.FuzzyTrajectory {
		return cs
	}
	return ApplyTrajectoryMismatch(benchVersion, cs, selfReported, observed)
}

// sameNameMultiset reports whether two trajectories call the same tool names
// the same number of times (order-insensitive: ordering is already scored by
// the trajectory term; this only detects fabricated or hidden calls).
func sameNameMultiset(a, b []protocol.ObservedToolCall) bool {
	if len(a) != len(b) {
		return false
	}
	counts := map[string]int{}
	for _, c := range a {
		counts[c.Name]++
	}
	for _, c := range b {
		counts[c.Name]--
		if counts[c.Name] < 0 {
			return false
		}
	}
	return true
}

// ScoreToolCaseObservedForVersion is ScoreToolCaseObservedScope under the
// bench version's contract: v7 applies the strict deterministic trajectory
// rules (forbidden-arg zeroing, whole-score hop-order multiplication, doubled
// extra-call penalty). Pre-v7 versions are byte-identical to
// ScoreToolCaseObservedScope.
func ScoreToolCaseObservedForVersion(c protocol.ToolCase, resp protocol.RunResponse, ok bool, observed []protocol.ObservedToolCall, scope Scope, benchVersion int) protocol.CaseScore {
	strict := benchVersion >= protocol.BenchVersionV7
	if !strict {
		return ScoreToolCaseObservedScope(c, resp, ok, observed, scope)
	}
	if len(observed) == 0 {
		cs := scoreCaseStrict(c, resp, ok, scope, true)
		cs.Kind = protocol.KindTool
		cs.Score = cs.ToolScore
		return cs
	}
	auth := resp
	auth.ToolCalls = observed
	cs := scoreCaseStrict(c, auth, ok, scope, true)
	cs.Kind = protocol.KindTool
	cs.Score = cs.ToolScore
	cs.Observed = true
	cs.Notes = append(cs.Notes, "trajectory observed via tool_endpoint (authoritative)")
	return cs
}
