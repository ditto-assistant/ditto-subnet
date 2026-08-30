// Package scorer turns harness RunResponses into a DittoBench ScoreReport.
// Scoring is fully deterministic (judge-free): a pure function of the dataset
// and the transcript, reproducible by anyone from the public
// dittobench-datagen module.
//
// Tool accuracy per case is deterministic trajectory + argument scoring (see
// trajectory.go): 0.4·name-F1 + 0.4·arg-F1 + 0.2·(order × extra-call
// discipline), and that accuracy is the case score (FinishTool). Cases with no
// expected tool score 1.0 iff the harness called nothing, else 0.0.
// Result-usage cases additionally require the served needle in the answer.
//
// Memory cases are graded by the public per-AnswerKind grader
// (dittobench-datagen/grade).
package scorer

import (
	"fmt"
	"math"
	"os"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Score builds the aggregate report for a set of cases and their responses.
// Missing responses (harness error / timeout) are scored as zero.
func Score(runID string, cases []protocol.ToolCase, resps map[string]protocol.RunResponse) protocol.ScoreReport {
	perCase := make([]protocol.CaseScore, 0, len(cases))
	var toolSum float64
	latencies := make([]int64, 0, len(cases))

	for _, c := range cases {
		resp, ok := resps[c.ID]
		cs := scoreCase(c, resp, ok, ScopePractice)
		cs.Score = cs.ToolScore // direct/legacy mode: composite == tool accuracy
		perCase = append(perCase, cs)
		toolSum += cs.ToolScore
		latencies = append(latencies, cs.LatencyMs)
	}

	n := len(cases)
	toolMean := 0.0
	if n > 0 {
		toolMean = toolSum / float64(n)
	}

	report := protocol.ScoreReport{
		RunID:       runID,
		GeneratedAt: protocol.DatasetEpochRFC3339,
		Composite:   toolMean, // tool-mean is the composite for the practice scope
		ToolMean:    toolMean,
		MedianMs:    median(latencies),
		N:           n,
		PerCase:     perCase,
	}
	return report
}

// ScoreToolCase computes the deterministic tool accuracy of a tool case.
// ToolScore (0..1) is the accuracy; the caller finalizes Score with FinishTool
// or ComposeResultUsage. ok=false means the harness gave no usable response
// (scored as a miss).
func ScoreToolCase(c protocol.ToolCase, resp protocol.RunResponse, ok bool) protocol.CaseScore {
	return ScoreToolCaseScope(c, resp, ok, ScopePractice)
}

// ScoreToolCaseScope is ScoreToolCase with an explicit scope. In ScopeScored the
// no-verifiable-evidence case-scoring rules tighten (see scoreCase); practice is
// identical to ScoreToolCase.
func ScoreToolCaseScope(c protocol.ToolCase, resp protocol.RunResponse, ok bool, scope Scope) protocol.CaseScore {
	cs := scoreCase(c, resp, ok, scope)
	cs.Kind = protocol.KindTool
	cs.Score = cs.ToolScore
	return cs
}

// FinishTool finalizes a non-result-usage tool case: the deterministic
// trajectory + argument accuracy IS the score. Judge-free scoring has no
// quality half; response quality under the model lock is a property of the
// locked model, not the harness.
func FinishTool(cs protocol.CaseScore) protocol.CaseScore {
	cs.Score = cs.ToolScore
	return cs
}

// ResultUsageTrajectoryWeight / ResultUsageAnswerWeight split a result-usage tool
// case's score between calling the right tool(s) and actually USING the returned
// content. The answer half dominates: the whole point (capability 13) is
// that the answer incorporates a value obtainable only by executing the tool.
const (
	resultUsageTrajectoryWeight = 0.4
	resultUsageAnswerWeight     = 0.6
)

// ComposeResultUsage finishes a result-usage tool case (observed execution). It scores
// deterministically: 0.4·trajectory (did it call the
// right tool) + 0.6·(answer carries the served needle value). Because the needle
// is a fabricated per-seed value that exists only in the tool's returned content,
// the answer half is unachievable without actually executing the tool and reading
// the result — self-report and base-model knowledge both score 0 on it.
func ComposeResultUsage(cs protocol.CaseScore, answer, needleValue string) protocol.CaseScore {
	return ComposeResultUsageWithDecoy(cs, answer, needleValue, "")
}

// ComposeResultUsageWithDecoy is ComposeResultUsage plus the served DECOY value:
// the plausible-but-wrong number the mock tool endpoint serves from NON-bearer
// content tools (dittobench-datagen toolexec, DecoyValue). A harness that fished
// a number out of the wrong tool's result carries the decoy, not the needle;
// such an answer scores the usage half 0 even if it ALSO happens to include the
// needle, because surfacing the decoy is the signature of grepping any number
// rather than reading the answer-bearing tool. decoyValue "" reproduces the
// plain behavior (no decoy configured). The caller obtains it from the case
// fixture; it is validator-internal grading material, so the score stays a pure
// function of (dataset, transcript).
func ComposeResultUsageWithDecoy(cs protocol.CaseScore, answer, needleValue, decoyValue string) protocol.CaseScore {
	usage := 0.0
	switch {
	case decoyValue != "" && answerCarriesValue(answer, decoyValue):
		usage = 0.0
		cs.Notes = append(cs.Notes, "answer carried the decoy value (fished from the wrong tool) — usage scored 0")
	case answerCarriesValue(answer, needleValue):
		usage = 1.0
		cs.Notes = append(cs.Notes, "answer incorporated the served tool result")
	default:
		cs.Notes = append(cs.Notes, "answer did NOT incorporate the served tool result")
	}
	cs.ResultUsage = usage
	cs.Score = round6(resultUsageTrajectoryWeight*cs.ToolScore + resultUsageAnswerWeight*usage)
	return cs
}

// answerCarriesValue reports whether a numeric needle value (e.g. "3,418")
// appears in the answer, tolerant of thousands separators — "3418", "3,418", and
// "3 418" all count, but not a coincidental substring inside a longer number.
func answerCarriesValue(answer, needleValue string) bool {
	want := stripSeparators(needleValue)
	if want == "" {
		return false
	}
	got := stripSeparators(normalizeAnswer(answer))
	return containsNumberToken(got, want)
}

// stripSeparators removes thousands separators (commas, spaces) that fall between
// digits so "3,418" / "3 418" / "3418" all normalize to "3418", without merging
// two genuinely separate numbers ("call 5, order 9" stays "call 5, order 9").
func stripSeparators(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c == ',' || c == ' ') && i > 0 && isDigit(s[i-1]) && i+1 < len(s) && isDigit(s[i+1]) {
			continue
		}
		b.WriteByte(c)
	}
	return b.String()
}

// Scope selects how strictly a run is scored. Practice is the lenient,
// self-hostable path (a miner testing their own harness): an unverifiable
// self-report still earns bounded, capped credit. Scored is the canonical
// on-chain path whose report feeds the KOTH ledger; there, observed execution is
// MANDATORY — an observable tool case the validator did not actually watch
// execute earns nothing. Memory-routing cases are routing-only in BOTH scopes
// (internal memory retrieval is unobservable through the tool endpoint, so a
// substantive direct-store answer credits and only an observed misroute
// zeroes). Keeping the split explicit
// means practice tooling and calibration stay unchanged while the trustless,
// stake-weighted path closes the parser-harness free points. A Scope is not a
// validator secret: it is a property of the request (the canonical /v1/score
// path pins the dataset), so any third party re-deriving the score from
// (dataset, transcript, scope) reproduces it byte-for-byte.
type Scope int

const (
	// ScopePractice keeps the historical lenient scoring (default zero value).
	ScopePractice Scope = iota
	// ScopeScored is the on-chain/KOTH path: observed execution is mandatory.
	ScopeScored
)

// UnobservedCeiling caps the composite of an OBSERVABLE tool case (every expected
// tool is validator-served) that the harness did NOT execute through the mock
// tool_endpoint, in PRACTICE scope. Its self-reported trajectory is untrusted:
// we cannot verify the tools actually ran, so the case is scored selection-only
// and cannot reach full marks. Set at 0.5 so a right-tool self-report still earns
// meaningful-but-capped credit, and a harness is materially rewarded for
// executing through the endpoint (where the full, verified score is reachable).
const UnobservedCeiling = 0.5

// ScoredUnobservedCeiling is the ceiling in SCORED scope: 0. On the on-chain
// path observed execution is mandatory, so an observable tool case that never
// routed through the validator's mock endpoint is unverifiable and earns
// nothing. This removes the deterministic-parser free 0.5: a harness that only
// self-reports its trajectory cannot bank half-credit against staked runs.
const ScoredUnobservedCeiling = 0.0

// UnobservedCeilingFor returns the observable-but-unobserved ceiling for a scope.
func UnobservedCeilingFor(scope Scope) float64 {
	if scope == ScopeScored {
		return ScoredUnobservedCeiling
	}
	return UnobservedCeiling
}

// ScoreToolCaseObserved computes the deterministic tool-accuracy half of a case
// under observed execution. When observed is non-empty (the harness
// routed its calls through the validator's mock endpoint) it REPLACES the
// harness's self-reported tool_calls as the authoritative trajectory — the
// validator grades what it actually saw execute, not what the harness claims.
// When observed is nil it degrades to the self-report path
// (ScoreToolCase); the caller applies CapUnobserved for an observable case so an
// unverifiable self-report cannot score full marks.
func ScoreToolCaseObserved(c protocol.ToolCase, resp protocol.RunResponse, ok bool, observed []protocol.ObservedToolCall) protocol.CaseScore {
	return ScoreToolCaseObservedScope(c, resp, ok, observed, ScopePractice)
}

// ScoreToolCaseObservedScope is ScoreToolCaseObserved with an explicit scope.
// The observed-overrides-self-report semantics are identical in both scopes; the
// scope only tightens the unobserved-observable ceiling applied by the caller
// (0.5 in practice, 0 scored). Memory-routing cases stay routing-only in both
// scopes; see the allMemoryTools branch in scoreCase.
func ScoreToolCaseObservedScope(c protocol.ToolCase, resp protocol.RunResponse, ok bool, observed []protocol.ObservedToolCall, scope Scope) protocol.CaseScore {
	if len(observed) == 0 {
		return ScoreToolCaseScope(c, resp, ok, scope)
	}
	auth := resp
	auth.ToolCalls = observed
	cs := ScoreToolCaseScope(c, auth, ok, scope)
	cs.Observed = true
	cs.Notes = append(cs.Notes, "trajectory observed via tool_endpoint (authoritative)")
	return cs
}

// Tool-efficiency term. Among tool cases the validator OBSERVED executing (so the
// call count is authoritative, not self-reported), a harness that reached a
// correct answer within its expected tool budget scores full efficiency; one that
// overshot the budget is penalized on a bounded, dead-zone curve. The factor
// multiplies the composite (never the per-suite means), so it can only separate
// otherwise-comparable harnesses — accuracy stays dominant. Only cases scoring at
// or above effGateScore count, so efficiency differentiates competent responses
// rather than rewarding a lean-but-wrong one.
const (
	effFreeOvershoot = 1    // extra calls beyond expected that incur no penalty
	effFullOvershoot = 5    // overshoot at/above which the penalty saturates
	effMaxPenalty    = 0.15 // deepest the factor can drop (matches the composite floor)
	effGateScore     = 0.5  // only cases scoring >= this contribute
)

// overshootEfficiency maps how far an observed trajectory exceeded the expected
// tool count to a factor in [1-effMaxPenalty, 1] under the historical (v3..v6)
// curve. The shared implementation lives in v7.go (overshootEfficiencyWith).
func overshootEfficiency(expected, actual int) float64 {
	return overshootEfficiencyWith(expected, actual, legacyEffParams)
}

// ToolEfficiencyFactor is the run's observed tool-efficiency multiplier for the
// composite: the mean per-case efficiency over observed, competently-answered
// tool cases, or 1.0 (no effect) when none ran under observed execution — e.g. a
// remote harness that never routed through the mock endpoint, where the call
// count is unverifiable and so is not scored.
// Memory-routing cases are scored routing-only precisely so the suite does
// not penalize a competent memory harness for HOW it retrieves (see
// scoreCase's allMemoryTools branch); charging overshoot there would
// re-penalize exactly that. AllowExtraTools cases REQUIRE more calls than
// expected (forced first-call failure), so they are exempt too. Both
// exemptions live in toolEfficiencyFactorWith (v7.go), shared with the v7
// tightened curve.
func ToolEfficiencyFactor(perCase []protocol.CaseScore) float64 {
	return toolEfficiencyFactorWith(perCase, legacyEffParams)
}

// CapUnobserved applies UnobservedCeiling to a fully-composed tool CaseScore when
// the case was observable but the harness did not execute through the endpoint.
// Call it AFTER ComposeTool (the ceiling is on the final composite, not just the
// deterministic half). No-op when the score is already at or below the ceiling.
func CapUnobserved(cs protocol.CaseScore) protocol.CaseScore {
	return CapUnobservedScope(cs, ScopePractice)
}

// CapUnobservedScope caps a fully-composed observable-but-unobserved tool
// CaseScore at the ceiling for its scope: 0.5 in practice, 0 on the scored
// on-chain path where observed execution is mandatory. No-op when the score is
// already at or below the ceiling. Call it AFTER ComposeTool.
func CapUnobservedScope(cs protocol.CaseScore, scope Scope) protocol.CaseScore {
	ceiling := UnobservedCeilingFor(scope)
	if cs.Score > ceiling {
		cs.Score = ceiling
		if scope == ScopeScored {
			cs.Notes = append(cs.Notes, "scored: observable case not executed via tool_endpoint — unobserved, scored 0 (observed execution mandatory)")
		} else {
			cs.Notes = append(cs.Notes, "capped: observable case not executed via tool_endpoint (self-report untrusted)")
		}
	}
	return cs
}

// Canary integrity gate. A per-run canary case asks for a seed-derived nonce
// seeded into the conversation; recalling it proves genuine in-context retrieval
// this run (the value cannot be cached across runs or known to a base model),
// and a bait decoy catches a harness that echoes any nonce-shaped token. The
// gate splits the two failure modes it can see: surfacing the bait (a leak, a
// cross-user boundary breach) stays a hard disqualifier, while an honest recall
// miss that neither leaks nor fabricates takes a bounded penalty. The split
// matters because on a nondeterministic model a single binary canary case is
// near a coin flip run to run; halving the composite on that coin flip injects
// noise, not signal, whereas a leak is a low-noise, deliberate-looking breach
// that a competent honest harness essentially never commits.
const (
	canaryPassThreshold = 0.5 // canary case Score at/above this counts as passed
	canaryLeakPenalty   = 0.5 // hard multiplier for a canary LEAK (bait echo): an integrity breach
)

// canaryLeakNote is the canonical marker GradeMemory appends when a canary case
// surfaced its bait/forbidden nonce, so CanaryIntegrityFactor can tell a genuine
// leak from an honest recall miss without matching the grader's human-readable
// note wording. Produced and consumed entirely in this package.
const canaryLeakNote = "canary-leak"

// hasNote reports whether a case carries an exact marker note.
func hasNote(cs protocol.CaseScore, marker string) bool {
	for _, n := range cs.Notes {
		if n == marker {
			return true
		}
	}
	return false
}

// isCanaryCase reports whether a CaseScore is the integrity canary (its category
// carries the "canary" question type).
func isCanaryCase(cs protocol.CaseScore) bool {
	return cs.Kind == protocol.KindMemory && strings.Contains(strings.ToLower(cs.Category), "canary")
}

// CanaryIntegrityFactor returns the composite multiplier for canary integrity,
// 1.0 when no canary leaked. Only a LEAK (bait echo, canaryLeakNote) — a
// deliberate-looking integrity breach a competent honest harness never commits —
// applies the hard canaryLeakPenalty.
//
// De-inversion (anti-gaming): an honest canary MISS no longer penalizes the
// composite. The miss is already counted in the canary case's own accuracy
// score; multiplying the composite again double-counted it, and because that
// penalty fell only on a nondeterministic model's coin-flip miss (a
// deterministic parser retrieves the seeded nonce every run), it taxed exactly
// the honest reasoner it was meant to protect. The composite must stay a pure
// function of (dataset, transcript) — trustless — so parser-vs-reasoner
// separation lives in task difficulty (the datagen suite) and in the advisory
// screener/gstudy signals, NOT in a variance penalty here. The leak
// disqualifier stays: it is a low-noise breach signature, not a variance tax.
// The penalty is applied AT MOST ONCE per run, no matter how many canary cases
// leaked. Leaking is one breach signature -- a harness that surfaces the bait
// does so because of how it retrieves, not once per probe -- so compounding over
// however many canary cases a seed happened to draw would scale the harshest
// penalty in the system with dataset shape rather than with behaviour. The
// generator no longer audit-duplicates the canary, which is what made a second
// case reachable; this keeps the factor correct regardless.
func CanaryIntegrityFactor(perCase []protocol.CaseScore) float64 {
	return canaryIntegrityFactorWith(perCase, canaryLeakPenalty)
}

// MetamorphicConsistency returns the fraction of invariance twin groups whose
// member cases the harness answered consistently — all correct or all incorrect
// A phrasing-brittle harness that gets one twin right and its
// reworded twin wrong scores below 1.0. Returns nil when no twin groups ran
// (nothing to measure). The raw rate is advisory; the derived
// MetamorphicConsistencyFactor is what folds into the composite.
func MetamorphicConsistency(perCase []protocol.CaseScore) *float64 {
	groups := map[string][]bool{}
	undelivered := map[string]bool{}
	for _, cs := range perCase {
		if cs.TwinGroup == "" {
			continue
		}
		// Transform-audit pairs share a TwinGroup too, but they are reported and
		// acted on separately (TransformRobustness). Counting them here as well
		// would apply two composite penalties for one signal, which is a silent
		// extra cost to a miner rather than a stronger defense.
		if isAuditGroup(cs.TwinGroup) {
			continue
		}
		// Injection twins are excluded for the same reason. They vary the ATTACK
		// FRAMING, not the phrasing of a fact, so an agent that resists two of
		// three framings has an injection-resistance gap -- already charged on the
		// complied case's own accuracy -- and no phrasing brittleness. Counting
		// them here charged one signal twice and let a family that measures no
		// phrasing at all contribute a quarter of the phrasing-robustness rate.
		if strings.HasPrefix(cs.TwinGroup, persona.InjectionTwinPrefix) {
			continue
		}
		// An undelivered sibling makes the whole family unusable: a 3-member group
		// with one timeout would otherwise read as split and charge phrasing
		// brittleness for a transport failure.
		if cs.Undelivered {
			undelivered[cs.TwinGroup] = true
			continue
		}
		groups[cs.TwinGroup] = append(groups[cs.TwinGroup], cs.Correct)
	}
	// Drop any family that lost a member to a transport failure: its surviving
	// verdicts cannot establish agreement across the fact's rewordings.
	for g := range undelivered {
		delete(groups, g)
	}
	if len(groups) == 0 {
		return nil
	}
	consistent := 0
	for _, verdicts := range groups {
		all, none := true, true
		for _, v := range verdicts {
			if v {
				none = false
			} else {
				all = false
			}
		}
		if all || none {
			consistent++
		}
	}
	v := round6(float64(consistent) / float64(len(groups)))
	return &v
}

// auditHalf classifies a case as the base or the transformed side of an audit
// pair, or "" for everything else. Derived from the validator-internal question
// id, so no new wire field is needed on the case itself and the harness sees
// nothing new.
func auditHalf(mc protocol.MemoryCase) string {
	if !isAuditGroup(mc.TwinGroup) {
		return ""
	}
	if strings.HasPrefix(mc.QuestionID, persona.AuditCaseIDPrefix) {
		return protocol.AuditHalfTransform
	}
	return protocol.AuditHalfBase
}

// AuditPairs is the 2x2 outcome table over the run's transform-audit pairs, and
// it is what a brittleness verdict should be built from rather than
// TransformRobustness.
//
// The distinction that matters is DIRECTION. A pair the harness got right on
// the base phrasing and wrong on the transformed one is the brittleness event;
// the reverse is not, and a harness with a surface-keyed lookup has no reason to
// produce it. The 2026-07-18 calibration measured an honest model at 5 base-only
// vs 6 transform-only (symmetric, i.e. noise) and a surface-gated harness at 6
// vs 0. Agreement collapses both of those to a single rate and cannot tell them
// apart, which is why it did not separate the two.
//
// Counts rather than a rate because they POOL: a consumer sums them over an
// agent's runs and over the k=3 validators and decides once on all the evidence,
// instead of averaging per-run ratios that weight a one-pair run like a
// seven-pair one.
func AuditPairs(perCase []protocol.CaseScore) protocol.AuditPairCounts {
	type sides struct{ base, xform *bool }
	groups := map[string]*sides{}
	for i := range perCase {
		cs := perCase[i]
		if !isAuditGroup(cs.TwinGroup) || cs.AuditHalf == "" {
			continue
		}
		// Never delivered: leave the side nil so the pair is dropped below. Before
		// this, a timed-out case still produced a (wrong) verdict, so the pair
		// looked complete and the guard below could never fire.
		if cs.Undelivered {
			continue
		}
		g := groups[cs.TwinGroup]
		if g == nil {
			g = &sides{}
			groups[cs.TwinGroup] = g
		}
		correct := cs.Correct
		if cs.AuditHalf == protocol.AuditHalfTransform {
			g.xform = &correct
		} else {
			g.base = &correct
		}
	}
	var out protocol.AuditPairCounts
	for _, g := range groups {
		// A half-delivered pair (a dropped or timed-out case) is dropped rather
		// than counted, so a transport failure never reads as brittleness.
		if g.base == nil || g.xform == nil {
			continue
		}
		switch {
		case *g.base && *g.xform:
			out.BothCorrect++
		case *g.base && !*g.xform:
			out.BaseOnly++
		case !*g.base && *g.xform:
			out.TransformOnly++
		default:
			out.BothWrong++
		}
	}
	return out
}

// isAuditGroup reports whether a TwinGroup is a reproduce-under-transform audit
// pair rather than an ordinary generator-chosen invariance family. The prefix is
// the datagen constant, so there is one definition shared by the generator, the
// scorer, and any third party recomputing the metric.
func isAuditGroup(twinGroup string) bool {
	return strings.HasPrefix(twinGroup, persona.AuditTwinPrefix)
}

// TransformRobustness is the reproduce-under-transform audit result (v3 Part A):
// over the run's audit pairs, the fraction the harness answered CONSISTENTLY.
// Each pair is a base case and the same underlying fact re-asked under a
// transform derived from the block-hash-seeded dataset seed, which postdates the
// submission's commit — so unlike the generator-chosen twins this generalizes,
// the harness cannot have pre-handled the specific rephrasing.
//
// Consistency, not correctness, is the measurement: a harness that is wrong on
// both halves is already penalized by accuracy, and counting it again here would
// double-charge it. What this isolates is the SPLIT — right on the phrasing it
// was built for, wrong on one it was not — which is the surface-brittleness
// signature, plus the covariance case where a memorized answer is stale.
//
// The pairing rides TwinGroup, which both halves carry, and the agreement test
// is symmetric, so no consumer needs to know which half was the transform.
// Returns nil when no audit pairs ran.
func TransformRobustness(perCase []protocol.CaseScore) (*float64, int) {
	groups := map[string][]bool{}
	for _, cs := range perCase {
		if !isAuditGroup(cs.TwinGroup) {
			continue
		}
		groups[cs.TwinGroup] = append(groups[cs.TwinGroup], cs.Correct)
	}
	// A pair needs both halves to say anything. A half-delivered pair (a case
	// dropped by a timeout) is dropped rather than scored, so a transport failure
	// never reads as brittleness.
	pairs := 0
	consistent := 0
	for _, verdicts := range groups {
		if len(verdicts) != 2 {
			continue
		}
		pairs++
		if verdicts[0] == verdicts[1] {
			consistent++
		}
	}
	if pairs == 0 {
		return nil, 0
	}
	v := round6(float64(consistent) / float64(pairs))
	return &v, pairs
}

// memoryOverCallMaxPenalty is the deepest the memory over-call factor can drop
// the composite. Deliberately small: this penalizes a WASTEFUL but not
// necessarily dishonest behaviour, and accuracy stays dominant.
const memoryOverCallMaxPenalty = 0.10

// boundedGateFloor is the deepest the BOUNDED factors can drop the composite in
// combination. Each factor is individually capped (0.15 / 0.15 / 0.10), but they
// multiply, so their unclamped worst case was 0.85*0.85*0.90 = 0.65 -- a 35%
// haircut where every documented bound implies at most 15%. All three are
// style-of-work penalties on a harness that answered correctly; stacking them
// into a larger penalty than any single breach carries is not a defensible
// ordering, so the product is floored.
//
// The canary disqualifier is deliberately OUTSIDE this floor: it is a breach
// signature, not a style penalty, and it is meant to be severe.
const boundedGateFloor = 0.75

// CompositeGate is the multiplier applied to the composite (and, identically, to
// its standard error -- the gate is a pure scalar on the mean, so
// SE(gate*mean) = gate*SE(mean)).
//
// Two tiers. The bounded style factors multiply and are then floored at
// boundedGateFloor. The canary integrity factor is applied afterwards and is not
// floored, so a genuine cross-user leak still hard-caps the composite.
//
// The v5 conversational-sanity factor is a THIRD tier, layered on in
// AggregateForVersion for v5 only (CompositeGateForVersion), so v3/v4 bytes and
// already-recorded scores stay exactly as they were.
func CompositeGate(perCase []protocol.CaseScore) float64 {
	bounded := ToolEfficiencyFactor(perCase) *
		MetamorphicConsistencyFactor(perCase) *
		MemoryOverCallFactor(perCase)
	if bounded < boundedGateFloor {
		bounded = boundedGateFloor
	}
	return round6(bounded * CanaryIntegrityFactor(perCase))
}

// convSanityFloor is the deepest the conversational-sanity factor can drop the
// composite. It bites HARDER than the bounded efficiency floors (0.85 tool
// over-call, 0.90 memory over-call) and than the bounded-product floor (0.75),
// because a leaked or irrelevant reply is a correctness failure, not a
// style-of-work waste. Set at 0.5 so a run that wholly fails conversational
// sanity cannot reach champion composite regardless of memory accuracy (a champion
// mean ~0.9 caps at ~0.45), while an honest harness -- which passes conversational
// sanity -- takes no penalty at all (factor 1.0). Applied as its OWN tier outside
// boundedGateFloor, like the canary disqualifier.
const convSanityFloor = 0.5

// conversationalSlices are the three v5 categories the first-class
// conversational-sanity metric is a CONJUNCTION over: greeting non-leak,
// declarative acknowledgement, and behavior-change application. The metric is the
// weakest link across the slices that ran, so a canned reply that clears the
// greeting slice (a fixed "Got it!" passes the non-leak floor) cannot bank it and
// dilute its failures on the other two (v5 plan 4.1 interlock).
var conversationalSlices = []string{
	gen.QTChitchat,
	gen.QTDeclarativeAck,
	gen.QTDeclarativeBehavior,
}

// sliceMean returns the mean correctness over the memory cases of one category,
// or nil when none ran.
func sliceMean(perCase []protocol.CaseScore, category string) *float64 {
	var sum float64
	var n int
	for _, cs := range perCase {
		if cs.Kind == protocol.KindMemory && cs.Category == category {
			if cs.Correct {
				sum++
			}
			n++
		}
	}
	if n == 0 {
		return nil
	}
	v := sum / float64(n)
	return &v
}

// ConversationalSanity is the first-class v5 conversational grounding metric: the
// GEOMETRIC MEAN of the per-slice pass rates across the conversationalSlices that
// ran. It is a graded conjunction: any slice that is FULLY failed (mean 0 — a
// harness that leaks on every greeting, or never captures a plain declarative)
// zeroes the whole metric, so the greeting-non-leak interlock a router must beat is
// preserved. But unlike the old weakest-link MINIMUM, a partially-weak slice no
// longer wholly dictates the score, and — critically — the metric is no longer
// dominated by the single noisiest small-N slice. That min-of-small-slices was the
// dominant source of PER-SEED variance: the behavior slice runs on only a handful
// of cases at a genuine harness's ~0.85 pass rate, so a one-case swing there halved
// a grounded champion's composite at random. The geometric mean pools the evidence
// across slices, cutting the metric's between-seed standard deviation by about half
// (measured) while still catching a leaked or uncaptured slice. Returns nil when no
// conversational case ran, so pre-v5 contracts and runs that drew none are
// unaffected. A pure function of (dataset, transcript): any third party recomputes it.
func ConversationalSanity(perCase []protocol.CaseScore) *float64 {
	logSum := 0.0
	n := 0
	for _, cat := range conversationalSlices {
		m := sliceMean(perCase, cat)
		if m == nil {
			continue
		}
		n++
		if *m <= 0 {
			// A fully-failed slice zeroes the geometric mean: the anti-gaming
			// interlock (a leaked greeting or an uncaptured declarative cannot be
			// hidden behind the other slices) is preserved exactly.
			z := 0.0
			return &z
		}
		logSum += math.Log(*m)
	}
	if n == 0 {
		return nil
	}
	v := round6(math.Exp(logSum / float64(n)))
	return &v
}

// ConversationalSanityFactor is the v5 composite multiplier from the
// conversational-sanity metric: a bounded, saturating penalty that is 1.0 when the
// metric is perfect (an honest harness) and drops to convSanityFloor when it is
// zero (a router that leaks or fails to ground every conversational turn).
// Returns 1.0 (no effect) when no conversational case ran. Pure function of
// perCase.
func ConversationalSanityFactor(perCase []protocol.CaseScore) float64 {
	return conversationalSanityFactorWith(perCase, convSanityFloor)
}

// Transform-audit enforcement (v5 plan 4.5). The reproduce-under-transform audit
// (persona/transform.go) has shipped OBSERVATIONAL since v3: TransformRobustness
// and the directional AuditPairs counts are published in RunDetails but never fold
// into the composite. This wires the gate so it CAN bite, keyed on the directional
// brittleness signal (base-only minus transform-only), which the 2026-07-18
// calibration showed separates a surface-keyed harness (directional) from a noisy
// honest one (symmetric).
const (
	// transformAuditMaxPenalty is the deepest the audit factor can drop the
	// composite. A bounded style penalty, same magnitude family as the other gates.
	transformAuditMaxPenalty = 0.15
	// transformAuditMinPairs is the fewest audit pairs a run needs before the gate
	// acts, so a one- or two-pair run cannot swing the composite on noise.
	transformAuditMinPairs = 4
	// transformAuditSaturate is the directional excess (base-only minus
	// transform-only) at or above which the penalty saturates.
	transformAuditSaturate = 3
)

// transformAuditEnforced reports whether the reproduce-under-transform audit is
// wired to GATE the composite, via the DITTO_TRANSFORM_AUDIT_ENFORCE environment
// variable. Default false: the gate is BUILT (TransformAuditFactor is a tested pure
// function) but ships observational, with no composite effect, until the
// champion-population false-positive posture is calibrated against a
// champion-competence local solver (v5 plan 4.5 / open risk). Reproducibility note:
// in the default (published) configuration the factor is 1.0, so the composite
// stays a pure function of (dataset, transcript); enabling enforcement is a
// deliberate, platform-wide, documented contract change applied uniformly across
// the k=3 validators, not a per-run secret.
func transformAuditEnforced() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("DITTO_TRANSFORM_AUDIT_ENFORCE"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// TransformAuditFactor is the composite multiplier from the reproduce-under-
// transform audit's DIRECTIONAL brittleness. It is 1.0 (no effect) unless
// enforcement is enabled AND the run shows enough audit pairs AND positive
// directional brittleness (more base-only than transform-only splits) -- the
// surface-keyed-lookup signature. A noisy honest model, whose discordant pairs are
// symmetric, has directional excess ~0 and is not penalized. Pure function of
// (perCase, enforced), so passing enforced explicitly keeps it testable and keeps
// the default-configuration composite reproducible.
func TransformAuditFactor(perCase []protocol.CaseScore, enforced bool) float64 {
	return transformAuditFactorWith(perCase, enforced, transformAuditMaxPenalty)
}

// CompositeGateForVersion is CompositeGate plus the version-gated v5 tiers: the
// conversational-sanity factor (4.1) and the transform-audit enforcement factor
// (4.5). Both are applied OUTSIDE boundedGateFloor, like the canary disqualifier,
// so a conversational-sanity failure can pull the composite below the bounded
// floor. For pre-v5 versions this is exactly CompositeGate, so v3/v4 scores are
// byte-identical. bench_version 7 replaces the whole gate stack with the
// deepened v7 contract (see compositeGateV7 in v7.go); v5/v6 replays keep the
// historical constants byte-for-byte.
func CompositeGateForVersion(perCase []protocol.CaseScore, benchVersion int) float64 {
	if benchVersion >= protocol.BenchVersionV7 {
		return compositeGateV7(perCase)
	}
	gate := CompositeGate(perCase)
	if benchVersion >= protocol.BenchVersionV5 {
		gate = round6(gate *
			ConversationalSanityFactor(perCase) *
			TransformAuditFactor(perCase, transformAuditEnforced()))
	}
	return gate
}

// MemoryOverCallFactor is the memory over-call multiplier (v3 B4). A pure-memory
// question is answered from the harness's own store; emitting a NON-memory tool
// call on such a case is observable but was previously unscored, so a harness
// could hedge — take an action AND give a memory answer — at no cost, on the
// theory that "only one is graded".
//
// Only non-memory calls count. A search_memories / fetch_memories call is how a
// harness legitimately retrieves, and penalizing it would be penalizing a
// competent harness for its retrieval mechanism, which is explicitly not the
// intent (same reasoning as the memory-tool routing rule above).
//
// Scored off the VALIDATOR-OBSERVED trajectory only (cs.Observed): a self-report
// cannot create or hide an over-call, so this cannot be laundered by editing the
// transcript. A run where nothing was observed yields 1.0 — no effect — so a
// harness that never routed through the endpoint is not penalized for a call the
// validator cannot see.
//
// Lifecycle mutation cases are excluded from both the numerator and denominator:
// save_memory, update_memory, and delete_memory are the intended work there, and
// the later memory-write-read case is the authoritative persistence check. The
// exemption is category-specific, so those same writes (and unrelated external
// actions) remain over-calls on ordinary memory retrieval cases.
//
// The penalty scales with the FRACTION of other observed memory cases that
// over-called, so an isolated stray call is nearly free while a harness that acts
// on every recall question takes the full bounded hit.
func MemoryOverCallFactor(perCase []protocol.CaseScore) float64 {
	return memoryOverCallFactorWith(perCase, memoryOverCallMaxPenalty)
}

// metamorphicMaxPenalty is the deepest the metamorphic-consistency factor can
// drop the composite (matches effMaxPenalty; accuracy stays dominant).
const metamorphicMaxPenalty = 0.15

// MetamorphicConsistencyFactor is the run's phrasing-robustness multiplier for
// the composite (anti-gaming addendum N2: promote invariance twins from an
// advisory fingerprint to a first-class score). It penalizes ONLY split twin
// groups: a group where the harness got at least one phrasing right and at
// least one reworded phrasing of the SAME fact wrong. That split is the
// template-matcher signature the survey targets (SCORE/PromptEval/CheckList
// INV): a grounded reader answers every sibling alike, a pattern-matcher rides
// one surface and fails the rest.
//
// A uniformly-wrong group is not a brittleness signal (accuracy already
// penalizes it), and a uniformly-correct group is ideal, so both leave the
// factor at 1.0; only the split fraction bites. The reported advisory
// metamorphic_consistency rate is exactly 1 - splitRate, so the applied factor
// is a pure function of that already-published value and stays auditable
// without a new wire field. Returns 1.0 (no effect) when no twin groups ran.
func MetamorphicConsistencyFactor(perCase []protocol.CaseScore) float64 {
	return metamorphicConsistencyFactorWith(perCase, metamorphicMaxPenalty)
}

// CalibrationBrier returns the mean Brier score over cases whose harness
// reported a confidence: mean((confidence - outcome)^2) where outcome
// is 1 for a correct case and 0 otherwise. Lower is better; a well-calibrated
// harness minimizes it, and always-claiming-1.0 is punished on its wrong cases.
// Returns (nil, 0) when no case carried a confidence. Advisory only.
func CalibrationBrier(perCase []protocol.CaseScore) (*float64, int) {
	var sum float64
	var n int
	for _, cs := range perCase {
		if cs.Confidence == nil {
			continue
		}
		c := clamp01(*cs.Confidence)
		outcome := 0.0
		if cs.Correct {
			outcome = 1.0
		}
		d := c - outcome
		sum += d * d
		n++
	}
	if n == 0 {
		return nil, 0
	}
	v := round6(sum / float64(n))
	return &v, n
}

// memoryCaseScore assembles a memory CaseScore from its graded score in [0,1].
func memoryCaseScore(mc protocol.MemoryCase, resp protocol.RunResponse, score float64) protocol.CaseScore {
	cs := protocol.CaseScore{
		CaseID:     mc.ID,
		Category:   mc.QuestionType,
		Kind:       protocol.KindMemory,
		Score:      clamp01(round6(score)),
		Correct:    score >= 0.5,
		TwinGroup:  mc.TwinGroup,
		AuditHalf:  auditHalf(mc),
		Confidence: resp.Confidence,
		LatencyMs:  resp.LatencyMs,
		Called:     calledNames(resp.ToolCalls),
		Expected:   []string{}, // non-nil so the JSON is [] not null (memory has no expected tools)
	}
	if mc.QuestionType == "" {
		cs.Category = "memory"
	}
	return cs
}

// GradeMemory scores one memory case with the public deterministic grader
// (dittobench-datagen/grade): a pure function of (case, response), no LLM.
// Anyone can re-grade a published transcript with the same module and
// reproduce the score byte-for-byte.
func GradeMemory(mc protocol.MemoryCase, resp protocol.RunResponse) protocol.CaseScore {
	v := grade.Memory(mc, resp)
	cs := memoryCaseScore(mc, resp, v.Score)
	cs.Injection = v.Injection
	cs.Notes = append(cs.Notes, v.Notes...)
	// Tag a canary that surfaced its bait/forbidden nonce so CanaryIntegrityFactor
	// treats a genuine leak as a hard disqualifier and spares an honest recall miss.
	// Recomputes the hit with the grader's own exported matcher over the same
	// slot+prose text.
	//
	// Surfacing the bait ALONGSIDE the user's own nonce is not a leak: the bait is
	// planted in the user's own conversation, so naming it to disambiguate ("your
	// code is X; the code Y on file is Dana's, not yours") is the most competent
	// answer available. Requiring the true value keeps the disqualifier's teeth --
	// echoing nonce-shaped tokens without the real one still leaks -- and mirrors
	// the grader's refuse-and-answer exemption, so the note and the score can never
	// disagree about whether the same response was a breach.
	if isCanaryCase(cs) && mc.ForbiddenAnswer != "" {
		full := strings.TrimSpace(resp.Answer) + "\n" + resp.FinalText
		if grade.Hit(mc.ForbiddenAnswer, full) && !grade.Hit(mc.ExpectedAnswer, full) {
			cs.Notes = append(cs.Notes, canaryLeakNote)
		}
	}
	return cs
}

// MemoryInjectionCompliance extracts only moderation telemetry from a raw
// harness response. It is used when some other contract violation requires the
// response to be sanitized and the case to be forced to zero: no score or raw
// response data crosses this boundary.
func MemoryInjectionCompliance(mc protocol.MemoryCase, resp protocol.RunResponse) bool {
	return grade.InjectionCompliance(mc, resp)
}

func clamp01(x float64) float64 {
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}

func round6(x float64) float64 { return math.Round(x*1e6) / 1e6 }

// stdErrOfMean returns the standard error of the mean of n per-case scores given
// their sum and sum-of-squares, using the sample variance (Bessel-corrected). It
// is 0 for n<2 (a single case has no spread estimate). Guards tiny negative
// variance from float rounding.
func stdErrOfMean(sum, sumSq float64, n int) float64 {
	if n < 2 {
		return 0
	}
	fn := float64(n)
	variance := (sumSq - sum*sum/fn) / (fn - 1)
	if variance <= 0 {
		return 0
	}
	return math.Sqrt(variance / fn)
}

// Aggregate scores a run under the CURRENT bench version's gate set. Prefer
// AggregateForVersion: a run's report must be reproducible under the contract it
// was generated for, not under whatever the module's current release is.
func Aggregate(runID string, perCase []protocol.CaseScore) protocol.ScoreReport {
	return AggregateForVersion(runID, perCase, protocol.CurrentBenchVersion)
}

// ModelRoutePreflightCaseID is the reserved scorer-owned broker reachability
// probe. It is exported so the caller and population filter cannot drift onto
// different spellings.
const ModelRoutePreflightCaseID = "__dittobench_model_route_preflight__"

// IsProtocolPreflightCaseID identifies mechanical reachability probes that are
// outside the benchmark population. The legacy prefix remains part of the
// public harness contract; the exact model-route id is the scorer-owned probe
// used to verify that a harness reaches the ticket broker. Neither is a graded
// question, even if a compatible harness returns a perfect answer or executes
// a served tool while answering it.
func IsProtocolPreflightCaseID(caseID string) bool {
	return strings.HasPrefix(caseID, "preflight:") || caseID == ModelRoutePreflightCaseID
}

// ScoredPopulation removes protocol preflights before any mean, category,
// uncertainty estimate, or gate population is computed. Ordinary runs return
// the original slice, preserving every pre-v9 report byte and allocation.
func ScoredPopulation(perCase []protocol.CaseScore) []protocol.CaseScore {
	first := -1
	for i, cs := range perCase {
		if IsProtocolPreflightCaseID(cs.CaseID) {
			first = i
			break
		}
	}
	if first < 0 {
		return perCase
	}
	filtered := make([]protocol.CaseScore, 0, len(perCase)-1)
	filtered = append(filtered, perCase[:first]...)
	for _, cs := range perCase[first+1:] {
		if !IsProtocolPreflightCaseID(cs.CaseID) {
			filtered = append(filtered, cs)
		}
	}
	return filtered
}

// AggregateForVersion folds per-case scores into a ScoreReport under an explicit
// bench version's contract: composite, per-category breakdown, and median
// latency.
//
// The composite gate is a v3 construct (B4 over-call, N2 phrasing robustness,
// observed-efficiency, canary integrity). v2 is a FROZEN contract whose composite
// is pure accuracy -- 0.5*tool_mean + 0.5*memory_mean -- so applying the v3 gates
// to a v2 run made it unreproducible by a third party holding the v2 contract.
// This is a no-op for v2 data in practice (v2 carries no twin, audit, or canary
// cases, so three of the four factors already evaluate to 1.0), but it makes the
// contract explicit rather than incidental.
func AggregateForVersion(runID string, perCase []protocol.CaseScore, benchVersion int) protocol.ScoreReport {
	perCase = ScoredPopulation(perCase)
	var toolSum, toolN, memSum, memN float64
	var toolSumSq, memSumSq float64
	latencies := make([]int64, 0, len(perCase))
	catSum := map[string]float64{}
	catSumSq := map[string]float64{}
	catCount := map[string]int{}
	catOrder := make([]string, 0)

	for _, cs := range perCase {
		latencies = append(latencies, cs.LatencyMs)
		switch cs.Kind {
		case protocol.KindMemory:
			memSum += cs.Score
			memSumSq += cs.Score * cs.Score
			memN++
		default:
			toolSum += cs.Score
			toolSumSq += cs.Score * cs.Score
			toolN++
		}
		if _, seen := catCount[cs.Category]; !seen {
			catOrder = append(catOrder, cs.Category)
		}
		catSum[cs.Category] += cs.Score
		catSumSq[cs.Category] += cs.Score * cs.Score
		catCount[cs.Category]++
	}

	toolMean := 0.0
	if toolN > 0 {
		toolMean = toolSum / toolN
	}
	memMean := 0.0
	if memN > 0 {
		memMean = memSum / memN
	}
	// DittoBench v2 composite (bench_version 2): 0.5*tool_mean + 0.5*memory_mean
	// when both kinds are present; falls back to the single present mean for
	// tool-only or memory-only runs. Rebalanced from v1's 0.6/0.4:
	// memory is the core product value, memory-coupled tool cases already push
	// memory competence into tool_mean, and the raw-pairs tier (Tier B) makes
	// memory_mean the harder axis. Latency/cost stay OUT of the composite.
	composite := 0.0
	switch {
	case toolN > 0 && memN > 0:
		composite = 0.5*toolMean + 0.5*memMean
	case toolN > 0:
		composite = toolMean
	case memN > 0:
		composite = memMean
	}
	// Apply the composite gate. Computed ONCE here and reused for the standard
	// error below, so the two can never drift apart. v5 layers the
	// conversational-sanity and transform-audit tiers on top of the v3 gate
	// (CompositeGateForVersion); pre-v5 versions get exactly the v3 gate.
	gate := 1.0
	if benchVersion >= protocol.BenchVersionV3 {
		gate = CompositeGateForVersion(perCase, benchVersion)
	}
	composite = round6(composite * gate)

	perCat := make([]protocol.CategoryStat, 0, len(catOrder))
	for _, cat := range catOrder {
		n := catCount[cat]
		mean := catSum[cat] / float64(n)
		perCat = append(perCat, protocol.CategoryStat{
			Category: cat,
			Count:    n,
			Mean:     round6(mean),
			StdErr:   round6(stdErrOfMean(catSum[cat], catSumSq[cat], n)),
		})
	}

	// Combine the tool-half and memory-half standard errors.
	// The two halves are independent samples and the composite is 0.5*tool +
	// 0.5*mem, so Var(composite) = 0.25*Var(toolMean) + 0.25*Var(memMean) and
	// SE = 0.5*sqrt(se_tool^2 + se_mem^2). When only one half is present the
	// composite is that half's mean, so its SE is that half's SE.
	seTool := stdErrOfMean(toolSum, toolSumSq, int(toolN))
	seMem := stdErrOfMean(memSum, memSumSq, int(memN))
	var compositeStderr float64
	switch {
	case toolN > 0 && memN > 0:
		compositeStderr = 0.5 * math.Sqrt(seTool*seTool+seMem*seMem)
	case toolN > 0:
		compositeStderr = seTool
	case memN > 0:
		compositeStderr = seMem
	}
	// Scale the SE by the same gate factors the composite carries, so this is the
	// SE of the *gated* composite the KOTH ledger compares, not of the ungated
	// mean. The gates are pure multipliers on the mean (composite = gate*mean),
	// so SE(gate*mean) = gate*SE(mean). Without this, whenever a gate < 1 the
	// reported SE overstates the final composite's uncertainty and widens the
	// dethrone band too far. The factors are the same pure functions of perCase
	// applied to the composite above.
	compositeStderr *= gate

	report := protocol.ScoreReport{
		RunID:           runID,
		GeneratedAt:     protocol.DatasetEpochRFC3339,
		Composite:       composite,
		CompositeStderr: round6(compositeStderr),
		ToolMean:        toolMean,
		MemoryMean:      memMean,
		MedianMs:        median(latencies),
		N:               len(perCase),
		PerCase:         perCase,
		PerCategory:     perCat,
	}
	// Publish the v5 conversational-sanity metric as a first-class field so a low
	// score cannot hide inside the memory mean. nil (omitted) for pre-v5 runs and
	// runs that drew no conversational case.
	if benchVersion >= protocol.BenchVersionV5 {
		report.ConversationalSanity = ConversationalSanity(perCase)
	}
	return report
}

func scoreCase(c protocol.ToolCase, resp protocol.RunResponse, ok bool, scope Scope) protocol.CaseScore {
	return scoreCaseStrict(c, resp, ok, scope, false)
}

// scoreCaseStrict is scoreCase with the v7 strict deterministic-trajectory flag
// (forbidden-arg zeroing, whole-score order multiplication, doubled extra-call
// penalty). strict=false reproduces the historical v2..v6 scoring exactly; the
// no-expected-tool and memory-routing branches are contract-invariant.
func scoreCaseStrict(c protocol.ToolCase, resp protocol.RunResponse, ok bool, scope Scope, strict bool) protocol.CaseScore {
	cs := protocol.CaseScore{
		CaseID:          c.ID,
		Category:        c.Category,
		Kind:            protocol.KindTool,
		LatencyMs:       resp.LatencyMs,
		Called:          calledNames(resp.ToolCalls),
		Expected:        expectedNames(c.ExpectedTools),
		AllowExtraTools: c.AllowExtraTools,
	}

	if !ok {
		cs.ToolScore = 0
		cs.Notes = append(cs.Notes, "no response from harness (error or timeout)")
		return cs
	}

	// No-expected-tool cases (chit-chat / abstention): perfect only if no ACTION
	// was taken. A single unexpected action zeroes the case.
	//
	// Read-only memory retrieval does not count as an action, for the same reason
	// the memory-tool branch below scores on routing: the mock endpoint never
	// serves memory tools, so a search_memories call is the harness consulting its
	// own store, not reaching into the world. These cases include prompts like
	// "Delete that memory for me" and "Read that link and summarize it", where
	// looking up WHICH memory or link is meant, then asking for the missing
	// detail, is the correct behaviour -- and previously scored 0.
	if len(c.ExpectedTools) == 0 {
		actions := 0
		for _, call := range resp.ToolCalls {
			if !memoryTools[call.Name] {
				actions++
			}
		}
		if actions == 0 {
			cs.ToolScore = 1.0
			if len(resp.ToolCalls) > 0 {
				cs.Notes = append(cs.Notes, "memory retrieval only; no action taken")
			}
		} else {
			cs.ToolScore = 0.0
			cs.Notes = append(cs.Notes, fmt.Sprintf("expected no tools but harness called %d", actions))
		}
		return cs
	}

	// Memory-tool cases: the expected tool is a memory-retrieval tool the mock
	// endpoint never serves, so the harness answers from its OWN seeded memory —
	// legitimately via internal retrieval rather than a catalog tool call. Score on
	// ROUTING, not the exact call: credit unless the harness misroutes the memory
	// request to a non-memory tool (e.g. search_web). This stops the suite from
	// penalizing a competent memory harness for how it retrieves; retrieval
	// accuracy itself is the memory suite's job.
	if allMemoryTools(c.ExpectedTools) {
		// Memory retrieval is INTERNAL and unobservable: the endpoint never serves
		// a memory tool, so a legitimate harness may answer straight from its
		// seeded store with no catalog call. Score this case only on what is
		// verifiable in the OBSERVED trajectory — routing — and never on a
		// self-reported memory call. resp.ToolCalls here is the validator-observed
		// trajectory (auth.ToolCalls = observed), so a memory-tool name that was
		// not routed through the endpoint does not appear and cannot earn credit.
		//
		// The previous scored rule required a memory-tool call as "evidence": that
		// was both spoofable (a fabricated search_memories self-report scored 1)
		// and a false positive (correct direct-store retrieval with no call scored
		// 0). It is replaced by the same routing-only rule in both scopes.
		for _, call := range resp.ToolCalls {
			if !memoryTools[call.Name] {
				cs.ToolScore = 0
				cs.Notes = append(cs.Notes, "misrouted a memory request to a non-memory tool: "+call.Name)
				return cs
			}
		}
		// No observed misrouting. Credit any genuine attempt (an observed memory
		// call or a substantive answer, in either the answer slot or the prose);
		// a pure no-op still fails the routing trap. Whether the answer is
		// CORRECT is the memory suite's job, not this case's.
		if len(resp.ToolCalls) > 0 || strings.TrimSpace(resp.FinalText) != "" ||
			strings.TrimSpace(resp.Answer) != "" {
			cs.ToolScore = 1.0
			cs.Notes = append(cs.Notes, "memory request routed to internal/memory retrieval, no non-memory misrouting observed")
		} else {
			cs.ToolScore = 0
			cs.Notes = append(cs.Notes, "no memory retrieval attempted (no observed call and no answer)")
		}
		return cs
	}

	// Deterministic trajectory + argument scoring: name-F1, arg-F1, and a
	// trajectory term (order credit × extra-call discipline). strict applies the
	// v7 tightenings.
	score, notes := deterministicToolScoreStrict(c, resp.ToolCalls, strict)
	cs.ToolScore = score
	cs.Notes = append(cs.Notes, notes...)
	return cs
}

func calledNames(calls []protocol.ObservedToolCall) []string {
	out := make([]string, 0, len(calls))
	for _, c := range calls {
		out = append(out, c.Name)
	}
	return out
}

// memoryTools are the catalog's memory-retrieval tools. The mock endpoint never
// serves them (that would leak seeded answers), so a harness satisfies a
// memory-tool case by retrieving from its own memory — by whatever mechanism.
var memoryTools = map[string]bool{
	"search_memories":             true,
	"search_subjects":             true,
	"fetch_memories":              true,
	"search_memories_in_subjects": true,
}

// allMemoryTools reports whether every expected tool is a memory-retrieval tool
// (so the case is answered from the harness's own memory, not a served tool).
func allMemoryTools(specs []protocol.ToolSpec) bool {
	if len(specs) == 0 {
		return false
	}
	for _, s := range specs {
		if !memoryTools[s.Name] {
			return false
		}
	}
	return true
}

// allMemoryToolNames is allMemoryTools over an already-flattened name list (a
// CaseScore carries Expected as names, not specs).
func allMemoryToolNames(names []string) bool {
	if len(names) == 0 {
		return false
	}
	for _, n := range names {
		if !memoryTools[n] {
			return false
		}
	}
	return true
}

func expectedNames(specs []protocol.ToolSpec) []string {
	out := make([]string, 0, len(specs))
	for _, s := range specs {
		out = append(out, s.Name)
	}
	return out
}

// containsBoundedPhrase reports whether phrase appears in text bounded on both
// sides by a non-alphanumeric char (or the string edge). Interior spaces in a
// multi-word phrase are fine; only the outer boundaries are checked.
func containsBoundedPhrase(text, phrase string) bool {
	for i := 0; ; {
		j := strings.Index(text[i:], phrase)
		if j < 0 {
			return false
		}
		j += i
		before := j == 0 || !isAlnum(text[j-1])
		after := j+len(phrase) >= len(text) || !isAlnum(text[j+len(phrase)])
		if before && after {
			return true
		}
		i = j + 1
	}
}

func isAlnum(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

// normalizeAnswer lowercases, trims surrounding punctuation/quotes, and collapses
// internal whitespace so containment ignores incidental formatting.
func normalizeAnswer(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	s = strings.Trim(s, `"'.,!?;:`)
	return strings.Join(strings.Fields(s), " ")
}

// isPureNumber reports whether s is only digits with at most one interior
// decimal/thousands separator (e.g. "42", "3.5", "1,000").
func isPureNumber(s string) bool {
	if s == "" {
		return false
	}
	seenSep := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= '0' && c <= '9' {
			continue
		}
		if (c == '.' || c == ',') && !seenSep && i > 0 && i < len(s)-1 {
			seenSep = true
			continue
		}
		return false
	}
	return true
}

// containsNumberToken reports whether num appears in text bounded by non-numeric
// characters — a digit OR a decimal/thousands separator counts as "attached", so
// "5" matches "have 5 cats" but neither "500" nor "3.5".
func containsNumberToken(text, num string) bool {
	for i := 0; ; {
		j := strings.Index(text[i:], num)
		if j < 0 {
			return false
		}
		j += i
		before := j == 0 || !numAttached(text[j-1])
		after := j+len(num) >= len(text) || !numAttached(text[j+len(num)])
		if before && after {
			return true
		}
		i = j + 1
	}
}

func isDigit(b byte) bool { return b >= '0' && b <= '9' }

// numAttached reports whether b would make an adjacent digit run part of a
// DIFFERENT number: another digit, a decimal/thousands separator, or a leading
// sign. Counting '-'/'+' as attached stops the expected "5" from matching the
// negation "-5" (a genuinely wrong answer) and stops a number embedded in a
// hyphenated token ("order-42") from being credited.
func numAttached(b byte) bool {
	return isDigit(b) || b == '.' || b == ',' || b == '-' || b == '+'
}

// median returns the median of latency values (0 for empty input).
func median(vals []int64) int64 {
	if len(vals) == 0 {
		return 0
	}
	cp := make([]int64, len(vals))
	copy(cp, vals)
	sort.Slice(cp, func(i, j int) bool { return cp[i] < cp[j] })
	mid := len(cp) / 2
	if len(cp)%2 == 1 {
		return cp[mid]
	}
	return (cp[mid-1] + cp[mid]) / 2
}
