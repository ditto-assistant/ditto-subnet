package datagen

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math/rand"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func v13FullCases(t *testing.T, seed int64) []protocol.ToolCase {
	t.Helper()
	return v13CasesForSize(t, seed, V9FullToolCaseCount)
}

func v13CasesForSize(t *testing.T, seed int64, n int) []protocol.ToolCase {
	t.Helper()
	rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, n, protocol.BenchVersionV13)
	return cases
}

func pairText(tc protocol.ToolCase) string {
	var sb strings.Builder
	for _, pair := range tc.PrerequisitePairs {
		sb.WriteString(strings.ToLower(pair.Prompt))
		sb.WriteString(" ")
		sb.WriteString(strings.ToLower(pair.Response))
		sb.WriteString(" ")
	}
	return sb.String()
}

// TestV13RestraintGroupsAreDistributionallyMatched pins the #1846 shape: a
// full run carries exactly sixteen restraint cases in decision_twin groups of
// two or three; every group is one family with at least one ask and one act
// member, a per-seed 2:1 or 1:2 triplet cardinality, and no two members share
// a surface. The four request-keyed no-tool families are gone, ask members
// carry a slot lexicon and record-grounded tokens that exist in their planted
// record, and each family is present in roughly two thirds of runs.
func TestV13RestraintGroupsAreDistributionallyMatched(t *testing.T) {
	const seeds = 40
	familySeeds := map[string]int{}
	askHeavy, actHeavy := 0, 0
	for seed := int64(1); seed <= seeds; seed++ {
		cases := v13FullCases(t, seed)
		groups := map[string][]protocol.ToolCase{}
		restraint := 0
		for _, tc := range cases {
			if v13LegacyNoToolFamily(tc.Category) {
				t.Fatalf("seed %d: legacy no-tool family %q survived v13", seed, tc.Category)
			}
			if len(tc.ExpectedTools) == 0 && tc.Restraint == nil {
				t.Fatalf("seed %d: no-expected-tool case %s without a restraint claim", seed, tc.ID)
			}
			if !IsV13Restraint(tc.Category) {
				continue
			}
			restraint++
			if tc.TwinGroup == "" || tc.TwinRelation != protocol.TwinRelationDecision {
				t.Fatalf("seed %d: restraint case %s is not registered as a decision twin", seed, tc.ID)
			}
			groups[tc.TwinGroup] = append(groups[tc.TwinGroup], tc)
		}
		if restraint != V13FullRestraintCaseCount {
			t.Fatalf("seed %d: %d restraint cases, want %d", seed, restraint, V13FullRestraintCaseCount)
		}
		seenFamily := map[string]bool{}
		for id, members := range groups {
			if len(members) != 2 && len(members) != 3 {
				t.Fatalf("seed %d group %s: %d members", seed, id, len(members))
			}
			family := members[0].Category
			seenFamily[family] = true
			asks, surfaces := 0, map[string]bool{}
			for _, m := range members {
				if m.Category != family {
					t.Fatalf("seed %d group %s mixes families %s and %s", seed, id, family, m.Category)
				}
				if surfaces[m.Prompt] {
					t.Fatalf("seed %d group %s: two members share the surface %q", seed, id, m.Prompt)
				}
				surfaces[m.Prompt] = true
				if m.Restraint != nil {
					asks++
					if len(m.ExpectedTools) != 0 || m.MaxToolCalls != 0 {
						t.Fatalf("seed %d: ask member %s expects tools", seed, m.ID)
					}
					if m.Restraint.Kind == protocol.RestraintClarifyFirst {
						if len(m.Restraint.Accept) == 0 || len(m.Restraint.Grounding) == 0 {
							t.Fatalf("seed %d: clarify member %s lacks a slot lexicon or grounding", seed, m.ID)
						}
						text := pairText(m)
						for _, token := range m.Restraint.Grounding {
							if !strings.Contains(text, strings.ToLower(token)) {
								t.Fatalf("seed %d: grounding token %q of %s is not in its planted record", seed, token, m.ID)
							}
						}
					}
					if len(m.Restraint.ForbiddenTools) == 0 {
						t.Fatalf("seed %d: ask member %s forbids no tool", seed, m.ID)
					}
				} else {
					if len(m.ExpectedTools) == 0 {
						t.Fatalf("seed %d: act member %s expects no tool", seed, m.ID)
					}
					for _, spec := range m.ExpectedTools {
						for _, value := range spec.RequiredArgs {
							if strings.Contains(strings.ToLower(m.Prompt), strings.ToLower(value)) && family != V13RestraintCategoryPrefix+string(v13FamilyDeclarative) {
								t.Fatalf("seed %d: act member %s exposes its record-bound value %q in the prompt", seed, m.ID, value)
							}
						}
					}
				}
			}
			if asks == 0 || asks == len(members) {
				t.Fatalf("seed %d group %s: %d asks of %d members — not a matched group", seed, id, asks, len(members))
			}
			if len(members) == 3 {
				if asks == 2 {
					askHeavy++
				} else {
					actHeavy++
				}
			}
		}
		for family := range seenFamily {
			familySeeds[family]++
		}
	}
	if askHeavy == 0 || actHeavy == 0 {
		t.Fatalf("triplet cardinality never varied: ask-heavy=%d act-heavy=%d", askHeavy, actHeavy)
	}
	for _, family := range v13RestraintFamilies {
		share := float64(familySeeds[V13RestraintCategoryPrefix+string(family)]) / seeds
		if share < 0.4 || share > 0.9 {
			t.Errorf("family %s present in %.0f%% of runs, want roughly 60%%", family, share*100)
		}
	}
}

// TestV13RestraintPolicyBaselinesScoreAtMostChance: under the decision_twin
// group rule (every member must be right), an always-ask, always-act, or
// random-split policy scores at most chance on the restraint slice while the
// record-reading oracle scores 1.0. This is the generator-side half; the
// scorer pins the same property over graded RunResponses.
func TestV13RestraintPolicyBaselinesScoreAtMostChance(t *testing.T) {
	const seeds = 40
	coin := rand.New(rand.NewSource(1846))
	total := 0.0
	sums := map[string]float64{}
	for seed := int64(1); seed <= seeds; seed++ {
		cases := v13FullCases(t, seed)
		groups := map[string][]protocol.ToolCase{}
		for _, tc := range cases {
			if IsV13Restraint(tc.Category) {
				groups[tc.TwinGroup] = append(groups[tc.TwinGroup], tc)
			}
		}
		for _, members := range groups {
			total += float64(len(members))
			right := map[string]int{"always_ask": 0, "always_act": 0, "random": 0, "oracle": 0}
			for _, m := range members {
				ask := m.Restraint != nil
				if ask {
					right["always_ask"]++
				} else {
					right["always_act"]++
				}
				if coin.Intn(2) == 0 == ask {
					right["random"]++
				}
				right["oracle"]++
			}
			for policy, n := range right {
				if n == len(members) {
					sums[policy] += float64(len(members))
				}
			}
		}
	}
	for _, policy := range []string{"always_ask", "always_act", "random"} {
		if share := sums[policy] / total; share > 1.0/3 {
			t.Errorf("%s scores %.1f%% of the restraint slice under the group rule, want <= 33%%", policy, share*100)
		}
	}
	if sums["oracle"] != total {
		t.Fatalf("oracle scored %.0f of %.0f", sums["oracle"], total)
	}
}

// TestV13MemoryEffectReadsCappedAndPlanted pins the #1845 memory-read shape:
// at most eight memory-routing tool cases per full run, every one carrying an
// effect answer that is planted in a prerequisite record (or, for a follow-up
// read, produced by an earlier mutation) and never exposed in the prompt.
func TestV13MemoryEffectReadsCappedAndPlanted(t *testing.T) {
	followUps := 0
	for seed := int64(1); seed <= 200; seed++ {
		cases := v13FullCases(t, seed)
		byID := map[string]protocol.ToolCase{}
		for _, tc := range cases {
			byID[tc.ID] = tc
		}
		routing := 0
		for _, tc := range cases {
			if !v13MemoryRoutingCase(tc) {
				if tc.EffectAnswer != "" {
					t.Fatalf("seed %d: non-memory case %s carries an effect answer", seed, tc.ID)
				}
				continue
			}
			routing++
			if tc.EffectAnswer == "" {
				t.Fatalf("seed %d: memory-read case %s (%s) has no effect answer", seed, tc.ID, tc.Category)
			}
			if strings.Contains(strings.ToLower(tc.Prompt), strings.ToLower(tc.EffectAnswer)) {
				t.Fatalf("seed %d: prompt of %s exposes the planted value %q", seed, tc.ID, tc.EffectAnswer)
			}
			if tc.RunAfterCaseID != "" {
				followUps++
				mutation, ok := byID[tc.RunAfterCaseID]
				if !ok || (mutation.Category != "world_memory_update" && mutation.Category != "world_memory_delete") {
					t.Fatalf("seed %d: follow-up %s points at %q, not a mutation", seed, tc.ID, tc.RunAfterCaseID)
				}
				if tc.Category != V13MutationFollowUpCategory {
					t.Fatalf("seed %d: follow-up %s has category %s", seed, tc.ID, tc.Category)
				}
				continue
			}
			if !strings.Contains(pairText(tc), strings.ToLower(tc.EffectAnswer)) {
				t.Fatalf("seed %d: planted value %q of %s is not in its prerequisite records", seed, tc.EffectAnswer, tc.ID)
			}
		}
		if routing > V13MemoryEffectReadCap {
			t.Fatalf("seed %d: %d memory-read cases, cap is %d", seed, routing, V13MemoryEffectReadCap)
		}
		if routing == 0 {
			t.Fatalf("seed %d: no memory-read cases at all", seed)
		}
	}
	if followUps == 0 {
		t.Fatal("no mutation follow-up reads were generated across 200 seeds")
	}
}

// TestV13MutationGrammarIsCueUnreliable: a verb-to-tool cue table (the
// pre-v13 shortcut) predicts the correct mutation tool on fewer than half of
// the v13 mutation cases, every update accepts delete + save as an
// end-state-equivalent alternative, and the delete claim forbids the person's
// canonical pairs.
func TestV13MutationGrammarIsCueUnreliable(t *testing.T) {
	cueTable := []struct {
		cues []string
		tool string
	}{
		{[]string{"scratch", "bin ", "forget", "drop ", "clear ", "can go", "clutter", "stale", "delete", "remove"}, "delete_memory"},
		{[]string{"remember", "note ", "save", "keep in mind"}, "save_memory"},
		{[]string{"update", "correct", "fix ", "change", "moved", "shifted", "make it"}, "update_memory"},
	}
	predict := func(prompt string) string {
		prompt = strings.ToLower(prompt)
		best, bestAt := "", len(prompt)+1
		for _, row := range cueTable {
			for _, cue := range row.cues {
				if at := strings.Index(prompt, cue); at >= 0 && at < bestAt {
					best, bestAt = row.tool, at
				}
			}
		}
		return best
	}
	mutations, hits := 0, 0
	for seed := int64(1); seed <= 40; seed++ {
		world := universe.Generate(seed, 3)
		byNote := map[string]universe.Person{}
		for _, p := range world.People {
			byNote[p.ToolNotePairID] = p
		}
		for _, tc := range v13FullCases(t, seed) {
			switch tc.Category {
			case "world_memory_update":
				mutations++
				if predict(tc.Prompt) == "update_memory" {
					hits++
				}
				if len(tc.AlternativeExpectedTools) != 1 || tc.AlternativeExpectedTools[0][0].Name != "delete_memory" || tc.AlternativeExpectedTools[0][1].Name != "save_memory" {
					t.Fatalf("seed %d: update %s lacks the delete+save alternative", seed, tc.ID)
				}
				claim, ok := tc.ExpectedTools[0].RequiredArgClaims["content"]
				if !ok || !strings.Contains(claim.Expected, "handoff is ") || strings.Contains(claim.Expected, "Friday") {
					t.Fatalf("seed %d: update %s content claim %+v", seed, tc.ID, claim)
				}
			case "world_memory_delete":
				mutations++
				if predict(tc.Prompt) == "delete_memory" {
					hits++
				}
				claim := tc.ExpectedTools[0].RequiredArgClaims["pair_id"]
				person, ok := byNote[claim.Expected]
				if !ok {
					t.Fatalf("seed %d: delete %s claim does not name a person's disposable note", seed, tc.ID)
				}
				if len(claim.Forbidden) != 4 || claim.Forbidden[2] != person.EmailPairID {
					t.Fatalf("seed %d: delete %s does not forbid the canonical pairs: %v", seed, tc.ID, claim.Forbidden)
				}
			}
		}
	}
	if mutations == 0 {
		t.Fatal("no mutation cases")
	}
	if share := float64(hits) / float64(mutations); share >= 0.5 {
		t.Fatalf("verb cue table predicts %.0f%% of %d mutation tools, want < 50%%", share*100, mutations)
	}
}

// TestV13StateDependentRoutingCoversCalendarAndEmail: the v13 route space
// includes calendar move-vs-create and email reply-vs-new, decided by the
// planted record, with the prompt never revealing the state; the family's slot
// share is unchanged from v12.
func TestV13StateDependentRoutingCoversCalendarAndEmail(t *testing.T) {
	outcomes := map[string]int{}
	v12Routes, v13Routes := 0, 0
	for seed := int64(1); seed <= 20; seed++ {
		rotated, _ := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV12)
		v12, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, 100, protocol.BenchVersionV12)
		for _, tc := range v12 {
			if tc.Category == "v10_state_dependent_routing" {
				v12Routes++
			}
		}
		for _, tc := range v13FullCases(t, seed) {
			switch tc.Category {
			case "v10_state_dependent_routing":
				v13Routes++
			case V13StateDependentCalendarCategory:
				v13Routes++
				lower := strings.ToLower(tc.Prompt)
				if strings.Contains(lower, "already") || strings.Contains(lower, "nothing") || strings.Contains(lower, "double") {
					t.Fatalf("seed %d: calendar prompt reveals the state: %q", seed, tc.Prompt)
				}
				if tc.ExpectedTools[0].Name == "calendar_search_events" {
					outcomes["calendar_move"]++
					if len(tc.ForbiddenTools) != 1 || tc.ForbiddenTools[0] != "calendar_create_event" {
						t.Fatalf("seed %d: move case %s does not forbid calendar_create_event", seed, tc.ID)
					}
				} else {
					outcomes["calendar_create"]++
				}
			case V13StateDependentEmailCategory:
				v13Routes++
				outcomes["email"]++
				if tc.ExpectedTools[0].RequiredArgClaims["to"].Kind != "email" {
					t.Fatalf("seed %d: email route %s lacks an email claim", seed, tc.ID)
				}
				if strings.Contains(strings.ToLower(tc.Prompt), "@") {
					t.Fatalf("seed %d: email prompt exposes the recipient: %q", seed, tc.Prompt)
				}
			}
		}
	}
	for _, key := range []string{"calendar_move", "calendar_create", "email"} {
		if outcomes[key] == 0 {
			t.Errorf("v13 never exercised the %s route across 20 seeds", key)
		}
	}
	// Routing weight unchanged in expectation: the route slots are the
	// world_agent_job_dispatch cases the v9 world pass carves per seed, and
	// that pass re-draws under the v13 40-case world envelope, so per-seed
	// counts differ (measured 7..19 either way) while the 20-seed aggregate
	// stays within a few percent (measured 263 -> 285). The bound is +-15%,
	// tighter than the family's own per-seed spread.
	if v13Routes < v12Routes*85/100 || v13Routes > v12Routes*115/100 {
		t.Fatalf("state-dependent routing weight moved: v12=%d v13=%d over 20 seeds (want within 15%%)", v12Routes, v13Routes)
	}
}

// TestV13BusinessWorkflowClaimsForbidOnlyTheDistractor pins the #1847 rule:
// the workflow-name claim accepts the project's formal name or alias and
// forbids the OTHER project's client, never the correct client or name.
func TestV13BusinessWorkflowClaimsForbidOnlyTheDistractor(t *testing.T) {
	seen := 0
	for seed := int64(1); seed <= 20; seed++ {
		world := universe.Generate(seed, 3)
		for _, tc := range v13FullCases(t, seed) {
			if tc.Category != "world_business_workflow" {
				continue
			}
			for _, spec := range tc.ExpectedTools {
				if spec.Name != "create_workflow" {
					continue
				}
				seen++
				name := spec.RequiredArgClaims["name"]
				var project *universe.Project
				for i := range world.Projects {
					if world.Projects[i].Name == name.Expected {
						project = &world.Projects[i]
					}
				}
				if project == nil {
					t.Fatalf("seed %d: name claim %q is not a project", seed, name.Expected)
				}
				if len(name.Accept) != 1 || name.Accept[0] != project.Alias {
					t.Fatalf("seed %d: name claim does not accept the alias: %+v", seed, name)
				}
				if len(name.Forbidden) != 1 {
					t.Fatalf("seed %d: name claim forbids %d entities, want the one distractor", seed, len(name.Forbidden))
				}
				if name.Forbidden[0] == project.Client || name.Forbidden[0] == project.Name || name.Forbidden[0] == project.Alias {
					t.Fatalf("seed %d: name claim forbids the correct party %q", seed, name.Forbidden[0])
				}
				steps := spec.RequiredArgClaims["steps"]
				if steps.Kind != "set" || !strings.Contains(steps.Expected, "@") {
					t.Fatalf("seed %d: steps claim %+v", seed, steps)
				}
			}
		}
	}
	if seen == 0 {
		t.Fatal("no business workflow cases")
	}
}

// TestV12ToolCasesUnchangedByV13 pins the v12 tool half byte-for-byte so a
// v13 lever that leaks below its gate fails here as well as in the gen
// known-vector suite.
func TestV12ToolCasesUnchangedByV13(t *testing.T) {
	const want = "526e9ab7d33a3637004a033eccdc32d3fc3efcf3aa90c0b5dbe57d6099cd60e5"
	ds, err := GenerateForVersion(7, 100, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(ds)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	if got := hex.EncodeToString(sum[:]); got != want {
		t.Fatalf("v12 tool dataset moved:\n got %s\nwant %s", got, want)
	}
	for _, tc := range ds.ToolCases {
		if tc.Restraint != nil || tc.EffectAnswer != "" || tc.TwinGroup != "" || len(tc.AlternativeExpectedTools) != 0 || len(tc.ForbiddenTools) != 0 {
			t.Fatalf("v12 case %s carries v13 grader-only state", tc.ID)
		}
		for _, spec := range tc.ExpectedTools {
			if len(spec.RequiredArgClaims) != 0 {
				t.Fatalf("v12 case %s carries argument claims", tc.ID)
			}
		}
	}
}

// TestV13RestraintCountMatchesRunSize is the generic invariant behind the
// full/medium/small profiles: the generated restraint-case count equals
// v13RestraintCaseCount(n) for every run size, the groups have the published
// shapes (16 = 3+3+3+3+2+2, 8 = 3+3+2, 0 = none), and every retained
// memory-routing case carries a planted value at every size.
func TestV13RestraintCountMatchesRunSize(t *testing.T) {
	for _, n := range []int{6, 48, 100} {
		for seed := int64(1); seed <= 12; seed++ {
			cases := v13CasesForSize(t, seed, n)
			if len(cases) != n {
				t.Fatalf("n=%d seed %d: generated %d cases", n, seed, len(cases))
			}
			groups := map[string]int{}
			restraint, routing := 0, 0
			for _, tc := range cases {
				if v13LegacyNoToolFamily(tc.Category) {
					t.Fatalf("n=%d seed %d: legacy no-tool family %q survived v13", n, seed, tc.Category)
				}
				if IsV13Restraint(tc.Category) {
					restraint++
					groups[tc.TwinGroup]++
				}
				if v13MemoryRoutingCase(tc) {
					routing++
					if tc.EffectAnswer == "" {
						t.Fatalf("n=%d seed %d: memory-read case %s (%s) has no effect answer", n, seed, tc.ID, tc.Category)
					}
				}
			}
			if want := v13RestraintCaseCount(n); restraint != want {
				t.Fatalf("n=%d seed %d: %d restraint cases, want %d", n, seed, restraint, want)
			}
			if routing > V13MemoryEffectReadCap {
				t.Fatalf("n=%d seed %d: %d memory-read cases, cap is %d", n, seed, routing, V13MemoryEffectReadCap)
			}
			sizes := []int{}
			for _, size := range groups {
				sizes = append(sizes, size)
			}
			sort.Sort(sort.Reverse(sort.IntSlice(sizes)))
			want := v13GroupShapes(v13RestraintCaseCount(n))
			if len(sizes) != len(want) {
				t.Fatalf("n=%d seed %d: group shapes %v, want %v", n, seed, sizes, want)
			}
			for i := range want {
				if sizes[i] != want[i] {
					t.Fatalf("n=%d seed %d: group shapes %v, want %v", n, seed, sizes, want)
				}
			}
		}
	}
}

// TestV13MediumRestraintGroups pins the medium profile (n=48): exactly eight
// restraint cases in two triplets and one pair, each group one family with at
// least one ask and one act member, no world-derived, result-usage, or
// state-dependent case consumed as a slot, and the world carrier untouched.
func TestV13MediumRestraintGroups(t *testing.T) {
	for seed := int64(1); seed <= 12; seed++ {
		cases := v13CasesForSize(t, seed, 48)
		before := v13CasesBeforeRestraint(t, seed, 48)
		groups := map[string][]protocol.ToolCase{}
		restraint := 0
		for i, tc := range cases {
			if !IsV13Restraint(tc.Category) {
				continue
			}
			restraint++
			groups[tc.TwinGroup] = append(groups[tc.TwinGroup], tc)
			source := before[i]
			if v13WorldDerived(source.Category) || IsResultUsage(source.Category) || len(source.PrerequisitePairs) != 0 {
				t.Fatalf("seed %d: medium restraint slot %d consumed a protected %s case", seed, i, source.Category)
			}
		}
		if restraint != V13MediumRestraintCaseCount {
			t.Fatalf("seed %d: %d medium restraint cases, want %d", seed, restraint, V13MediumRestraintCaseCount)
		}
		if len(groups) != 3 {
			t.Fatalf("seed %d: %d groups, want 3 (3+3+2)", seed, len(groups))
		}
		for id, members := range groups {
			asks := 0
			for _, m := range members {
				if m.Category != members[0].Category {
					t.Fatalf("seed %d group %s mixes families", seed, id)
				}
				if m.Restraint != nil {
					asks++
				}
			}
			if asks == 0 || asks == len(members) {
				t.Fatalf("seed %d group %s: %d asks of %d — not matched", seed, id, asks, len(members))
			}
		}
		worldDerived := 0
		for _, tc := range cases {
			if v13WorldDerived(tc.Category) {
				worldDerived++
			}
		}
		if worldDerived == 0 {
			t.Fatalf("seed %d: medium run lost every world-derived case", seed)
		}
	}
}

// v13CasesBeforeRestraint regenerates a v13 run and returns the cases as they
// stood before applyV13ToolSemantics, so a test can see which source case each
// restraint slot replaced. It re-runs the v9/v10 passes exactly as
// GenerateCasesWithFillersForVersion does.
func v13CasesBeforeRestraint(t *testing.T, seed int64, n int) []protocol.ToolCase {
	t.Helper()
	rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	after := v13CasesForSize(t, seed, n)
	// Every pass before v13 is deterministic in (seed, n, version); the v13
	// pass only REPLACES cases in place, so the pre-v13 category of a slot is
	// recoverable by generating with the v13 pass disabled.
	cases, _ := generateCasesWithFillersForVersionSkippingV13(rand.New(rand.NewSource(rotated)), seed, n)
	if len(cases) != len(after) {
		t.Fatalf("pre-v13 regeneration produced %d cases, want %d", len(cases), len(after))
	}
	for i := range cases {
		if cases[i].ID != after[i].ID {
			t.Fatalf("pre-v13 regeneration misaligned at %d: %s vs %s", i, cases[i].ID, after[i].ID)
		}
	}
	return cases
}

// TestV13GroundingIsRecordOnly pins the S1/S2 defence of the clarify families:
// every Grounding token exists only in the member's planted record — never in
// the member's own prompt, and never in the slot lexicon (Accept) — so a
// clarifying template keyed on the request or the tool name cannot cite one
// without reading the record. The declarative family grounds a no-call
// acknowledgement on the stored value by contract (PROTOCOL.md: "cites the
// stored value"); its read is discriminated by the act sibling, so it is the
// documented exception to the prompt rule, not to the Accept rule.
func TestV13GroundingIsRecordOnly(t *testing.T) {
	seen := map[string]int{}
	for seed := int64(1); seed <= 40; seed++ {
		for _, tc := range v13FullCases(t, seed) {
			if !IsV13Restraint(tc.Category) || tc.Restraint == nil || len(tc.Restraint.Grounding) == 0 {
				continue
			}
			seen[tc.Category]++
			accept := map[string]bool{}
			for _, token := range tc.Restraint.Accept {
				accept[strings.ToLower(token)] = true
			}
			prompt := strings.ToLower(tc.Prompt)
			declarative := tc.Category == V13RestraintCategoryPrefix+string(v13FamilyDeclarative)
			for _, token := range tc.Restraint.Grounding {
				lower := strings.ToLower(token)
				if accept[lower] {
					t.Fatalf("seed %d: %s grounding token %q is also in the slot lexicon", seed, tc.Category, token)
				}
				if !declarative && strings.Contains(prompt, lower) {
					t.Fatalf("seed %d: %s grounding token %q appears in the member's own prompt %q", seed, tc.Category, token, tc.Prompt)
				}
				if tc.Restraint.Kind == protocol.RestraintClarifyFirst && !strings.Contains(pairText(tc), lower) {
					t.Fatalf("seed %d: %s grounding token %q is not in the planted record", seed, tc.Category, token)
				}
			}
		}
	}
	for _, family := range []v13RestraintFamily{v13FamilyEffort, v13FamilyCalendar, v13FamilyEmail} {
		if seen[V13RestraintCategoryPrefix+string(family)] == 0 {
			t.Fatalf("no grounded ask member of %s across 40 seeds", family)
		}
	}
}

func generateCasesWithFillersForVersionSkippingV13(r *rand.Rand, seed int64, n int) ([]protocol.ToolCase, []string) {
	v13SkipToolSemantics = true
	defer func() { v13SkipToolSemantics = false }()
	return GenerateCasesWithFillersForVersion(r, seed, n, protocol.BenchVersionV13)
}
