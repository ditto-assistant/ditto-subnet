package scorer

import (
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// pairedBank is a synthetic v13 paired bank: one metamorphic group (base,
// renderer-invariant, distractor-invariant, causal counterfactual), one
// decision twin (a restraint case whose right move is a grounded decline plus
// the matched case where the same surface has evidence and demands an answer),
// and one as-of twin (two point-in-time readings of one fact whose answers
// differ). Every expected answer is distinct so "identical answer" can only
// come from an evidence-independent default.
type bankCase struct {
	id, kind, group, relation, twinRelation, expected string
	// wantDecision is the decision class a correct response shows.
	wantDecision string
}

var pairedBank = []bankCase{
	{id: "m-base", kind: protocol.KindMemory, group: "g1", relation: RelationBase, expected: "1200", wantDecision: DecisionAnswer},
	{id: "m-renderer", kind: protocol.KindMemory, group: "g1", relation: RelationRendererInvariant, expected: "1200", wantDecision: DecisionAnswer},
	{id: "m-distractor", kind: protocol.KindMemory, group: "g1", relation: RelationDistractorInvariant, expected: "1200", wantDecision: DecisionAnswer},
	{id: "m-counterfactual", kind: protocol.KindMemory, group: "g1", relation: RelationCausalCounterfactual, expected: "900", wantDecision: DecisionAnswer},
	{id: "d-restraint", kind: protocol.KindMemory, group: "d1", twinRelation: protocol.TwinRelationDecision, expected: "", wantDecision: DecisionAbstain},
	{id: "d-answer", kind: protocol.KindMemory, group: "d1", twinRelation: protocol.TwinRelationDecision, expected: "lisbon", wantDecision: DecisionAnswer},
	{id: "a-early", kind: protocol.KindMemory, group: "a1", twinRelation: protocol.TwinRelationAsOf, expected: "blue", wantDecision: DecisionAnswer},
	{id: "a-late", kind: protocol.KindMemory, group: "a1", twinRelation: protocol.TwinRelationAsOf, expected: "green", wantDecision: DecisionAnswer},
	// Tool-side decision twin: the same surface once demands restraint (reply,
	// call nothing) and once demands the action.
	{id: "t-restraint", kind: protocol.KindTool, group: "t1", twinRelation: protocol.TwinRelationDecision, expected: "noted", wantDecision: DecisionAnswer},
	{id: "t-act", kind: protocol.KindTool, group: "t1", twinRelation: protocol.TwinRelationDecision, expected: "", wantDecision: DecisionAct},
}

// strategy maps a bank case to the (answer, decision) a harness produces.
type strategy func(bankCase) (answer, decision string)

func oracle(c bankCase) (string, string) { return c.expected, c.wantDecision }

// alwaysAnswer answers every case with the LATEST state of its fact: an
// ingest-time compilation that keeps one value per fact, so both as-of
// readings get the late value, the counterfactual gets the base value, and the
// restraint case gets a confident (fabricated) answer.
func alwaysAnswer(c bankCase) (string, string) {
	switch c.id {
	case "m-counterfactual":
		return "1200", DecisionAnswer
	case "a-early":
		return "green", DecisionAnswer
	case "d-restraint":
		return "lisbon", DecisionAnswer
	case "t-act":
		return "noted", DecisionAnswer
	}
	return c.expected, DecisionAnswer
}

func alwaysAbstain(bankCase) (string, string) { return "i don't have that", DecisionAbstain }

func alwaysAct(bankCase) (string, string) { return "", DecisionAct }

// runBank grades a strategy against the bank with the simplest deterministic
// rule (exact match on the expected answer / decision) and returns the scored
// population plus the twin evidence the caller would assemble.
func runBank(s strategy) ([]protocol.CaseScore, map[string]TwinEvidence) {
	perCase := make([]protocol.CaseScore, 0, len(pairedBank))
	evidence := map[string]TwinEvidence{}
	for _, c := range pairedBank {
		answer, decision := s(c)
		score := 0.0
		switch {
		case c.wantDecision == DecisionAbstain && decision == DecisionAbstain:
			score = 1
		case c.wantDecision == DecisionAnswer && decision == DecisionAnswer && answer == c.expected:
			score = 1
		case c.wantDecision == DecisionAct && decision == DecisionAct:
			score = 1
		}
		perCase = append(perCase, protocol.CaseScore{
			CaseID: c.id, Kind: c.kind, Category: "bank", Score: score, Correct: score >= 0.5,
			Relation: c.relation, Called: []string{}, Expected: []string{},
		})
		evidence[c.id] = TwinEvidence{Group: c.group, Relation: c.relation, TwinRelation: c.twinRelation, Answer: answer, Decision: decision}
	}
	return perCase, evidence
}

func bankMean(perCase []protocol.CaseScore) float64 {
	sum := 0.0
	for _, cs := range perCase {
		sum += cs.Score
	}
	return sum / float64(len(perCase))
}

// pairedMean is the mean over the PAIRED bank: the twin members and the base +
// counterfactual pair. The renderer/distractor invariants are deliberately
// outside it -- an evidence-independent default that happens to read the base
// value keeps them, which is the "solver capped at 0.5 of the group" bound.
func pairedMean(perCase []protocol.CaseScore) float64 {
	paired := map[string]bool{}
	for _, c := range pairedBank {
		if c.twinRelation != "" || c.relation == RelationBase || c.relation == RelationCausalCounterfactual {
			paired[c.id] = true
		}
	}
	sum, n := 0.0, 0
	for _, cs := range perCase {
		if paired[cs.CaseID] {
			sum += cs.Score
			n++
		}
	}
	return sum / float64(n)
}

func enforceConfig(rule TwinRule) TwinPostPassConfig {
	return TwinPostPassConfig{Posture: TwinPostureEnforce, Rule: rule}
}

func TestTwinPostPassBaselinesZeroAndOracleFull(t *testing.T) {
	for _, rule := range []TwinRule{TwinRuleConcordantZero, TwinRulePairProduct} {
		perCase, evidence := runBank(oracle)
		out, summary := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(rule), protocol.BenchVersionV13)
		if got := bankMean(out); got != 1 {
			t.Fatalf("rule %s: oracle bank mean = %v, want 1.0 (%+v)", rule, got, summary)
		}
		if summary.CasesAffected != 0 || summary.Applied || summary.TwinGroupsConcordant != 0 || summary.CounterfactualInsensitive != 0 {
			t.Fatalf("rule %s: oracle was marked: %+v", rule, summary)
		}
		if summary.TwinGroups != 3 || summary.CounterfactualPairs != 1 {
			t.Fatalf("rule %s: oracle summary did not count the bank: %+v", rule, summary)
		}
		for name, s := range map[string]strategy{"always-answer": alwaysAnswer, "always-abstain": alwaysAbstain, "always-act": alwaysAct} {
			perCase, evidence := runBank(s)
			before := pairedMean(perCase)
			out, summary := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(rule), protocol.BenchVersionV13)
			if got := pairedMean(out); got != 0 {
				t.Fatalf("rule %s: %s paired-bank mean = %v (raw %v), want 0: %+v", rule, name, got, before, summary)
			}
			if before == 0 {
				t.Fatalf("rule %s: %s scored 0 before the pass; the bank does not exercise the rule", rule, name)
			}
			if !summary.Applied {
				t.Fatalf("rule %s: %s did not report an applied pass: %+v", rule, name, summary)
			}
		}
	}
}

// The always-answer default keeps the renderer/distractor members: the
// counterfactual rule zeroes only the base + counterfactual pair, so a solver is
// capped at 0.5 of the group and one honest miss recovers 0.5.
func TestTwinPostPassCounterfactualZeroesOnlyThePair(t *testing.T) {
	perCase, evidence := runBank(oracle)
	// An honest harness that answered the counterfactual with the base value
	// but got every invariant member right.
	for i := range perCase {
		if perCase[i].CaseID == "m-counterfactual" {
			perCase[i].Score, perCase[i].Correct = 0, false
			evidence[perCase[i].CaseID] = TwinEvidence{Group: "g1", Relation: RelationCausalCounterfactual, Answer: "1200", Decision: DecisionAnswer}
		}
	}
	out, summary := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(TwinRuleConcordantZero), protocol.BenchVersionV13)
	byID := map[string]protocol.CaseScore{}
	for _, cs := range out {
		byID[cs.CaseID] = cs
	}
	if byID["m-base"].Score != 0 || byID["m-counterfactual"].Score != 0 {
		t.Fatalf("pair not zeroed: %+v", byID)
	}
	if byID["m-renderer"].Score != 1 || byID["m-distractor"].Score != 1 {
		t.Fatalf("invariant members were charged: %+v", byID)
	}
	if !hasNote(byID["m-base"], TwinNoteCounterfactualInsensitive) || !hasNote(byID["m-counterfactual"], TwinNoteCounterfactualInsensitive) {
		t.Fatalf("pair lacks the marker note: %+v", byID)
	}
	if hasNote(byID["m-renderer"], TwinNoteCounterfactualInsensitive) {
		t.Fatalf("renderer member carries the pair marker: %+v", byID["m-renderer"])
	}
	if summary.CounterfactualPairs != 1 || summary.CounterfactualInsensitive != 1 || summary.CasesAffected != 2 {
		t.Fatalf("summary = %+v", summary)
	}
	groupMean := (byID["m-base"].Score + byID["m-renderer"].Score + byID["m-distractor"].Score + byID["m-counterfactual"].Score) / 4
	if groupMean != 0.5 {
		t.Fatalf("group mean = %v, want 0.5", groupMean)
	}
}

func TestTwinPostPassObserveLeavesScoresAndAnnotates(t *testing.T) {
	perCase, evidence := runBank(alwaysAnswer)
	out, summary := ApplyV13TwinPostPass(perCase, evidence, DefaultTwinPostPassConfig(), protocol.BenchVersionV13)
	if len(out) != len(perCase) {
		t.Fatalf("population changed: %d != %d", len(out), len(perCase))
	}
	annotated := 0
	for i := range out {
		if out[i].Score != perCase[i].Score || out[i].Correct != perCase[i].Correct {
			t.Fatalf("observe posture moved a score: %+v -> %+v", perCase[i], out[i])
		}
		if hasNote(out[i], TwinNoteConcordant) || hasNote(out[i], TwinNoteCounterfactualInsensitive) {
			annotated++
		}
		if len(perCase[i].Notes) != 0 {
			t.Fatalf("input notes were mutated: %+v", perCase[i])
		}
	}
	// base+counterfactual pair, both decision twins, and the as-of twin.
	if annotated != 8 {
		t.Fatalf("annotated %d cases, want 8", annotated)
	}
	if summary.Posture != string(TwinPostureObserve) || summary.Applied || summary.CasesAffected != 8 {
		t.Fatalf("summary = %+v", summary)
	}
	if summary.Rule != string(TwinRuleConcordantZero) || summary.RuleRequested != string(TwinRuleConcordantZero) || summary.AutoFallback {
		t.Fatalf("default rule drifted: %+v", summary)
	}
	if len(summary.PerRelation) == 0 {
		t.Fatalf("per-relation means were not published: %+v", summary)
	}
	for _, stat := range summary.PerRelation {
		if stat.Count == 0 {
			t.Fatalf("empty relation stat: %+v", stat)
		}
	}
}

func TestTwinPostPassLeavesEarlierVersionsUntouched(t *testing.T) {
	for _, version := range []int{protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11, protocol.BenchVersionV12} {
		perCase, evidence := runBank(alwaysAnswer)
		snapshot := make([]protocol.CaseScore, len(perCase))
		copy(snapshot, perCase)
		out, summary := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(TwinRuleConcordantZero), version)
		if summary != nil {
			t.Fatalf("v%d produced a twin summary: %+v", version, summary)
		}
		if !reflect.DeepEqual(out, snapshot) || len(out) != len(perCase) || &out[0] != &perCase[0] {
			t.Fatalf("v%d population changed: %+v", version, out)
		}
	}
}

func TestTwinPostPassPairProductAndAutoFallback(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "a", Kind: protocol.KindMemory, Score: 0.8, Correct: true},
		{CaseID: "b", Kind: protocol.KindMemory, Score: 0.5, Correct: true},
	}
	evidence := map[string]TwinEvidence{
		"a": {Group: "x", TwinRelation: protocol.TwinRelationAsOf, Answer: "same", Decision: DecisionAnswer},
		"b": {Group: "x", TwinRelation: protocol.TwinRelationAsOf, Answer: "same", Decision: DecisionAnswer},
	}
	cfg := TwinPostPassConfig{Posture: TwinPostureEnforce, Rule: TwinRuleConcordantZero, HonestConcordantErrorRate: 0.06}
	rule, fallback := cfg.EffectiveRule()
	if rule != TwinRulePairProduct || !fallback {
		t.Fatalf("honest concordant-error rate above %v did not fall back: %s %v", TwinHonestConcordantErrorFallback, rule, fallback)
	}
	out, summary := ApplyV13TwinPostPass(perCase, evidence, cfg, protocol.BenchVersionV13)
	if out[0].Score != 0.4 || out[1].Score != 0.4 || out[0].Correct || out[1].Correct {
		t.Fatalf("pair product not applied: %+v", out)
	}
	if !summary.AutoFallback || summary.Rule != string(TwinRulePairProduct) || summary.RuleRequested != string(TwinRuleConcordantZero) {
		t.Fatalf("summary = %+v", summary)
	}
	// At or below the threshold the requested rule stands.
	cfg.HonestConcordantErrorRate = TwinHonestConcordantErrorFallback
	if rule, fallback := cfg.EffectiveRule(); rule != TwinRuleConcordantZero || fallback {
		t.Fatalf("rate at threshold fell back: %s %v", rule, fallback)
	}
	// Pair-product requested explicitly never reports a fallback.
	cfg = TwinPostPassConfig{Posture: TwinPostureEnforce, Rule: TwinRulePairProduct, HonestConcordantErrorRate: 0.5}
	if rule, fallback := cfg.EffectiveRule(); rule != TwinRulePairProduct || fallback {
		t.Fatalf("explicit pair-product = %s %v", rule, fallback)
	}
}

func TestTwinPostPassSkipsUndeliveredAndMixedGroups(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "a", Kind: protocol.KindMemory, Score: 1, Correct: true},
		{CaseID: "b", Kind: protocol.KindMemory, Score: 0, Undelivered: true},
		{CaseID: "base", Kind: protocol.KindMemory, Score: 1, Correct: true},
		{CaseID: "cf", Kind: protocol.KindMemory, Score: 0, Undelivered: true},
		{CaseID: "m1", Kind: protocol.KindMemory, Score: 1, Correct: true},
		{CaseID: "m2", Kind: protocol.KindMemory, Score: 1, Correct: true},
		{CaseID: "solo", Kind: protocol.KindMemory, Score: 1, Correct: true},
	}
	evidence := map[string]TwinEvidence{
		"a":    {Group: "x", TwinRelation: protocol.TwinRelationDecision, Decision: DecisionAbstain},
		"b":    {Group: "x", TwinRelation: protocol.TwinRelationDecision, Decision: DecisionAbstain},
		"base": {Group: "g", Relation: RelationBase, Answer: "7"},
		"cf":   {Group: "g", Relation: RelationCausalCounterfactual, Answer: "7"},
		// A group whose members disagree on the relation is not a twin group.
		"m1":   {Group: "mixed", TwinRelation: protocol.TwinRelationDecision, Decision: DecisionAnswer},
		"m2":   {Group: "mixed", TwinRelation: protocol.TwinRelationAsOf, Answer: "v"},
		"solo": {Group: "single", TwinRelation: protocol.TwinRelationAsOf, Answer: "v"},
	}
	out, summary := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(TwinRuleConcordantZero), protocol.BenchVersionV13)
	for i := range out {
		if out[i].Score != perCase[i].Score {
			t.Fatalf("a transport failure or malformed group moved a score: %+v", out[i])
		}
	}
	if summary.TwinGroups != 0 || summary.CounterfactualPairs != 0 || summary.CasesAffected != 0 {
		t.Fatalf("summary counted an unusable group: %+v", summary)
	}
}

func TestTwinPostPassToolCaseScoresStayConsistent(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "t1", Kind: protocol.KindTool, Category: "restraint", Score: 1, ToolScore: 1, ResultUsage: 1},
		{CaseID: "t2", Kind: protocol.KindTool, Category: "restraint", Score: 0, ToolScore: 0},
	}
	evidence := map[string]TwinEvidence{
		"t1": {Group: "restraint", TwinRelation: protocol.TwinRelationDecision, Decision: DecisionAct},
		"t2": {Group: "restraint", TwinRelation: protocol.TwinRelationDecision, Decision: DecisionAct},
	}
	out, _ := ApplyV13TwinPostPass(perCase, evidence, enforceConfig(TwinRuleConcordantZero), protocol.BenchVersionV13)
	if out[0].Score != 0 || out[0].ToolScore != 0 || out[0].ResultUsage != 0 || out[0].Correct {
		t.Fatalf("tool case derived fields drifted from Score: %+v", out[0])
	}
}

func TestClassifyDecisionAndAssertedAnswer(t *testing.T) {
	if got := ClassifyDecision(protocol.RunResponse{FinalText: "You live in Lisbon."}, nil); got != DecisionAnswer {
		t.Fatalf("plain answer = %s", got)
	}
	if got := ClassifyDecision(protocol.RunResponse{Abstain: true}, nil); got != DecisionAbstain {
		t.Fatalf("abstain flag = %s", got)
	}
	if got := ClassifyDecision(protocol.RunResponse{FinalText: "I don't have a record of that."}, nil); got != DecisionAbstain {
		t.Fatalf("decline lexicon = %s", got)
	}
	// Memory retrieval is how a harness reads; it is not an action.
	memory := []protocol.ObservedToolCall{{Name: "search_memories"}}
	if got := ClassifyDecision(protocol.RunResponse{FinalText: "Lisbon"}, memory); got != DecisionAnswer {
		t.Fatalf("memory tool read as act: %s", got)
	}
	act := []protocol.ObservedToolCall{{Name: "gmail_send"}}
	if got := ClassifyDecision(protocol.RunResponse{FinalText: "Sent.", Abstain: true}, act); got != DecisionAct {
		t.Fatalf("observed action = %s", got)
	}
	// The self-report is the fallback when nothing was observed.
	if got := ClassifyDecision(protocol.RunResponse{ToolCalls: act}, nil); got != DecisionAct {
		t.Fatalf("self-reported action = %s", got)
	}
	if got := AssertedAnswer(protocol.RunResponse{Answer: "  Lisbon. ", FinalText: "You live in Porto."}); got != "lisbon" {
		t.Fatalf("slot not authoritative: %q", got)
	}
	if got := AssertedAnswer(protocol.RunResponse{FinalText: "You live in  Porto"}); got != "you live in porto" {
		t.Fatalf("prose fallback: %q", got)
	}
}

func TestTwinPostPassConfigFromEnvDefaultsToObserve(t *testing.T) {
	for _, key := range []string{TwinPostureEnv, TwinRuleEnv, TwinHonestConcordantErrorRateEnv} {
		t.Setenv(key, "")
	}
	if got := TwinPostPassConfigFromEnv(); got != DefaultTwinPostPassConfig() {
		t.Fatalf("empty env = %+v", got)
	}
	t.Setenv(TwinPostureEnv, "ENFORCE")
	t.Setenv(TwinRuleEnv, "pair_product")
	t.Setenv(TwinHonestConcordantErrorRateEnv, "0.03")
	got := TwinPostPassConfigFromEnv()
	if got.Posture != TwinPostureEnforce || got.Rule != TwinRulePairProduct || got.HonestConcordantErrorRate != 0.03 {
		t.Fatalf("env not honored: %+v", got)
	}
	t.Setenv(TwinPostureEnv, "definitely")
	t.Setenv(TwinRuleEnv, "zero-everything")
	t.Setenv(TwinHonestConcordantErrorRateEnv, "2")
	if got := TwinPostPassConfigFromEnv(); got != DefaultTwinPostPassConfig() {
		t.Fatalf("unrecognized values did not fall back to the safe defaults: %+v", got)
	}
}

// The relation constants are string literals in the generator. Pin them against
// a generated v13 dataset so a rename on either side fails here rather than by
// silently skipping every metamorphic group.
func TestTwinRelationConstantsMatchGenerator(t *testing.T) {
	prof, ok := gen.ProfileForVersion("small", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 small profile unavailable")
	}
	// The small profile may draw no program group; medium always does.
	prof, ok = gen.ProfileForVersion("medium", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 medium profile unavailable")
	}
	artifact, err := gen.GenerateDataset(41, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	known := map[string]bool{RelationBase: true, RelationRendererInvariant: true, RelationDistractorInvariant: true, RelationCausalCounterfactual: true}
	seen := map[string]int{}
	groups := map[string]map[string]bool{}
	for _, mc := range artifact.MemoryCases {
		if mc.V10Provenance == nil {
			continue
		}
		relation := mc.V10Provenance.Relation
		if !known[relation] {
			t.Fatalf("generator relation %q is not a scorer constant", relation)
		}
		seen[relation]++
		group := mc.V10Provenance.MetamorphicGroup
		if groups[group] == nil {
			groups[group] = map[string]bool{}
		}
		groups[group][relation] = true
		// The counterfactual member deliberately carries no TwinGroup (its
		// answer must differ), which is why the pass pairs it through the
		// provenance group rather than CaseScore.TwinGroup.
		if relation == RelationCausalCounterfactual && mc.TwinGroup != "" {
			t.Fatalf("counterfactual member %s carries TwinGroup %q", mc.ID, mc.TwinGroup)
		}
	}
	for relation := range known {
		if seen[relation] == 0 {
			t.Fatalf("generated v13 dataset carries no %q member: %v", relation, seen)
		}
	}
	for group, relations := range groups {
		if !relations[RelationBase] || !relations[RelationCausalCounterfactual] {
			t.Fatalf("group %s lacks a base+counterfactual pair: %v", group, relations)
		}
	}
}
