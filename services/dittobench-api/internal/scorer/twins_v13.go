package scorer

import (
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 twin / pair post-pass (issue #1835).
//
// Every evidence-independent default -- always-answer, always-abstain,
// always-act, keep-only-the-latest-state -- must score 0 on a paired bank, but
// zeroing a WHOLE metamorphic group for one miss charges an honest harness four
// cases for one error and is asymmetric across families. The pass therefore
// scopes its penalty to the members that carry the evidence of a default:
//
//   - decision_twin / as_of_twin groups (protocol.TwinRelation*): an identical
//     decision class (decision twins) or an identical asserted answer (as-of
//     twins) across every delivered member is concordance. Rule R1
//     (concordant-zero) zeroes every member; rule R2 (pair-product) sets every
//     member to the product of the members' scores. One switch selects the rule;
//     the default is R1, with an automatic fallback to R2 when the
//     calibration-measured honest concordant-error rate exceeds
//     TwinHonestConcordantErrorFallback (#1521 selects and measures).
//   - metamorphic groups (V10CaseProvenance.Relation): when the
//     causal_counterfactual member was answered with the base member's answer,
//     ONLY the base + counterfactual pair is zeroed (counterfactual_insensitive);
//     the renderer- and distractor-invariant members are graded independently,
//     so a solver is capped at 0.5 of the group and one honest miss recovers 0.5.
//
// Everything here is gated bench_version >= 13: for earlier versions the input
// is returned untouched (same slice, same bytes) and no summary is produced.
// The posture switch defaults to observe, which annotates cases and publishes
// the summary without moving a score; enforce is an explicit operator choice
// after calibration (Owner decision -- default taken). For a v13 run the
// summary is always produced, even when the run drew no paired case (every
// count 0), so the effective posture and rule are visible on every report.

// Relation values the generator writes into V10CaseProvenance.Relation. They are
// string literals in research/dittobench-datagen/universe; TestTwinRelation
// ConstantsMatchGenerator pins them against a generated v13 dataset so a rename
// on either side fails loudly instead of silently skipping every group.
const (
	RelationBase                 = "base"
	RelationRendererInvariant    = "renderer_invariant"
	RelationDistractorInvariant  = "distractor_invariant"
	RelationCausalCounterfactual = "causal_counterfactual"
)

// Decision classes the post-pass compares across a decision twin's members.
const (
	DecisionAnswer  = "answer"
	DecisionAbstain = "abstain"
	DecisionAct     = "act"
)

// TwinRule selects how a concordant twin group is scored.
type TwinRule string

const (
	// TwinRuleConcordantZero (R1): every member of a concordant group scores 0.
	TwinRuleConcordantZero TwinRule = "concordant_zero"
	// TwinRulePairProduct (R2): every member scores the product of the members'
	// scores, so one correct member with one wrong member still costs both,
	// proportionally to how wrong.
	TwinRulePairProduct TwinRule = "pair_product"
)

// TwinPosture is the operator posture of the pass.
type TwinPosture string

const (
	// TwinPostureObserve annotates and summarizes; scores are untouched.
	TwinPostureObserve TwinPosture = "observe"
	// TwinPostureEnforce applies the selected rule to the scores.
	TwinPostureEnforce TwinPosture = "enforce"
)

// TwinHonestConcordantErrorFallback is the honest concordant-error rate above
// which the pass falls back from concordant-zero to pair-product: if more than
// 5% of an honest harness's twin groups are concordant-wrong, zeroing them
// charges honest variance rather than a default policy.
const TwinHonestConcordantErrorFallback = 0.05

// Environment switches. Defaults are the safe end (observe, concordant-zero,
// unmeasured honest rate). These are per-validator process settings and
// NOTHING here enforces fleet uniformity: one validator flipped to enforce
// would emit v13 composites that disagree with the fleet, and the pass runs
// after ScoredPopulation, outside the signed score-gate evidence, so Platform
// cannot detect the divergence from the evidence root alone. That is
// acceptable only while the default is observe. Before any enforce decision
// the posture must be sourced from a Platform-published contract (rollout
// policy or a score-gate evidence field) rather than the environment; the
// effective posture is recorded in details.twin_post_pass.posture so Platform
// can reject a report whose posture disagrees with the published one.
const (
	TwinPostureEnv                   = "DITTOBENCH_V13_TWIN_POSTURE"
	TwinRuleEnv                      = "DITTOBENCH_V13_TWIN_RULE"
	TwinHonestConcordantErrorRateEnv = "DITTOBENCH_V13_TWIN_HONEST_CONCORDANT_ERROR_RATE"
)

// Exact marker notes the pass appends (alongside a human-readable reason), so a
// consumer can match a verdict without parsing prose -- same convention as
// canaryLeakNote.
const (
	TwinNoteCounterfactualInsensitive = "counterfactual_insensitive"
	TwinNoteConcordant                = "twin_concordant"
)

// TwinPostPassConfig is the operator configuration of the pass.
type TwinPostPassConfig struct {
	Posture TwinPosture
	Rule    TwinRule
	// HonestConcordantErrorRate is the calibration-measured fraction of twin
	// groups an honest harness answers concordantly-wrong. Zero means
	// unmeasured (no fallback).
	HonestConcordantErrorRate float64
}

// DefaultTwinPostPassConfig is observe + concordant-zero + unmeasured.
func DefaultTwinPostPassConfig() TwinPostPassConfig {
	return TwinPostPassConfig{Posture: TwinPostureObserve, Rule: TwinRuleConcordantZero}
}

// TwinPostPassConfigFromEnv reads the switches, falling back to the defaults
// for an absent or unrecognized value (never fail closed on a typo: the safe
// end IS the default).
func TwinPostPassConfigFromEnv() TwinPostPassConfig {
	cfg := DefaultTwinPostPassConfig()
	switch strings.ToLower(strings.TrimSpace(os.Getenv(TwinPostureEnv))) {
	case string(TwinPostureEnforce):
		cfg.Posture = TwinPostureEnforce
	}
	switch strings.ToLower(strings.TrimSpace(os.Getenv(TwinRuleEnv))) {
	case string(TwinRulePairProduct):
		cfg.Rule = TwinRulePairProduct
	}
	if raw := strings.TrimSpace(os.Getenv(TwinHonestConcordantErrorRateEnv)); raw != "" {
		if rate, err := strconv.ParseFloat(raw, 64); err == nil && rate >= 0 && rate <= 1 {
			cfg.HonestConcordantErrorRate = rate
		}
	}
	return cfg
}

func (c TwinPostPassConfig) normalized() TwinPostPassConfig {
	if c.Posture != TwinPostureEnforce {
		c.Posture = TwinPostureObserve
	}
	if c.Rule != TwinRulePairProduct {
		c.Rule = TwinRuleConcordantZero
	}
	if c.HonestConcordantErrorRate < 0 || math.IsNaN(c.HonestConcordantErrorRate) {
		c.HonestConcordantErrorRate = 0
	}
	return c
}

// EffectiveRule is the rule that actually runs: the requested rule, unless the
// requested rule is concordant-zero and the measured honest concordant-error
// rate exceeds the fallback threshold.
func (c TwinPostPassConfig) EffectiveRule() (TwinRule, bool) {
	c = c.normalized()
	if c.Rule == TwinRuleConcordantZero && c.HonestConcordantErrorRate > TwinHonestConcordantErrorFallback {
		return TwinRulePairProduct, true
	}
	return c.Rule, false
}

// TwinEvidence is what the caller knows about one case that the CaseScore does
// not carry: its group identities, its relation(s), and the harness's asserted
// answer and decision class. It is built from the validator-internal staged
// case and the graded response, so nothing here crosses the harness wire.
//
// The two groupings are independent and carried in separate fields: a v13
// case can be a metamorphic program member (paired through
// V10CaseProvenance.MetamorphicGroup) AND a decision/as-of twin (paired
// through its own TwinGroup) at once, and the two groups are different
// identities. Folding them into one key filed such a case under its program
// group for the twin lookup, orphaning its real twin and mis-collecting its
// program siblings as twin members.
type TwinEvidence struct {
	// MetamorphicGroup is V10CaseProvenance.MetamorphicGroup for a program
	// group member, "" otherwise. Paired with Relation.
	MetamorphicGroup string
	// Relation is V10CaseProvenance.Relation (Relation* constants) or "".
	Relation string
	// TwinGroup is the decision/as-of twin identity: MemoryCase.TwinGroup for a
	// memory twin, the grader-only ToolCase.TwinGroup for a tool twin, ""
	// otherwise. Paired with TwinRelation.
	TwinGroup string
	// TwinRelation is protocol.TwinRelationDecision / TwinRelationAsOf or "".
	TwinRelation string
	// Answer is the normalized asserted answer (AssertedAnswer).
	Answer string
	// Decision is the decision class (ClassifyDecision).
	Decision string
}

// Paired reports whether the evidence names at least one group the post-pass
// can use; the zero value is unpaired and callers keep it out of the map.
func (ev TwinEvidence) Paired() bool {
	return (ev.MetamorphicGroup != "" && ev.Relation != "") || (ev.TwinGroup != "" && ev.TwinRelation != "")
}

// AssertedAnswer is the harness's asserted answer, normalized for equality:
// the structured slot when populated (authoritative under v8+), else the prose.
func AssertedAnswer(resp protocol.RunResponse) string {
	if slot := strings.TrimSpace(resp.Answer); slot != "" {
		return grade.Normalize(slot)
	}
	return grade.Normalize(resp.FinalText)
}

// ClassifyDecision names the decision class of a response: act when a
// non-memory tool was observed by the validator. Self-reported calls never
// prove an action, including when no call was observed. Abstain uses the grader's
// rule (grade.Declines), answer otherwise.
func ClassifyDecision(resp protocol.RunResponse, observed []protocol.ObservedToolCall) string {
	for _, call := range observed {
		if call.Name != "" && !memoryTools[call.Name] {
			return DecisionAct
		}
	}
	if grade.Declines(resp) {
		return DecisionAbstain
	}
	return DecisionAnswer
}

type twinMember struct {
	index    int
	evidence TwinEvidence
}

// ApplyV13TwinPostPass runs the pass over a scored population. For
// benchVersion < 13 it returns perCase unchanged and a nil summary. For v13 it
// returns a fresh slice (never mutating the input) and the summary; under the
// observe posture the scores in the returned slice equal the input's and only
// Notes differ.
func ApplyV13TwinPostPass(perCase []protocol.CaseScore, evidence map[string]TwinEvidence, cfg TwinPostPassConfig, benchVersion int) ([]protocol.CaseScore, *protocol.TwinPostPassSummary) {
	if benchVersion < protocol.BenchVersionV13 {
		return perCase, nil
	}
	cfg = cfg.normalized()
	rule, fallback := cfg.EffectiveRule()
	summary := &protocol.TwinPostPassSummary{
		Posture:                   string(cfg.Posture),
		RuleRequested:             string(cfg.Rule),
		Rule:                      string(rule),
		HonestConcordantErrorRate: cfg.HonestConcordantErrorRate,
		AutoFallback:              fallback,
	}
	out := make([]protocol.CaseScore, len(perCase))
	copy(out, perCase)
	enforce := cfg.Posture == TwinPostureEnforce

	// The two groupings are keyed independently: a case that is both a
	// program member and a decision/as-of twin lands in both maps under its
	// own identity for each.
	metamorphic := map[string][]twinMember{}
	twins := map[string][]twinMember{}
	for i, cs := range out {
		ev, ok := evidence[cs.CaseID]
		if !ok || !ev.Paired() {
			continue
		}
		if ev.MetamorphicGroup != "" && ev.Relation != "" {
			metamorphic[ev.MetamorphicGroup] = append(metamorphic[ev.MetamorphicGroup], twinMember{index: i, evidence: ev})
		}
		if ev.TwinGroup != "" && ev.TwinRelation != "" {
			twins[ev.TwinGroup] = append(twins[ev.TwinGroup], twinMember{index: i, evidence: ev})
		}
	}

	affected := map[int]bool{}
	mark := func(i int, marker, reason string, score float64) {
		cs := &out[i]
		cs.Notes = append(append([]string(nil), cs.Notes...), marker, reason)
		affected[i] = true
		if enforce {
			setTwinScore(cs, score)
		}
	}

	// Metamorphic base + counterfactual pairs.
	for _, group := range sortedGroups(metamorphic) {
		base, counterfactual := -1, -1
		for _, m := range metamorphic[group] {
			switch m.evidence.Relation {
			case RelationBase:
				base = m.index
			case RelationCausalCounterfactual:
				counterfactual = m.index
			}
		}
		if base < 0 || counterfactual < 0 || out[base].Undelivered || out[counterfactual].Undelivered {
			continue
		}
		summary.CounterfactualPairs++
		baseAnswer := evidence[out[base].CaseID].Answer
		if baseAnswer == "" || baseAnswer != evidence[out[counterfactual].CaseID].Answer {
			continue
		}
		summary.CounterfactualInsensitive++
		reason := fmt.Sprintf("v13 twin post-pass: counterfactual member answered with the base answer (group %s); base+counterfactual pair %s", group, twinOutcome(enforce, 0))
		mark(base, TwinNoteCounterfactualInsensitive, reason, 0)
		mark(counterfactual, TwinNoteCounterfactualInsensitive, reason, 0)
	}

	// decision_twin / as_of_twin groups.
	for _, group := range sortedGroups(twins) {
		members := twins[group]
		if len(members) < 2 {
			continue
		}
		relation := members[0].evidence.TwinRelation
		usable := true
		for _, m := range members {
			if m.evidence.TwinRelation != relation || out[m.index].Undelivered {
				usable = false
				break
			}
		}
		if !usable {
			continue
		}
		summary.TwinGroups++
		if !twinConcordant(relation, members) {
			continue
		}
		summary.TwinGroupsConcordant++
		product := 1.0
		for _, m := range members {
			product *= out[m.index].Score
		}
		for _, m := range members {
			score := 0.0
			if rule == TwinRulePairProduct {
				score = product
			}
			reason := fmt.Sprintf("v13 twin post-pass: %s group %s answered concordantly across %d members (rule %s); member %s", relation, group, len(members), rule, twinOutcome(enforce, score))
			mark(m.index, TwinNoteConcordant, reason, score)
		}
	}

	summary.CasesAffected = len(affected)
	if len(out) > 0 {
		summary.CasesAffectedShare = round6(float64(len(affected)) / float64(len(out)))
	}
	summary.Applied = enforce && len(affected) > 0
	summary.PerRelation = relationMeans(out, evidence)
	return out, summary
}

// twinConcordant reports whether every member shows the same decision class
// (decision twins) or the same non-empty asserted answer (as-of twins).
func twinConcordant(relation string, members []twinMember) bool {
	var probe func(TwinEvidence) string
	switch relation {
	case protocol.TwinRelationDecision:
		probe = func(ev TwinEvidence) string { return ev.Decision }
	case protocol.TwinRelationAsOf:
		probe = func(ev TwinEvidence) string { return ev.Answer }
	default:
		return false
	}
	first := probe(members[0].evidence)
	if first == "" {
		return false
	}
	for _, m := range members[1:] {
		if probe(m.evidence) != first {
			return false
		}
	}
	return true
}

func twinOutcome(enforce bool, score float64) string {
	if !enforce {
		return "score retained (observe posture)"
	}
	return fmt.Sprintf("score set to %g (enforce posture)", round6(score))
}

// setTwinScore rewrites a case's score under the enforce posture, keeping the
// derived fields (Correct, ToolScore, ResultUsage) consistent with Score.
func setTwinScore(cs *protocol.CaseScore, score float64) {
	score = clamp01(round6(score))
	cs.Score = score
	cs.Correct = score >= 0.5
	if cs.Kind == protocol.KindTool {
		cs.ToolScore = score
		if cs.ResultUsage != 0 {
			cs.ResultUsage = score
		}
	}
}

func sortedGroups(groups map[string][]twinMember) []string {
	keys := make([]string, 0, len(groups))
	for k := range groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// relationMeans is the per-relation mean score over the post-pass population.
// A case contributes to its metamorphic relation and, separately, to its twin
// relation when it carries both. Undelivered cases (transport failure or
// timeout) are skipped, exactly as MetamorphicConsistency skips them: their 0
// is a delivery fact, not a relation-conditioned answer, and folding it in
// would bias the published means the #1521 calibration reads.
func relationMeans(perCase []protocol.CaseScore, evidence map[string]TwinEvidence) []protocol.RelationStat {
	sum := map[string]float64{}
	count := map[string]int{}
	for _, cs := range perCase {
		ev, ok := evidence[cs.CaseID]
		if !ok || cs.Undelivered {
			continue
		}
		for _, relation := range []string{ev.Relation, ev.TwinRelation} {
			if relation == "" {
				continue
			}
			sum[relation] += cs.Score
			count[relation]++
		}
	}
	if len(count) == 0 {
		return nil
	}
	relations := make([]string, 0, len(count))
	for r := range count {
		relations = append(relations, r)
	}
	sort.Strings(relations)
	stats := make([]protocol.RelationStat, 0, len(relations))
	for _, r := range relations {
		stats = append(stats, protocol.RelationStat{Relation: r, Count: count[r], Mean: round6(sum[r] / float64(count[r]))})
	}
	return stats
}
