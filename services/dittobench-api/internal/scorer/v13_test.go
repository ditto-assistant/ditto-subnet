package scorer

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func rawArgs(t *testing.T, args map[string]any) json.RawMessage {
	t.Helper()
	b, err := json.Marshal(args)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func v13Score(t *testing.T, c protocol.ToolCase, resp protocol.RunResponse) protocol.CaseScore {
	t.Helper()
	return ScoreToolCaseObservedForVersion(c, resp, true, resp.ToolCalls, ScopeScored, protocol.BenchVersionV13)
}

// TestArgClaimSatisfiedAcceptsHonestParaphrase pins the #1847 reward side:
// each claim kind accepts the paraphrases an honest harness emits and fails
// only on the distractor surface or candidate-stuffing.
func TestArgClaimSatisfiedAcceptsHonestParaphrase(t *testing.T) {
	fact := protocol.Claim{Kind: "fact_update", Expected: "handoff is Monday", Accept: []string{"handoff: Monday", "handoff -> Monday"}}
	for _, ok := range []string{
		"handoff is Monday", "Handoff: Monday", "handoff -> Monday", "The handoff moved to Monday.",
		"Monday handoff (was Friday)", "Handoff scratchpad: we now hand off on Monday",
	} {
		if !argClaimSatisfied(ok, fact) {
			t.Errorf("fact paraphrase rejected: %q", ok)
		}
	}
	for _, bad := range []string{"handoff is Friday", "Monday", "handoff", "the reviewer is Monday-ish"} {
		if argClaimSatisfied(bad, fact) {
			t.Errorf("fact miss accepted: %q", bad)
		}
	}
	facts := protocol.Claim{Kind: "facts", Expected: "handoff is Monday; reviewer is Kes"}
	if !argClaimSatisfied("Handoff now Monday, reviewer Kes.", facts) || argClaimSatisfied("Handoff now Monday.", facts) {
		t.Error("multi-fact claim graded wrongly")
	}

	name := protocol.Claim{Kind: "entity", Expected: "Harborlight Review Programme", Accept: []string{"harborlight workstream"}, Forbidden: []string{"Westden Collective"}}
	for _, ok := range []string{"Harborlight Review Programme", "Acme — harborlight workstream review", "Glenford Partners: Harborlight Review Programme"} {
		if !argClaimSatisfied(ok, name) {
			t.Errorf("entity paraphrase rejected: %q", ok)
		}
	}
	if argClaimSatisfied("Westden Collective harborlight workstream", name) {
		t.Error("distractor party accepted inside the workflow name")
	}
	if argClaimSatisfied("some other workflow", name) {
		t.Error("unrelated name accepted")
	}

	email := protocol.Claim{Kind: "email", Expected: "dana@x.com", Accept: []string{"Dana"}}
	for _, ok := range []string{"dana@x.com", "Dana Whitfield <dana@x.com>", "DANA@X.COM", "\"Dana\" <dana@x.com>, "} {
		if !argClaimSatisfied(ok, email) {
			t.Errorf("email form rejected: %q", ok)
		}
	}
	for _, bad := range []string{"Dana", "old.dana@x.com", "dana@x.com.au"} {
		if argClaimSatisfied(bad, email) {
			t.Errorf("non-canonical recipient accepted: %q", bad)
		}
	}

	enum := protocol.Claim{Kind: "enum", Expected: "IBM Plex Sans"}
	if !argClaimSatisfied("ibm plex sans", enum) || !argClaimSatisfied("IBM-Plex-Sans", enum) || argClaimSatisfied("IBM Plex Mono", enum) {
		t.Error("enum canonicalization graded wrongly")
	}
	id := protocol.Claim{Kind: "id", Expected: "cabc123", Forbidden: []string{"cdef456"}}
	if !argClaimSatisfied("cabc123", id) || argClaimSatisfied("cdef456", id) || argClaimSatisfied("cabc1234", id) {
		t.Error("id claim graded wrongly")
	}
	set := protocol.Claim{Kind: "set", Expected: "lead@x.com; publish notes"}
	if !argClaimSatisfied("1. review with Lead <lead@x.com> 2. publish notes", set) || argClaimSatisfied("publish notes only", set) {
		t.Error("set claim graded wrongly")
	}
	stuffed := strings.Repeat("Harborlight Review Programme Westmere Kestrel Orchard Juniper ", 6)
	if argClaimSatisfied(stuffed, protocol.Claim{Kind: "entity", Expected: "Kestrel"}) {
		t.Error("candidate stuffing accepted")
	}
	if argClaimSatisfied("", fact) || argClaimSatisfied("x", protocol.Claim{Kind: "entity"}) {
		t.Error("empty inputs accepted")
	}
}

// TestArgValueEqualUnchangedForV12 pins the frozen v2..v12 comparator so the
// claim grader cannot have leaked into it.
func TestArgValueEqualUnchangedForV12(t *testing.T) {
	table := []struct {
		got  any
		want string
		ok   bool
	}{
		{"news about Tokyo", "Tokyo", true},
		{"TOKYO", "tokyo", true},
		{"yellow", "low", false},
		{"5.5", "5", false},
		{"have 5 cats", "5", true},
		{"handoff: Monday", "handoff is Monday", false},
		{"Dana <dana@x.com>", "dana@x.com", true},
		{"Dana", "dana@x.com", false},
		{strings.Repeat("Tokyo Paris London Berlin ", 4), "Tokyo", false},
		{"", "x", false},
		{"x", "", false},
		{3, "3", true},
	}
	for _, tt := range table {
		if got := argValueEqual(tt.got, tt.want); got != tt.ok {
			t.Errorf("argValueEqual(%v, %q) = %v, want %v", tt.got, tt.want, got, tt.ok)
		}
	}
}

func v13UpdateCase() protocol.ToolCase {
	claim := protocol.Claim{Kind: "fact_update", Expected: "handoff is Monday", Critical: true}
	return protocol.ToolCase{
		ID: "upd", Category: "world_memory_update", MaxToolCalls: 15, AllowExtraTools: true, FuzzyTrajectory: true,
		ExpectedTools: []protocol.ToolSpec{{
			Name: "update_memory", RequiredArgs: map[string]string{"pair_id": "cnote", "content": "handoff is Monday"},
			RequiredArgClaims: map[string]protocol.Claim{"content": claim},
		}},
		AlternativeExpectedTools: [][]protocol.ToolSpec{{
			{Name: "delete_memory", RequiredArgs: map[string]string{"pair_id": "cnote"}},
			{Name: "save_memory", RequiredArgClaims: map[string]protocol.Claim{"content": claim}},
		}},
	}
}

// TestV13UpdateAcceptsParaphraseAndDeletePlusSave: the update case credits an
// in-place update whose content paraphrases the fact, credits delete + save as
// an end-state-equivalent alternative, and rejects the wrong value; the same
// case graded under v12 keeps the exact-content rule.
func TestV13UpdateAcceptsParaphraseAndDeletePlusSave(t *testing.T) {
	c := v13UpdateCase()
	paraphrase := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{
		{Name: "search_memories"},
		{Name: "update_memory", Args: rawArgs(t, map[string]any{"pair_id": "cnote", "content": "Handoff scratchpad — handoff moved to Monday."})},
	}}
	if got := v13Score(t, c, paraphrase); got.Score != 1 {
		t.Fatalf("paraphrased update scored %v: %v", got.Score, got.Notes)
	}
	deleteSave := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{
		{Name: "delete_memory", Args: rawArgs(t, map[string]any{"pair_id": "cnote"})},
		{Name: "save_memory", Args: rawArgs(t, map[string]any{"content": "Handoff for the workstream: Monday"})},
	}}
	got := v13Score(t, c, deleteSave)
	if got.Score != 1 || !strings.Contains(strings.Join(got.Notes, " "), "alternative") {
		t.Fatalf("delete+save scored %v: %v", got.Score, got.Notes)
	}
	wrong := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{
		{Name: "update_memory", Args: rawArgs(t, map[string]any{"pair_id": "cnote", "content": "handoff is Friday"})},
	}}
	if got := v13Score(t, c, wrong); got.Score != 0 {
		t.Fatalf("wrong day scored %v", got.Score)
	}
	// v12: the frozen exact-content rule, no alternative.
	v12 := ScoreToolCaseObservedForVersion(c, paraphrase, true, paraphrase.ToolCalls, ScopeScored, protocol.BenchVersionV12)
	if v12.Score != 0 {
		t.Fatalf("v12 must keep exact-content grading, scored %v", v12.Score)
	}
	v12Alt := ScoreToolCaseObservedForVersion(c, deleteSave, true, deleteSave.ToolCalls, ScopeScored, protocol.BenchVersionV12)
	if v12Alt.Score != 0 {
		t.Fatalf("v12 must not honor alternatives, scored %v", v12Alt.Score)
	}
}

// TestV13ForbiddenToolsAndForbiddenClaimValues: a call to a case's forbidden
// tool zeroes it, and a claim's forbidden value zeroes it across every call,
// not only the best one.
func TestV13ForbiddenToolsAndForbiddenClaimValues(t *testing.T) {
	move := protocol.ToolCase{
		ID: "mv", Category: datagen.V13StateDependentCalendarCategory, MaxToolCalls: 15, AllowExtraTools: true, FuzzyTrajectory: true,
		ExpectedTools:  []protocol.ToolSpec{{Name: "calendar_search_events", RequiredArgs: map[string]string{"query": "kestrel review"}, RequiredArgClaims: map[string]protocol.Claim{"query": {Kind: "entity", Expected: "kestrel review"}}}},
		ForbiddenTools: []string{"calendar_create_event"},
	}
	both := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{
		{Name: "calendar_search_events", Args: rawArgs(t, map[string]any{"query": "kestrel review"})},
		{Name: "calendar_create_event", Args: rawArgs(t, map[string]any{"title": "kestrel review"})},
	}}
	if got := v13Score(t, move, both); got.Score != 0 {
		t.Fatalf("search-and-create hedge scored %v on a move case", got.Score)
	}
	if got := v13Score(t, move, protocol.RunResponse{ToolCalls: both.ToolCalls[:1]}); got.Score != 1 {
		t.Fatalf("search-only scored %v: %v", got.Score, got.Notes)
	}
	del := protocol.ToolCase{
		ID: "del", Category: "world_memory_delete", MaxToolCalls: 15, AllowExtraTools: true, FuzzyTrajectory: true,
		ExpectedTools: []protocol.ToolSpec{{Name: "delete_memory", RequiredArgs: map[string]string{"pair_id": "cnote"}, RequiredArgClaims: map[string]protocol.Claim{"pair_id": {Kind: "id", Expected: "cnote", Forbidden: []string{"cemail"}}}}},
	}
	cleanup := protocol.RunResponse{ToolCalls: []protocol.ObservedToolCall{
		{Name: "delete_memory", Args: rawArgs(t, map[string]any{"pair_id": "cnote"})},
		{Name: "delete_memory", Args: rawArgs(t, map[string]any{"pair_id": "cemail"})},
	}}
	if got := v13Score(t, del, cleanup); got.Score != 0 {
		t.Fatalf("deleting the protected contact pair scored %v", got.Score)
	}
	if got := v13Score(t, del, protocol.RunResponse{ToolCalls: cleanup.ToolCalls[:1]}); got.Score != 1 {
		t.Fatalf("clean delete scored %v: %v", got.Score, got.Notes)
	}
}

// TestV13MemoryEffectRemovesAnyTextCredit: the v13 memory-read case credits
// only an answer that carries the planted value; the pre-v13 routing rule
// (any non-empty text) is unchanged for v12.
func TestV13MemoryEffectRemovesAnyTextCredit(t *testing.T) {
	c := protocol.ToolCase{ID: "read", Category: "memory_lookup", ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}}, MaxToolCalls: 2, EffectAnswer: "LFU-229"}
	if got := v13Score(t, c, protocol.RunResponse{FinalText: "I looked it up for you."}); got.Score != 0 {
		t.Fatalf("substantive-but-wrong text scored %v", got.Score)
	}
	if got := v13Score(t, c, protocol.RunResponse{FinalText: "The cabin guest wifi is LFU-229."}); got.Score != 1 {
		t.Fatalf("correct prose scored %v: %v", got.Score, got.Notes)
	}
	if got := v13Score(t, c, protocol.RunResponse{Answer: "lfu-229"}); got.Score != 1 {
		t.Fatalf("correct slot scored %v", got.Score)
	}
	if got := v13Score(t, c, protocol.RunResponse{FinalText: "LFU-2290"}); got.Score != 0 {
		t.Fatalf("adjacent value scored %v", got.Score)
	}
	misroute := protocol.RunResponse{FinalText: "LFU-229", ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}
	if got := v13Score(t, c, misroute); got.Score != 0 {
		t.Fatalf("misroute scored %v", got.Score)
	}
	numeric := c
	numeric.EffectAnswer = "4471"
	if got := v13Score(t, numeric, protocol.RunResponse{FinalText: "Unit 4,471 at Larkhill."}); got.Score != 1 {
		t.Fatalf("numeric with separator scored %v", got.Score)
	}
	if got := v13Score(t, numeric, protocol.RunResponse{FinalText: "Unit 44710."}); got.Score != 0 {
		t.Fatalf("numeric superstring scored %v", got.Score)
	}
	// v12 contract: the frozen routing-only rule.
	legacy := c
	legacy.EffectAnswer = ""
	v12 := ScoreToolCaseObservedForVersion(legacy, protocol.RunResponse{FinalText: "I looked it up for you."}, true, nil, ScopeScored, protocol.BenchVersionV12)
	if v12.Score != 1 {
		t.Fatalf("v12 routing-only credit moved: %v", v12.Score)
	}
	// A v13 memory-read case without a planted value is never credited on text.
	if got := v13Score(t, legacy, protocol.RunResponse{FinalText: "Anything."}); got.Score != 0 {
		t.Fatalf("v13 routing-only credit survived: %v", got.Score)
	}
}

// TestV13RestraintTextGrading pins the AnswerClarify and no-call rules: the
// clarifying turn must name the slot and cite a searched record, a generic
// question scores 0, acting scores 0, confirm-and-act phrasing passes on the
// ask half, and a grounded no-call answer must be substantive.
func TestV13RestraintTextGrading(t *testing.T) {
	ask := protocol.ToolCase{
		ID: "ask", Category: datagen.V13RestraintCategoryPrefix + "effort", MaxToolCalls: 0,
		Restraint: &protocol.RestraintClaim{
			Kind: protocol.RestraintClarifyFirst, ForbiddenTools: []string{"set_reasoning_effort"},
			Accept:    []string{"effort", "reasoning", "level", "low", "medium", "high", "esfuerzo"},
			Grounding: []string{"low", "high"},
		},
	}
	cases := []struct {
		name string
		resp protocol.RunResponse
		want float64
	}{
		{"generic", protocol.RunResponse{FinalText: "Sure — what would you like?"}, 0},
		{"slot without grounding", protocol.RunResponse{FinalText: "Which reasoning effort would you like?"}, 0},
		{"slot and grounding", protocol.RunResponse{FinalText: "You've gone back and forth between low and high — which effort should be the default?"}, 1},
		{"multilingual slot", protocol.RunResponse{FinalText: "¿Qué esfuerzo prefieres: low o high?"}, 1},
		{"confirm and act phrasing", protocol.RunResponse{FinalText: "Set it to high — your usual?"}, 1},
		{"acted", protocol.RunResponse{FinalText: "Which level?", ToolCalls: []protocol.ObservedToolCall{{Name: "set_reasoning_effort", Args: rawArgs(t, map[string]any{"effort": "high"})}}}, 0},
		{"memory read then ask", protocol.RunResponse{FinalText: "No default saved — low or high effort?", ToolCalls: []protocol.ObservedToolCall{{Name: "search_memories"}}}, 1},
		{"slot only in answer field", protocol.RunResponse{Answer: "clarify: effort level (low/high)?"}, 1},
	}
	for _, tt := range cases {
		if got := v13Score(t, ask, tt.resp); got.Score != tt.want {
			t.Errorf("%s: scored %v, want %v (%v)", tt.name, got.Score, tt.want, got.Notes)
		}
	}
	noCall := protocol.ToolCase{
		ID: "nc", Category: datagen.V13RestraintCategoryPrefix + "declarative_preference", MaxToolCalls: 0,
		Restraint: &protocol.RestraintClaim{Kind: protocol.RestraintNoCall, ForbiddenTools: []string{"set_accent_color"}, Grounding: []string{"teal"}},
	}
	if got := v13Score(t, noCall, protocol.RunResponse{FinalText: "Your accent is already teal — nothing to change."}); got.Score != 1 {
		t.Errorf("grounded acknowledgement scored %v: %v", got.Score, got.Notes)
	}
	if got := v13Score(t, noCall, protocol.RunResponse{FinalText: "Done."}); got.Score != 0 {
		t.Errorf("bare acknowledgement scored %v", got.Score)
	}
	if got := v13Score(t, noCall, protocol.RunResponse{FinalText: "Sure thing, all set for you."}); got.Score != 0 {
		t.Errorf("ungrounded acknowledgement scored %v", got.Score)
	}
	acted := protocol.RunResponse{FinalText: "Teal it is.", ToolCalls: []protocol.ObservedToolCall{{Name: "set_accent_color", Args: rawArgs(t, map[string]any{"color": "teal"})}}}
	if got := v13Score(t, noCall, acted); got.Score != 0 {
		t.Errorf("redundant setter call scored %v", got.Score)
	}
	negation := protocol.ToolCase{ID: "neg", Category: datagen.V13RestraintCategoryPrefix + "negation_web", Restraint: &protocol.RestraintClaim{Kind: protocol.RestraintNoCall, ForbiddenTools: []string{"search_web"}}}
	if got := v13Score(t, negation, protocol.RunResponse{FinalText: "Per ounce, espresso is stronger than drip."}); got.Score != 1 {
		t.Errorf("general-knowledge answer scored %v", got.Score)
	}
	if got := v13Score(t, negation, protocol.RunResponse{FinalText: "Espresso.", ToolCalls: []protocol.ObservedToolCall{{Name: "search_web"}}}); got.Score != 0 {
		t.Errorf("negated search scored %v", got.Score)
	}
}

// TestV13RestraintProvenanceAndGroupRulePostures: the provenance rule and the
// group rule annotate under shadow, zero under enforce, and are no-ops off and
// for every earlier version.
func TestV13RestraintProvenanceAndGroupRulePostures(t *testing.T) {
	c := protocol.ToolCase{ID: "ask", Restraint: &protocol.RestraintClaim{Kind: protocol.RestraintNoCall}}
	base := protocol.CaseScore{CaseID: "ask", Kind: protocol.KindTool, Score: 1, ToolScore: 1}
	swallowed := V13RestraintEvidence{ModelSelectedNotExecuted: 1}
	if got := ApplyV13RestraintProvenance(protocol.BenchVersionV13, c, base, swallowed, V13PostureShadow); got.Score != 1 || !strings.Contains(strings.Join(got.Notes, " "), "swallowed_model_call") {
		t.Fatalf("shadow provenance: %+v", got)
	}
	if got := ApplyV13RestraintProvenance(protocol.BenchVersionV13, c, base, swallowed, V13PostureEnforce); got.Score != 0 {
		t.Fatalf("enforce provenance kept %v", got.Score)
	}
	if got := ApplyV13RestraintProvenance(protocol.BenchVersionV12, c, base, swallowed, V13PostureEnforce); got.Score != 1 || len(got.Notes) != 0 {
		t.Fatalf("v12 touched by v13 provenance: %+v", got)
	}
	absent := false
	if got := ApplyV13RestraintProvenance(protocol.BenchVersionV13, c, base, V13RestraintEvidence{CatalogPresent: &absent}, V13PostureShadow); !strings.Contains(strings.Join(got.Notes, " "), "restraint_without_offer") {
		t.Fatalf("catalog absence not annotated: %+v", got)
	}
	if got := ApplyV13RestraintProvenance(protocol.BenchVersionV13, c, base, V13RestraintEvidence{}, V13PostureEnforce); got.Score != 1 || len(got.Notes) != 0 {
		t.Fatalf("clean evidence produced a finding: %+v", got)
	}

	perCase := []protocol.CaseScore{
		{CaseID: "a1", Kind: protocol.KindTool, TwinGroup: "g1", Relation: protocol.TwinRelationDecision, Score: 1, ToolScore: 1},
		{CaseID: "a2", Kind: protocol.KindTool, TwinGroup: "g1", Relation: protocol.TwinRelationDecision, Score: 0},
		{CaseID: "b1", Kind: protocol.KindTool, TwinGroup: "g2", Relation: protocol.TwinRelationDecision, Score: 1, ToolScore: 1},
		{CaseID: "b2", Kind: protocol.KindTool, TwinGroup: "g2", Relation: protocol.TwinRelationDecision, Score: 1, ToolScore: 1},
		{CaseID: "m", Kind: protocol.KindMemory, TwinGroup: "twin-x", Score: 1, Correct: true},
	}
	shadow := ApplyV13RestraintGroupRule(protocol.BenchVersionV13, perCase, V13PostureShadow)
	if shadow[0].Score != 1 || !strings.Contains(strings.Join(shadow[0].Notes, " "), "shadow") || len(shadow[2].Notes) != 0 {
		t.Fatalf("shadow group rule: %+v", shadow)
	}
	enforce := ApplyV13RestraintGroupRule(protocol.BenchVersionV13, perCase, V13PostureEnforce)
	if enforce[0].Score != 0 || enforce[1].Score != 0 || enforce[2].Score != 1 || enforce[3].Score != 1 || enforce[4].Score != 1 {
		t.Fatalf("enforce group rule: %+v", enforce)
	}
	if perCase[0].Score != 1 {
		t.Fatal("group rule mutated its input")
	}
	if got := ApplyV13RestraintGroupRule(protocol.BenchVersionV12, perCase, V13PostureEnforce); got[0].Score != 1 {
		t.Fatal("v12 touched by the v13 group rule")
	}
	// Decision twins never enter the metamorphic-consistency factor.
	if mc := MetamorphicConsistency(perCase); mc == nil || *mc != 1 {
		t.Fatalf("metamorphic consistency counted restraint groups: %v", mc)
	}
}

// v13Policy renders a RunResponse for a restraint case under one decision
// policy. The oracle reads the grader-only fields the way a record-reading
// harness would read the seeded record; the baselines ignore the record.
func v13Policy(t *testing.T, policy string, coin *rand.Rand, c protocol.ToolCase) protocol.RunResponse {
	t.Helper()
	ask := c.Restraint != nil
	clarify := func() protocol.RunResponse {
		text := "Could you tell me which option you want?"
		if ask {
			slot, ground := "", ""
			if len(c.Restraint.Accept) > 0 {
				slot = c.Restraint.Accept[0]
			}
			if len(c.Restraint.Grounding) > 0 {
				ground = c.Restraint.Grounding[0]
			}
			text = fmt.Sprintf("I checked your notes — %s — but there is no settled value; which %s do you want, and I'll answer honestly from what I know.", ground, slot)
		}
		return protocol.RunResponse{FinalText: text}
	}
	act := func() protocol.RunResponse {
		if ask {
			// An always-act policy fires the forbidden tool with a guessed value.
			return protocol.RunResponse{FinalText: "Done.", ToolCalls: []protocol.ObservedToolCall{{Name: c.Restraint.ForbiddenTools[0], Args: rawArgs(t, map[string]any{"value": "guess"})}}}
		}
		var calls []protocol.ObservedToolCall
		for _, spec := range c.ExpectedTools {
			args := map[string]any{}
			for key, value := range spec.RequiredArgs {
				args[key] = value
			}
			calls = append(calls, protocol.ObservedToolCall{Name: spec.Name, Args: rawArgs(t, args)})
		}
		return protocol.RunResponse{FinalText: "Done.", ToolCalls: calls}
	}
	switch policy {
	case "always_ask":
		return clarify()
	case "always_act":
		return act()
	case "random":
		if coin.Intn(2) == 0 {
			return clarify()
		}
		return act()
	default: // oracle
		if ask {
			return clarify()
		}
		return act()
	}
}

// TestV13RestraintBaselinesScoreAtMostChanceEndToEnd grades generated v13
// restraint groups through the real v13 tool grader and the enforce-mode group
// rule: always-ask, always-act, and random-split earn at most chance on the
// slice, the record-reading oracle earns 1.0, and under the default shadow
// posture the per-case scores are untouched.
func TestV13RestraintBaselinesScoreAtMostChanceEndToEnd(t *testing.T) {
	prof, ok := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 full profile missing")
	}
	coin := rand.New(rand.NewSource(1846))
	totals := map[string]float64{}
	count := 0.0
	for seed := int64(1); seed <= 8; seed++ {
		artifact, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, policy := range []string{"always_ask", "always_act", "random", "oracle"} {
			var perCase []protocol.CaseScore
			for _, c := range artifact.ToolCases {
				if !datagen.IsV13Restraint(c.Category) {
					continue
				}
				resp := v13Policy(t, policy, coin, c)
				perCase = append(perCase, v13Score(t, c, resp))
			}
			shadow := ApplyV13RestraintGroupRule(protocol.BenchVersionV13, perCase, V13PostureShadow)
			enforced := ApplyV13RestraintGroupRule(protocol.BenchVersionV13, perCase, V13PostureEnforce)
			for i := range perCase {
				if shadow[i].Score != perCase[i].Score {
					t.Fatalf("shadow posture moved a score")
				}
				totals[policy] += enforced[i].Score
				if policy == "oracle" && perCase[i].Score != 1 {
					t.Fatalf("seed %d: oracle scored %v on %s (%s): %v", seed, perCase[i].Score, perCase[i].CaseID, perCase[i].Category, perCase[i].Notes)
				}
			}
			if policy == "oracle" {
				count += float64(len(perCase))
			}
		}
	}
	if count == 0 {
		t.Fatal("no restraint cases generated")
	}
	for _, policy := range []string{"always_ask", "always_act", "random"} {
		if share := totals[policy] / count; share > 1.0/3 {
			t.Errorf("%s earns %.1f%% of the restraint slice under the group rule, want <= 33%%", policy, share*100)
		}
	}
	if totals["oracle"] != count {
		t.Fatalf("oracle earned %.0f of %.0f", totals["oracle"], count)
	}
}

// TestV13PostureDefaultsToShadow pins the owner-decision safety property: an
// unset, empty, or unrecognized posture variable is SHADOW (annotate, never
// move a score); only the exact words off / enforce (any case, trimmed) select
// the other postures.
func TestV13PostureDefaultsToShadow(t *testing.T) {
	const name = "DITTOBENCH_V13_TEST_POSTURE"
	for _, tt := range []struct {
		value string
		set   bool
		want  V13Posture
	}{
		{"", false, V13PostureShadow},
		{"", true, V13PostureShadow},
		{"garbage", true, V13PostureShadow},
		{"ENFORCE!", true, V13PostureShadow},
		{"observe", true, V13PostureShadow},
		{"review", true, V13PostureShadow},
		{"shadow", true, V13PostureShadow},
		{"Enforce", true, V13PostureEnforce},
		{" enforce ", true, V13PostureEnforce},
		{"OFF", true, V13PostureOff},
	} {
		if tt.set {
			t.Setenv(name, tt.value)
		} else {
			os.Unsetenv(name)
		}
		if got := V13PostureFromEnv(name); got != tt.want {
			t.Errorf("posture(%q set=%v) = %q, want %q", tt.value, tt.set, got, tt.want)
		}
	}
}

// TestV13FollowUpReadRejectsStaleHedge: a follow-up read carries the
// pre-mutation value as EffectForbidden; an answer that lists both the stale
// and the corrected state scores 0, the corrected state alone scores 1, and a
// case without EffectForbidden (an ordinary planted read) is unaffected.
func TestV13FollowUpReadRejectsStaleHedge(t *testing.T) {
	c := protocol.ToolCase{
		ID: "fu", Category: datagen.V13MutationFollowUpCategory,
		ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}}, MaxToolCalls: 2,
		EffectAnswer: "Monday", EffectForbidden: []string{"Friday"}, RunAfterCaseID: "upd",
	}
	for _, tt := range []struct {
		name string
		resp protocol.RunResponse
		want float64
	}{
		{"corrected only", protocol.RunResponse{FinalText: "The handoff is on Monday now."}, 1},
		{"corrected in slot", protocol.RunResponse{Answer: "Monday"}, 1},
		{"hedge old and new", protocol.RunResponse{FinalText: "It was Friday, now maybe Monday."}, 0},
		{"stale only", protocol.RunResponse{FinalText: "Friday."}, 0},
		{"hedge across fields", protocol.RunResponse{Answer: "Monday", FinalText: "Your notes say Friday and Monday."}, 0},
		{"weekday inflection is bounded", protocol.RunResponse{FinalText: "Monday — the Fridays note is gone."}, 1},
	} {
		if got := v13Score(t, c, tt.resp); got.Score != tt.want {
			t.Errorf("%s: scored %v, want %v (%v)", tt.name, got.Score, tt.want, got.Notes)
		}
	}
	del := protocol.ToolCase{
		ID: "fud", Category: datagen.V13MutationFollowUpCategory,
		ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}}, MaxToolCalls: 2,
		EffectAnswer: "kes.arden@northgate.example", EffectForbidden: []string{"kes@oldmail.example"},
	}
	if got := v13Score(t, del, protocol.RunResponse{FinalText: "Kes is at kes.arden@northgate.example (previously kes@oldmail.example)."}); got.Score != 0 {
		t.Errorf("stale address listed beside the current one scored %v", got.Score)
	}
	if got := v13Score(t, del, protocol.RunResponse{FinalText: "kes.arden@northgate.example"}); got.Score != 1 {
		t.Errorf("current address alone scored %v: %v", got.Score, got.Notes)
	}
	plain := c
	plain.EffectForbidden = nil
	if got := v13Score(t, plain, protocol.RunResponse{FinalText: "Friday, now Monday."}); got.Score != 1 {
		t.Errorf("ordinary planted read applied a forbid it does not carry: %v", got.Score)
	}
}

// TestV13ShadowPostureCeilingsPerCase documents what the DEFAULT shadow
// posture leaves on the table, so the "<= chance" property is understood as
// enforce-only: per case, an always-ask policy keeps every ask member it can
// ground and an always-act policy keeps every act member, and both lose the
// other half of every group. Under enforce the group rule takes the rest.
func TestV13ShadowPostureCeilingsPerCase(t *testing.T) {
	prof, ok := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 full profile missing")
	}
	coin := rand.New(rand.NewSource(1846))
	asks, acts := 0.0, 0.0
	askKept := map[string]float64{}
	actKept := map[string]float64{}
	for seed := int64(1); seed <= 4; seed++ {
		artifact, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range artifact.ToolCases {
			if !datagen.IsV13Restraint(c.Category) {
				continue
			}
			if c.Restraint != nil {
				asks++
			} else {
				acts++
			}
			for _, policy := range []string{"always_ask", "always_act"} {
				score := v13Score(t, c, v13Policy(t, policy, coin, c)).Score
				if c.Restraint != nil {
					askKept[policy] += score
				} else {
					actKept[policy] += score
				}
			}
		}
	}
	if asks == 0 || acts == 0 {
		t.Fatal("no restraint members")
	}
	// The oracle-shaped clarification in v13Policy cites the record's grounding
	// token; a real no-read always-ask cannot, so this is the policy's CEILING.
	if askKept["always_ask"] != asks || actKept["always_ask"] != 0 {
		t.Fatalf("always-ask per-case: ask half %.0f/%.0f, act half %.0f/%.0f — expected the whole ask half and none of the act half", askKept["always_ask"], asks, actKept["always_ask"], acts)
	}
	if actKept["always_act"] != acts || askKept["always_act"] != 0 {
		t.Fatalf("always-act per-case: act half %.0f/%.0f, ask half %.0f/%.0f — expected the whole act half and none of the ask half", actKept["always_act"], acts, askKept["always_act"], asks)
	}
}
