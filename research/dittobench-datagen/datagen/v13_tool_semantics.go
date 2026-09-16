package datagen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 tool-bench semantics (issues #1845, #1846, #1847). Every lever here
// is gated on bench_version >= 13 by the single call site in
// GenerateCasesWithFillersForVersion, so v2..v12 tool bytes are untouched:
//
//   - Restraint groups (#1846) replace the four request-keyed no-tool families
//     (no_tool, abstention, arg_hallucination, negation_no_tool) with
//     distributionally matched groups: same family and oracle, a different
//     grammar draw per member, and a per-seed 2 ask : 1 act or 1 ask : 2 act
//     cardinality. The ask member's correct move is restraint (a clarifying
//     question naming the slot and citing a record token, or a grounded no-call
//     answer); the act member's correct move is the grounded action, with the
//     value read from a seeded record. An always-ask, always-act, or
//     random-split rule is wrong on at least one member of every group.
//   - Memory-effect reads (#1845): memory-read tool cases are capped at eight
//     and graded on EFFECT. The needle is planted in the seeded world through
//     the ordinary /seed boundary, memory tools stay harness-internal and are
//     never served, and the answer must carry the needle.
//   - Cue-unreliable mutations (#1845): the world memory update/delete prompts
//     carry delete/save/update verbs that do not predict the correct tool, the
//     content argument is a paraphrase-accepting claim (#1847), delete + save is
//     an accepted alternative outcome for update, and a follow-up read placed
//     after the mutation verifies the end state.
//   - Argument claims (#1847): world_business_workflow and world_memory_update
//     emit ToolSpec.RequiredArgClaims beside RequiredArgs, so the v13 grader can
//     accept an honest paraphrase and forbid only the distractor party.

// v13RestraintFamily names one restraint family. A family is the pair
// (surface grammar, oracle) shared by every member of a group; which member is
// the ask (restraint) half and which is the act half is decided by the seeded
// record, never by the request surface.
type v13RestraintFamily string

const (
	v13FamilyEffort      v13RestraintFamily = "effort"
	v13FamilyCalendar    v13RestraintFamily = "calendar"
	v13FamilyEmail       v13RestraintFamily = "email"
	v13FamilyNegation    v13RestraintFamily = "negation_web"
	v13FamilyAbstention  v13RestraintFamily = "abstention_web"
	v13FamilyDeclarative v13RestraintFamily = "declarative_preference"
)

var v13RestraintFamilies = []v13RestraintFamily{
	v13FamilyEffort, v13FamilyCalendar, v13FamilyEmail,
	v13FamilyNegation, v13FamilyAbstention, v13FamilyDeclarative,
}

const (
	// V13RestraintCategoryPrefix prefixes every restraint-group case category.
	V13RestraintCategoryPrefix = "v13_restraint_"
	// V13MutationFollowUpCategory is the follow-up read that verifies a
	// mutation's end state.
	V13MutationFollowUpCategory = "v13_mutation_follow_up_read"
	// V13StateDependentCalendarCategory / V13StateDependentEmailCategory are the
	// v13 extensions of the v10 state-dependent routing family.
	V13StateDependentCalendarCategory = "v13_state_dependent_calendar"
	V13StateDependentEmailCategory    = "v13_state_dependent_email"

	// V13FullRestraintCaseCount is the number of restraint-group cases in a
	// full run: four triplets and two pairs.
	V13FullRestraintCaseCount = 16
	// V13MediumRestraintCaseCount is the medium-profile count: two triplets and
	// one pair.
	V13MediumRestraintCaseCount = 8
	// V13MemoryEffectReadCap caps the effect-graded memory-read tool cases per
	// run so memory competence is not double-counted against the memory half.
	V13MemoryEffectReadCap = 8
	// V13TwinMinGap is the minimum run-order distance between two members of
	// one restraint group in the harness projection; members are never
	// adjacent.
	V13TwinMinGap = 20
	// V13FollowUpMinGap is the minimum run-order distance between a mutation
	// and its follow-up read.
	V13FollowUpMinGap = 40

	// V13FullWorldActionTarget / V13FullWorldActionMinimum are the v13 world
	// envelope. Sixteen restraint cases cannot coexist with v9's 48-case world
	// target inside the one-per-family floor, so v13 lowers the world target
	// explicitly; restraint groups are themselves evidence-bound (the decision
	// depends on a seeded record), so the composed/evidence-bound half of the
	// run does not shrink.
	V13FullWorldActionTarget  = 40
	V13FullWorldActionMinimum = 34
)

// v13LegacyNoToolFamily reports whether a category is one of the request-keyed
// no-tool families v13 retires in favor of restraint groups.
func v13LegacyNoToolFamily(name string) bool {
	switch name {
	case "no_tool", "abstention", "arg_hallucination", "negation_no_tool":
		return true
	}
	return false
}

// IsV13Restraint reports whether a category is a v13 restraint-group case.
func IsV13Restraint(category string) bool {
	return strings.HasPrefix(category, V13RestraintCategoryPrefix)
}

// v13ToolRNG returns a seeded RNG for one v13 tool-bench domain. It hashes
// outside the shared toolMixRNG stream so the v9 family histogram and the v10
// route stream are untouched.
func v13ToolRNG(seed int64, n int, domain string) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-tool:%d:%d:%s", seed, n, domain)
	return rand.New(rand.NewSource(int64(h.Sum64() & ((1 << 63) - 1))))
}

// v13Pick is a seeded per-slot chooser hashed outside every shared stream.
func v13Pick(seed int64, salt string, bank []string) string {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-pick:%d:%s", seed, salt)
	return bank[h.Sum64()%uint64(len(bank))]
}

// v13WorldEnvelope returns the full-run world-action target and minimum for
// a contract: v13 lowers both explicitly (see V13FullWorldActionTarget).
func v13WorldEnvelope(benchVersion int) (target, minimum int) {
	if benchVersion >= protocol.BenchVersionV13 {
		return V13FullWorldActionTarget, V13FullWorldActionMinimum
	}
	return V9FullWorldActionTarget, V9FullWorldActionMinimum
}

// v13WorldMinimumForRun scales the v13 world minimum to a run of n tool cases
// (34 at full, 16 at medium), so the restraint pass never measures a medium
// run against the full-run floor.
func v13WorldMinimumForRun(n int) int {
	_, minimum := v13WorldEnvelope(protocol.BenchVersionV13)
	return minimum * n / V9FullToolCaseCount
}

// v13RestraintCaseCount is the restraint-group case count for a run size.
// Small stays a cheap smoke path with no groups.
func v13RestraintCaseCount(n int) int {
	switch {
	case n >= 80:
		return V13FullRestraintCaseCount
	case n >= 30:
		return V13MediumRestraintCaseCount
	default:
		return 0
	}
}

// applyV13ToolSemantics is the single v13 entry point, run after the v9 world
// and v10 route passes and before writing noise so every planted record and
// prompt receives the same projection as the rest of the run.
func applyV13ToolSemantics(seed int64, benchVersion int, cases []protocol.ToolCase) {
	if benchVersion < protocol.BenchVersionV13 || len(cases) == 0 {
		return
	}
	scale := 1
	if len(cases) >= 80 {
		scale = 3
	} else if len(cases) >= 30 {
		scale = 2
	}
	world := universe.Generate(seed, scale)
	worldCarrier := v13WorldCarrier(world, cases)
	applyV13RestraintGroups(seed, world, worldCarrier, cases)
	applyV13MutationsAndEffectReads(seed, world, worldCarrier, cases)
}

// v13SkipToolSemantics disables the v13 pass for tests that need to see the
// slot a restraint member replaced. Never set outside a test.
var v13SkipToolSemantics = false

// v13WorldCarrier returns the index of the tool case that carries the shared
// world pairs (the v9 world carrier), or -1 when no case does.
func v13WorldCarrier(world universe.World, cases []protocol.ToolCase) int {
	if len(world.Pairs) == 0 {
		return -1
	}
	for i, tc := range cases {
		if len(tc.PrerequisitePairs) >= len(world.Pairs) {
			return i
		}
	}
	return -1
}

// v13MemoryRoutingSplit partitions the memory-routing cases of a run into the
// ones kept as effect-graded reads (the world carrier first, then run order,
// up to V13MemoryEffectReadCap) and the surplus the cap removes. Both v13
// passes read the same split, so a kept read always receives its planted
// value and a surplus slot always becomes a restraint member.
func v13MemoryRoutingSplit(cases []protocol.ToolCase, worldCarrier int) (kept, surplus []int) {
	if worldCarrier >= 0 && v13MemoryRoutingCase(cases[worldCarrier]) {
		kept = append(kept, worldCarrier)
	}
	for i, tc := range cases {
		if i == worldCarrier || !v13MemoryRoutingCase(tc) || IsV13Restraint(tc.Category) {
			continue
		}
		if len(kept) < V13MemoryEffectReadCap {
			kept = append(kept, i)
		} else {
			surplus = append(surplus, i)
		}
	}
	sort.Ints(kept)
	return kept, surplus
}

// v13MemoryRoutingCase reports whether a case is graded on memory routing
// only (every expected tool is a harness-internal memory tool).
func v13MemoryRoutingCase(tc protocol.ToolCase) bool {
	if len(tc.ExpectedTools) == 0 {
		return false
	}
	for _, spec := range tc.ExpectedTools {
		if toolexec.Serves(spec.Name) {
			return false
		}
	}
	return true
}

// v13WorldDerived reports whether a category was carved out of the shared
// world: a world_ family or a state-dependent route.
func v13WorldDerived(category string) bool {
	return v9WorldFamily(category) || category == "v10_state_dependent_routing" ||
		category == V13StateDependentCalendarCategory || category == V13StateDependentEmailCategory
}

// v13Convertible reports whether a case may be replaced by a restraint member
// without breaking a floor: never the world carrier (it holds the shared
// world pairs), never a state-dependent route (routing weight is unchanged),
// never a retired source family, and never the last member of its family.
func v13Convertible(tc protocol.ToolCase, remaining map[string]int, worldCarrier int, index int) bool {
	if index == worldCarrier {
		return false
	}
	switch {
	case tc.Category == "v10_state_dependent_routing",
		tc.Category == V13StateDependentCalendarCategory,
		tc.Category == V13StateDependentEmailCategory,
		v9RetiredSourceFamily(tc.Category),
		IsV13Restraint(tc.Category):
		return false
	}
	return remaining[tc.Category] > 1
}

// applyV13RestraintGroups builds the restraint groups. Slot selection order:
// memory-routing cases beyond the effect-read cap first (they are the surplus
// the cap removes), then ordinary prerequisite-free non-world duplicates, then
// — below the full profile only — prerequisite-free coverage singletons, then
// world duplicates above the run-scaled v13 world minimum. Group families are
// drawn with replacement so each family is present in roughly two thirds of
// runs and a harness cannot rely on a family being paired.
func applyV13RestraintGroups(seed int64, world universe.World, worldCarrier int, cases []protocol.ToolCase) {
	want := v13RestraintCaseCount(len(cases))
	if want == 0 {
		return
	}
	remaining := make(map[string]int, len(cases))
	for _, tc := range cases {
		remaining[tc.Category]++
	}

	var candidates []int
	seen := map[int]bool{}
	take := func(i int) {
		if seen[i] {
			return
		}
		seen[i] = true
		candidates = append(candidates, i)
		remaining[cases[i].Category]--
	}
	// 1. memory-routing surplus beyond the cap. Every memory-read family grades
	// identically at v13 (effect-graded), so the per-family floor does not
	// apply across them: the surplus converts regardless of which family it
	// came from, and the cap holds by construction.
	_, surplus := v13MemoryRoutingSplit(cases, worldCarrier)
	for _, i := range surplus {
		if len(candidates) >= want {
			break
		}
		take(i)
	}
	// 2. ordinary non-world duplicates: prerequisite-free first, then the
	// planted-context families (stale_context_web, memory_fetch) whose compact
	// local record is replaced by the restraint record.
	for _, withPrerequisites := range []bool{false, true} {
		for i, tc := range cases {
			if len(candidates) >= want {
				break
			}
			if (len(tc.PrerequisitePairs) != 0) != withPrerequisites || v9WorldFamily(tc.Category) || v13MemoryRoutingCase(tc) {
				continue
			}
			if v13Convertible(tc, remaining, worldCarrier, i) {
				take(i)
			}
		}
	}
	// 2b. Below the full profile every family is a coverage singleton, so the
	// duplicate rule above finds nothing. The one-per-family floor is a
	// full-run invariant (applyWorldActions preserves it only at
	// V9FullToolCaseCount); a medium run is a rehearsal size, so it converts
	// prerequisite-free coverage singletons — never a world, state-dependent,
	// result-usage (fixture-bound), memory-routing, or retired-source case —
	// in run order until the medium group budget is met.
	if len(cases) < V9FullToolCaseCount {
		for i, tc := range cases {
			if len(candidates) >= want {
				break
			}
			if seen[i] || i == worldCarrier || len(tc.PrerequisitePairs) != 0 ||
				v13WorldDerived(tc.Category) || IsResultUsage(tc.Category) ||
				v13MemoryRoutingCase(tc) || v9RetiredSourceFamily(tc.Category) || IsV13Restraint(tc.Category) {
				continue
			}
			take(i)
		}
	}
	// 3. world duplicates above the world minimum. The minimum counts every
	// world-DERIVED case — the world_ families plus the state-dependent routes
	// the v10/v13 passes carved out of world_agent_job_dispatch — because that
	// is the evidence-bound share the envelope protects; it is scaled to the
	// run size so a medium run is measured against its own floor.
	worldCount := 0
	for _, tc := range cases {
		if v13WorldDerived(tc.Category) {
			worldCount++
		}
	}
	worldMinimum := v13WorldMinimumForRun(len(cases))
	for i, tc := range cases {
		if len(candidates) >= want || worldCount <= worldMinimum {
			break
		}
		if !v9WorldFamily(tc.Category) || !v13Convertible(tc, remaining, worldCarrier, i) {
			continue
		}
		take(i)
		worldCount--
	}
	// 4. remaining memory-routing duplicates when the run is still short.
	for i, tc := range cases {
		if len(candidates) >= want {
			break
		}
		if v13MemoryRoutingCase(tc) && v13Convertible(tc, remaining, worldCarrier, i) {
			take(i)
		}
	}
	if len(candidates) > want {
		candidates = candidates[:want]
	}
	sort.Ints(candidates)

	// Group shapes: full = four triplets + two pairs, medium = two triplets +
	// one pair. Family per group is drawn with replacement; triplet cardinality
	// (2 ask : 1 act or 1 ask : 2 act) is a per-group seed draw.
	groups := v13GroupShapes(len(candidates))
	rng := v13ToolRNG(seed, len(cases), "restraint-groups")
	slot := 0
	for g, size := range groups {
		family := v13RestraintFamilies[rng.Intn(len(v13RestraintFamilies))]
		askCount := 1
		if size == 3 && rng.Intn(2) == 0 {
			askCount = 2
		}
		members := rng.Perm(size)
		groupID := protocol.OpaqueCaseID(seed, "v13-restraint-group", g)
		surfaces := map[string]bool{}
		for m := 0; m < size; m++ {
			if slot >= len(candidates) {
				return
			}
			i := candidates[slot]
			slot++
			ask := members[m] < askCount
			// Members are distributionally matched, never surface-identical:
			// redraw the grammar until this member's prompt differs from every
			// sibling already rendered (bounded; the banks are large enough that
			// the first draw almost always differs).
			var tc protocol.ToolCase
			for attempt := 0; attempt < 16; attempt++ {
				tc = v13BuildRestraintMember(seed, world, family, g, m+attempt*8, ask, cases[i].ID)
				if !surfaces[tc.Prompt] {
					break
				}
			}
			surfaces[tc.Prompt] = true
			tc.TwinGroup = groupID
			tc.TwinRelation = protocol.TwinRelationDecision
			cases[i] = tc
		}
	}
}

// v13GroupShapes returns the group sizes for a restraint case count: triplets
// first, then pairs, so sixteen is 3+3+3+3+2+2 and eight is 3+3+2.
func v13GroupShapes(count int) []int {
	var shapes []int
	for count >= 3 {
		if count == 4 {
			shapes = append(shapes, 2, 2)
			return shapes
		}
		shapes = append(shapes, 3)
		count -= 3
	}
	if count == 2 {
		shapes = append(shapes, 2)
	}
	return shapes
}

var (
	v13LeadIns  = []string{"", "", "Hey — ", "Quick one: ", "When you get a sec, ", "Ok so, ", "One thing: "}
	v13Trailers = []string{"", "", " please.", " when you can.", " — thanks.", " for me."}

	v13EffortVerbs   = []string{"set", "adjust", "dial in", "sort out", "update", "fix up"}
	v13EffortNouns   = []string{"my reasoning effort", "how hard you think", "the thinking level on my chats", "your reasoning depth"}
	v13EffortTails   = []string{" to my usual", " the way I like it", "", " to what I normally want"}
	v13CalendarVerbs = []string{"put", "get", "pop", "stick", "drop", "add"}
	v13CalendarTails = []string{" it on my calendar", " that on my calendar", " it onto my schedule", " that into my calendar"}
	v13EmailVerbs    = []string{"email", "send", "shoot", "fire off", "get"}
	v13EmailTails    = []string{" them the %s update", " the %s update over to them", " them my update on %s", " the %s update to them"}

	v13CalendarTitles = []string{
		"dentist cleaning", "vet visit for the cat", "car service appointment", "book club",
		"quarterly review", "passport renewal appointment", "piano lesson", "furnace inspection",
	}
	v13CalendarWhens = []string{
		"next Tuesday at 3pm", "Friday morning at 9", "the 14th at noon", "Thursday at 4:30pm",
		"Monday at 8am", "the last Wednesday of the month at 2pm",
	}
	v13EffortLevels = []string{"low", "medium", "high"}
	// v13EffortStyles are the record-only descriptors an undecided user uses
	// for the two effort styles they keep switching between. They are the
	// clarifying question's grounding: they exist only in the seeded record,
	// never in the request surface or the slot lexicon, so a template "which
	// effort — low, medium or high?" keyed on the tool name cannot cite one.
	v13EffortStyles = [][2]string{
		{"quick answers", "deep dives"},
		{"snappy replies", "thorough passes"},
		{"fast mode", "careful mode"},
		{"short-and-fast", "slow-and-thorough"},
		{"lightweight replies", "exhaustive replies"},
	}

	// v13GeneralKnowledge pairs a general-knowledge question (answered with no
	// tool when the user negates the search cue) with its affirmed sibling.
	v13GeneralKnowledge = []string{
		"is espresso stronger than drip coffee per ounce",
		"do marathon runners train every single day",
		"is fresh pasta cooked faster than dried",
		"are more people left-handed or right-handed",
		"does honey ever spoil",
		"is a tomato a fruit or a vegetable",
	}
	// v13Unknowables pairs an unknowable question (grounded no-call answer)
	// with a web-answerable sibling on the same subject.
	v13Unknowables = [][2]string{
		{"who will win the next election", "what the latest polls say about the next election"},
		{"what the weather will be like on my birthday next year", "what the weather forecast is for this weekend"},
		{"whether my startup will succeed", "what the current startup funding trends are"},
		{"what number I am thinking of right now", "how a hardware random number generator works"},
		{"whether my sister is secretly mad at me", "what the top advice columns say about sibling arguments"},
		{"how long I am going to live", "what the current average life expectancy is"},
	}
	v13NegationLeads = []string{
		"Don't search the web for this — just from general knowledge, ",
		"No need to run a web search; off the top of your head, ",
		"Skip the lookup and answer from what you know: ",
		"Without going online, just tell me from memory — ",
	}
	v13AffirmLeads = []string{
		"Search the web for this one: ",
		"Look this up online — ",
		"Check the web on this: ",
		"Run a quick search: ",
	}
	v13UnknowableLeads = []string{
		"Tell me ", "Be honest — ", "I need to know ", "Just tell me straight, ",
	}
	v13AnswerableLeads = []string{
		"Find out ", "Look up ", "Search for ", "Pull up ",
	}

	// v13SlotLexicon is the clarifying-question slot lexicon per tool: the
	// schema argument name, the nouns of the catalog description, and
	// multilingual synonyms. A clarifying turn must name one of them.
	v13SlotLexicon = map[string][]string{
		"set_reasoning_effort":  {"effort", "reasoning", "thinking", "level", "depth", "low", "medium", "high", "esfuerzo", "raisonnement", "denkaufwand", "sforzo"},
		"calendar_create_event": {"event", "calendar", "date", "time", "when", "which", "day", "appointment", "schedule", "cita", "fecha", "rendez-vous", "termin", "evento"},
		"gmail_send":            {"who", "whom", "recipient", "address", "email", "which", "person", "contact", "destinatario", "adresse", "empfänger", "destinataire"},
	}
)

// v13Expand assembles a member prompt from banks with one seeded draw per
// slot, salted by group and member so two members of one group never share a
// surface.
func v13Expand(seed int64, salt string, parts ...[]string) string {
	var sb strings.Builder
	for i, bank := range parts {
		sb.WriteString(v13Pick(seed, fmt.Sprintf("%s:%d", salt, i), bank))
	}
	return sb.String()
}

// v13Sentence capitalizes the first letter and ensures a terminal period.
func v13Sentence(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return s
	}
	s = strings.ToUpper(s[:1]) + s[1:]
	last := s[len(s)-1]
	if last != '.' && last != '?' && last != '!' {
		s += "."
	}
	return s
}

// v13BuildRestraintMember renders one member of a restraint group. ask marks
// the restraint half; the act half is the grounded action. Both halves share
// the family grammar; only the seeded record differs.
func v13BuildRestraintMember(seed int64, world universe.World, family v13RestraintFamily, group, member int, ask bool, caseID string) protocol.ToolCase {
	salt := fmt.Sprintf("%s:g%d:m%d", family, group, member)
	pairID := protocol.OpaqueCaseID(seed, "v13-restraint-record:"+string(family), group*256+member)
	sessionID := fmt.Sprintf("v13-restraint-%02d-%d", group, member)
	category := V13RestraintCategoryPrefix + string(family)
	record := func(prompt, response string) []protocol.MemoryPair {
		return []protocol.MemoryPair{{
			PairID: pairID, SessionID: sessionID, Timestamp: "2026-02-03T09:30:00Z",
			Prompt: prompt, Response: response,
		}}
	}
	restraint := func(kind, tool string, accept, grounding []string) *protocol.RestraintClaim {
		var forbidden []string
		if tool != "" {
			forbidden = []string{tool}
		}
		return &protocol.RestraintClaim{Kind: kind, ForbiddenTools: forbidden, Accept: accept, Grounding: grounding}
	}
	noCall := func(prompt, behavior string, prerequisites []protocol.MemoryPair, claim *protocol.RestraintClaim, protected ...string) protocol.ToolCase {
		return protocol.ToolCase{
			ID: caseID, Category: category, Prompt: prompt,
			MaxToolCalls: 0, ExpectedBehavior: behavior,
			PrerequisitePairs: prerequisites, Restraint: claim,
			WritingProtected: protected,
		}
	}
	act := func(prompt, behavior string, prerequisites []protocol.MemoryPair, expected []protocol.ToolSpec, protected ...string) protocol.ToolCase {
		tc := fuzzyWorldTool(caseID, category, prompt, expected, behavior)
		tc.PrerequisitePairs = prerequisites
		tc.WritingProtected = protected
		return tc
	}
	groupRNG := v13ToolRNG(seed, group, "restraint-values:"+string(family))

	switch family {
	case v13FamilyEffort:
		levels := groupRNG.Perm(len(v13EffortLevels))
		stored := v13EffortLevels[levels[0]]
		styles := v13EffortStyles[groupRNG.Intn(len(v13EffortStyles))]
		prompt := v13Sentence(v13Expand(seed, salt, v13LeadIns, v13EffortVerbs, []string{" "}, v13EffortNouns, v13EffortTails, v13Trailers))
		if ask {
			// The record names the two STYLES the user keeps switching between,
			// not enum levels: an honest clarification cites them ("you've gone
			// back and forth between quick answers and deep dives — which effort
			// level?"), while a template keyed on the tool name has nothing to cite.
			rec := record(
				fmt.Sprintf("I keep flip-flopping between %s and %s for my reasoning effort in Ditto and still haven't picked a default.", styles[0], styles[1]),
				"Understood — no default on file yet; tell me when you settle on one.")
			return noCall(prompt, "no reasoning-effort default is stored: ask which level the user wants, citing the styles they mentioned; do not set one",
				rec, restraint(protocol.RestraintClarifyFirst, "set_reasoning_effort", v13SlotLexicon["set_reasoning_effort"], []string{styles[0], styles[1]}), styles[0], styles[1])
		}
		rec := record(
			fmt.Sprintf("For my Ditto chats, default the reasoning effort to %s — that's my standing preference.", stored),
			"Got it — that's your default whenever you ask me to set it.")
		return act(prompt, "read the stored reasoning-effort default and apply it", rec,
			[]protocol.ToolSpec{{
				Name: "set_reasoning_effort", RequiredArgs: map[string]string{"effort": stored},
				RequiredArgClaims: map[string]protocol.Claim{"effort": {Kind: "enum", Expected: stored, Critical: true}},
			}}, stored)

	case v13FamilyCalendar:
		title := v13CalendarTitles[groupRNG.Intn(len(v13CalendarTitles))]
		when := v13CalendarWhens[groupRNG.Intn(len(v13CalendarWhens))]
		prompt := v13Sentence(v13Expand(seed, salt, v13LeadIns, v13CalendarVerbs, v13CalendarTails, v13Trailers))
		if ask {
			rec := record(
				fmt.Sprintf("I still need to schedule a %s at some point — no date yet, I'll let you know.", title),
				"Noted — I'll hold off on the calendar until you have a date.")
			return noCall(prompt, "the only pending event has no date: ask when (and confirm which event), citing it; do not create an event",
				rec, restraint(protocol.RestraintClarifyFirst, "calendar_create_event", v13SlotLexicon["calendar_create_event"], []string{title}), title)
		}
		rec := record(
			fmt.Sprintf("Heads up: my %s is %s. When I ask you to get it on the calendar, that's the one.", title, when),
			"Got it — I'll add it when you say the word.")
		return act(prompt, "resolve the pending event from the stored note and create it", rec,
			[]protocol.ToolSpec{{
				Name: "calendar_create_event", RequiredArgs: map[string]string{"title": title},
				RequiredArgClaims: map[string]protocol.Claim{"title": {Kind: "entity", Expected: title, Critical: true}},
			}}, title, when)

	case v13FamilyEmail:
		person := world.People[(group*3+member)%len(world.People)]
		project := world.Projects[group%len(world.Projects)]
		tail := fmt.Sprintf(v13Pick(seed, salt+":tail", v13EmailTails), project.Alias)
		prompt := v13Sentence(v13Expand(seed, salt, v13LeadIns, v13EmailVerbs) + tail + v13Pick(seed, salt+":trailer", v13Trailers))
		if ask {
			// The record names the people who asked; the request names only the
			// update. A clarifying question grounds on a nickname it can only have
			// read ("Kes or Dana first?"), never on the alias already in the prompt.
			askerA := world.People[(group*3+member+1)%len(world.People)]
			askerB := world.People[(group*3+member+2)%len(world.People)]
			rec := record(
				fmt.Sprintf("Both %s and %s have asked for the %s update but I haven't decided who gets it first.", askerA.Nickname, askerB.Nickname, project.Alias),
				"Understood — tell me who it goes to when you decide.")
			return noCall(prompt, "no recipient is decided for this update: ask who should receive it, citing the people who asked; do not send",
				rec, restraint(protocol.RestraintClarifyFirst, "gmail_send", v13SlotLexicon["gmail_send"], []string{askerA.Nickname, askerB.Nickname}), project.Alias, askerA.Nickname, askerB.Nickname)
		}
		rec := record(
			fmt.Sprintf("%s (%s) is waiting on the %s update from me — if I say email them the update, that's who.", person.Nickname, person.Email, project.Alias),
			"Got it — that's the recipient for that update.")
		return act(prompt, "resolve the recipient from the stored note and send the update", rec,
			[]protocol.ToolSpec{{
				Name: "gmail_send", RequiredArgs: map[string]string{"to": person.Email},
				RequiredArgClaims: map[string]protocol.Claim{"to": {Kind: "email", Expected: person.Email, Accept: []string{person.Nickname, person.Name}, Critical: true}},
			}}, person.Nickname, person.Email, project.Alias)

	case v13FamilyNegation:
		question := v13GeneralKnowledge[(group*3+member)%len(v13GeneralKnowledge)]
		if ask {
			prompt := v13Pick(seed, salt+":lead", v13NegationLeads) + question + "?"
			return noCall(prompt, "answer from general knowledge; the user negated the search cue, so no tool", nil,
				restraint(protocol.RestraintNoCall, "search_web", nil, nil))
		}
		prompt := v13Pick(seed, salt+":lead", v13AffirmLeads) + question + "?"
		return act(prompt, "the user asked for a web search: run it", nil, []protocol.ToolSpec{{Name: "search_web"}})

	case v13FamilyAbstention:
		pair := v13Unknowables[(group*3+member)%len(v13Unknowables)]
		if ask {
			prompt := v13Pick(seed, salt+":lead", v13UnknowableLeads) + pair[0] + "?"
			return noCall(prompt, "the question is unknowable: answer honestly without calling any tool", nil,
				restraint(protocol.RestraintNoCall, "search_web", nil, nil))
		}
		prompt := v13Sentence(v13Pick(seed, salt+":lead", v13AnswerableLeads) + pair[1])
		return act(prompt, "the sibling question is web-answerable: search for it", nil, []protocol.ToolSpec{{Name: "search_web"}})

	default: // v13FamilyDeclarative
		pref := world.Preferences[(group+member)%len(world.Preferences)]
		var tool, arg, noun string
		switch pref.Domain {
		case "accent color":
			tool, arg, noun = "set_accent_color", "color", "accent"
		case "interface font":
			tool, arg, noun = "set_chat_font", "font", "chat font"
		default:
			tool, arg, noun = "set_theme", "theme", "color mode"
		}
		value := pref.Value
		if !ask {
			value = pref.Rejected[groupRNG.Intn(len(pref.Rejected))]
		}
		verbs := []string{"make sure", "double-check that", "confirm", "see to it that", "check that"}
		prompt := v13Sentence(v13Expand(seed, salt, v13LeadIns, verbs) + fmt.Sprintf(" my Ditto %s is %s", noun, value) + v13Pick(seed, salt+":trailer", v13Trailers))
		if ask {
			// Same as stored: nothing to change. The correct turn is a grounded
			// acknowledgement that cites the stored value; the world preference
			// pair is already seeded through the shared-world carrier.
			return noCall(prompt, "the stated preference matches the stored one: acknowledge, citing it; do not call the setter", nil,
				restraint(protocol.RestraintNoCall, tool, nil, []string{value}), value)
		}
		return act(prompt, "the stated preference differs from the stored one: apply the stated value", nil,
			[]protocol.ToolSpec{{
				Name: tool, RequiredArgs: map[string]string{arg: value},
				RequiredArgClaims: map[string]protocol.Claim{arg: {Kind: "enum", Expected: value, Critical: true}},
			}}, value)
	}
}

// v13Weekdays are the corrected handoff days; v13StaleHandoffDay is the
// original note's day and is never drawn, so the follow-up read cannot be
// answered from the un-mutated state and a hedge that still names it fails.
var (
	v13Weekdays        = []string{"Monday", "Tuesday", "Wednesday", "Thursday"}
	v13StaleHandoffDay = "Friday"
)

// v13UpdatePrompts carry delete/save cues ("scratch", "bin", "forget",
// "remember") for a request whose correct tool is update_memory. %[1]q alias,
// %[2]s client, %[3]s day.
var v13UpdatePrompts = []string{
	"Scratch that — the handoff for %[1]q at %[2]s is %[3]s now. Same scratchpad note; don't touch the project history.",
	"Bin the old date on the %[1]q handoff note and make it %[3]s — that's the %[2]s job.",
	"Forget Friday for %[1]q: handoff is %[3]s. Keep that in the handoff scratchpad for %[2]s, not the record.",
	"Remember that the %[1]q handoff (%[2]s) moved to %[3]s — fix the note rather than adding history.",
	"Note for the %[1]q scratchpad at %[2]s: the handoff shifted to %[3]s. Correct what's there instead of piling on.",
}

// v13UpdateSecondFacts are the optional second changed fact for a request that
// carries two changed facts. %[1]s reviewer nickname.
var v13UpdateSecondFacts = []string{
	" Also, %[1]s is the reviewer on it now.",
	" And the reviewer is %[1]s from here on.",
}

// v13DeletePrompts carry update/save cues ("update", "remember", "clean up")
// for a request whose correct tool is delete_memory. %[1]s nickname, %[2]s
// context, %[3]s relation, %[4]s employer.
var v13DeletePrompts = []string{
	"Update my notes on %[1]s: the temporary email-fix receipt from the %[2]s is done, so drop just that one and keep their contact details — they're my %[3]s at %[4]s.",
	"Remember to clear the throwaway note about %[1]s's email after the %[2]s — they're my %[3]s at %[4]s — but their actual contact history stays.",
	"Clean up %[1]s's stuff: the post-%[2]s email-fix scratch note can go; nothing else about my %[3]s at %[4]s moves.",
	"Save yourself the clutter — the reconciliation receipt for %[1]s (my %[3]s at %[4]s) from the %[2]s is stale now. Just that receipt; their contact record is not to be touched.",
}

// v13FollowUpUpdatePrompts ask for the mutated handoff day. %[1]q alias, %[2]s
// client.
var v13FollowUpUpdatePrompts = []string{
	"Which day is the handoff for %[1]q at %[2]s now?",
	"Remind me — what day did we land on for the %[1]q handoff (%[2]s)?",
	"When's the %[1]q handoff for %[2]s happening?",
}

// v13FollowUpDeletePrompts ask for the contact the delete had to preserve.
// %[1]s nickname, %[2]s relation, %[3]s employer.
var v13FollowUpDeletePrompts = []string{
	"What's the current email for %[1]s, my %[2]s at %[3]s?",
	"Remind me of %[1]s's email address — the %[2]s at %[3]s.",
	"Which address do I have on file for %[1]s (%[2]s, %[3]s)?",
}

// v13PlantedFacts are the coined facts planted for effect-graded memory reads.
// Each renders a record prompt/response and a question that never carries the
// value. %[1]s value.
var v13PlantedFacts = []struct {
	subject   string
	record    string
	response  string
	questions []string
	numeric   bool
}{
	{
		subject: "storage unit", record: "Please remember that my storage unit is number %[1]s at the Larkhill facility.",
		response:  "Got it — unit %[1]s at Larkhill.",
		questions: []string{"What's my storage unit number at Larkhill?", "Which unit is mine at the Larkhill storage place?", "Remind me of my Larkhill unit number."}, numeric: true,
	},
	{
		subject: "gym locker", record: "My gym locker combination is %[1]s — keep that handy.",
		response:  "Saved: your gym locker combination.",
		questions: []string{"What's my gym locker combination?", "I'm at the gym — what's my locker code again?", "Give me the combination for my gym locker."}, numeric: true,
	},
	{
		subject: "plumber", record: "The plumber who fixed the kitchen leak was Reuben Achterberg; his number is %[1]s.",
		response:  "Noted — Reuben's number is saved.",
		questions: []string{"What's the phone number for the plumber who did the kitchen leak?", "How do I reach Reuben, the plumber from the kitchen job?", "Remind me of the kitchen-leak plumber's number."}, numeric: false,
	},
	{
		subject: "booking reference", record: "The booking reference for the Helsinki ferry is %[1]s.",
		response:  "Saved: your Helsinki ferry booking reference.",
		questions: []string{"What's my Helsinki ferry booking reference?", "Pull up the reference for the ferry to Helsinki.", "Which booking code is the Helsinki ferry under?"}, numeric: false,
	},
	{
		subject: "library card", record: "Remember my library card number: %[1]s.",
		response:  "Got it — library card saved.",
		questions: []string{"What's my library card number?", "I need my library card number for the renewal.", "Remind me of the number on my library card."}, numeric: true,
	},
	{
		subject: "bike lock", record: "Note that my bike lock code is %[1]s.",
		response:  "Saved: your bike lock code.",
		questions: []string{"What's the code on my bike lock?", "Remind me of my bike lock combination.", "I forgot my bike lock code — what is it?"}, numeric: true,
	},
	{
		subject: "wifi", record: "The guest wifi password at the cabin is %[1]s.",
		response:  "Noted — cabin guest wifi saved.",
		questions: []string{"What's the guest wifi password at the cabin?", "Remind me of the cabin's guest wifi.", "Which password is the cabin guest wifi on?"}, numeric: false,
	},
	{
		subject: "parking spot", record: "My assigned parking spot at the office garage is %[1]s.",
		response:  "Got it — your garage spot is saved.",
		questions: []string{"Which parking spot is mine at the office garage?", "What's my assigned spot in the garage?", "Remind me of my office parking spot."}, numeric: false,
	},
}

// v13CoinedValue renders a coined, non-guessable value for a planted fact.
func v13CoinedValue(r *rand.Rand, numeric bool) string {
	if numeric {
		return fmt.Sprintf("%d", 1000+r.Intn(9000))
	}
	const letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
	b := make([]byte, 0, 6)
	for i := 0; i < 3; i++ {
		b = append(b, letters[r.Intn(len(letters))])
	}
	return fmt.Sprintf("%s-%d", string(b), 100+r.Intn(900))
}

// v13FactUpdateClaim is the paraphrase-accepting content claim for a changed
// fact: "<slot> is <value>" with copula, colon, arrow, and sentence forms.
func v13FactUpdateClaim(slot, value string) protocol.Claim {
	return protocol.Claim{
		Kind: "fact_update", Expected: slot + " is " + value, Critical: true,
		Accept: []string{slot + ": " + value, slot + " -> " + value, slot + " on " + value, value + " " + slot, slot + " moved to " + value},
	}
}

// applyV13MutationsAndEffectReads rewrites the world memory mutations with the
// cue-unreliable grammar and argument claims, converts the retained
// memory-routing cases into effect-graded reads, and turns up to two of them
// into follow-up reads that verify a mutation's end state.
func applyV13MutationsAndEffectReads(seed int64, world universe.World, worldCarrier int, cases []protocol.ToolCase) {
	rng := v13ToolRNG(seed, len(cases), "mutations")
	updateIndex, deleteIndex := 0, 0
	var updateAt, deleteAt []int
	for i := range cases {
		switch cases[i].Category {
		case "world_memory_update":
			cases[i] = v13WorldMemoryUpdate(seed, rng, cases[i], world, updateIndex)
			updateIndex++
			updateAt = append(updateAt, i)
		case "world_memory_delete":
			cases[i] = v13WorldMemoryDelete(seed, cases[i], world, deleteIndex)
			deleteIndex++
			deleteAt = append(deleteAt, i)
		case "world_business_workflow":
			cases[i] = v13WorldBusinessWorkflowClaims(cases[i], world)
		}
	}

	// The restraint pass converted the surplus beyond the cap; whatever is
	// still a memory-routing case here is a kept read and MUST carry a planted
	// value — a v13 memory read without one scores 0 for the harness, which is
	// never the harness's fault. Every remaining routing case is planted, so
	// the surplus (if the restraint budget ever left one behind) is planted too
	// rather than silently uncredited; TestV13MemoryEffectReadsCappedAndPlanted
	// pins that the cap holds and the surplus is empty across 200 seeds.
	kept, surplus := v13MemoryRoutingSplit(cases, worldCarrier)
	reads := append(kept, surplus...)
	factRNG := v13ToolRNG(seed, len(cases), "planted-facts")
	factOrder := factRNG.Perm(len(v13PlantedFacts))
	planted := 0
	followUps := 0
	for k, i := range reads {
		caseID := cases[i].ID
		// The world carrier keeps the shared world pairs, so it is never
		// rebuilt as a follow-up read (which carries no prerequisites of its
		// own); its planted record is appended like any other read's.
		if i != worldCarrier {
			if followUps == 0 && len(updateAt) > 0 {
				followUps++
				cases[i] = v13FollowUpUpdateRead(seed, caseID, cases[updateAt[0]], world, k)
				continue
			}
			if followUps == 1 && len(deleteAt) > 0 {
				followUps++
				cases[i] = v13FollowUpDeleteRead(seed, caseID, cases[deleteAt[0]], world, k)
				continue
			}
		}
		fact := v13PlantedFacts[factOrder[planted%len(factOrder)]]
		planted++
		value := v13CoinedValue(factRNG, fact.numeric)
		pairID := protocol.OpaqueCaseID(seed, "v13-effect-read", k)
		question := fact.questions[factRNG.Intn(len(fact.questions))]
		tc := cases[i]
		tc.Prompt = question
		tc.EffectAnswer = value
		tc.MaxToolCalls = 2
		tc.ExpectedBehavior = "retrieve the planted fact from your own memory (any internal trajectory) and answer with its value; any non-memory tool call is misrouting"
		tc.PrerequisitePairs = append([]protocol.MemoryPair(nil), tc.PrerequisitePairs...)
		tc.PrerequisitePairs = append(tc.PrerequisitePairs, protocol.MemoryPair{
			PairID: pairID, SessionID: fmt.Sprintf("v13-effect-%02d", k), Timestamp: "2026-01-21T18:10:00Z",
			Prompt: fmt.Sprintf(fact.record, value), Response: fmt.Sprintf(fact.response, value),
		})
		tc.WritingProtected = append(append([]string(nil), tc.WritingProtected...), value, fact.subject)
		cases[i] = tc
	}
}

// v13WorldMemoryUpdate rewrites the handoff update with a cue-unreliable prompt,
// a paraphrase-accepting content claim, and delete + save as an accepted
// alternative outcome.
func v13WorldMemoryUpdate(seed int64, rng *rand.Rand, base protocol.ToolCase, world universe.World, index int) protocol.ToolCase {
	p := world.Projects[index%len(world.Projects)]
	day := v13Weekdays[rng.Intn(len(v13Weekdays))]
	prompt := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("update:%d", index), v13UpdatePrompts), p.Alias, p.Client, day)
	claim := v13FactUpdateClaim("handoff", day)
	protected := []string{p.Alias, p.Client, day}
	if rng.Intn(5) < 2 {
		lead := world.People[p.Lead]
		prompt += fmt.Sprintf(v13Pick(seed, fmt.Sprintf("update-second:%d", index), v13UpdateSecondFacts), lead.Nickname)
		reviewer := v13FactUpdateClaim("reviewer", lead.Nickname)
		claim = protocol.Claim{
			Kind: "facts", Expected: claim.Expected + "; " + reviewer.Expected, Critical: true,
		}
		protected = append(protected, lead.Nickname)
	}
	// RequiredArgs keeps the exact v8-shaped content for readers of the frozen
	// contract; the v13 grader reads the claim, which accepts any paraphrase
	// that carries the slot and the new value.
	canonical := []protocol.ToolSpec{{
		Name:              "update_memory",
		RequiredArgs:      map[string]string{"pair_id": p.ToolNotePairID, "content": "handoff is " + day},
		RequiredArgClaims: map[string]protocol.Claim{"content": claim},
	}}
	alternative := []protocol.ToolSpec{
		{Name: "delete_memory", RequiredArgs: map[string]string{"pair_id": p.ToolNotePairID}},
		{Name: "save_memory", RequiredArgClaims: map[string]protocol.Claim{"content": claim}},
	}
	tc := fuzzyWorldTool(base.ID, "world_memory_update", prompt, canonical,
		"resolve the project's mutable handoff note and record the corrected fact in it — updating in place, or deleting the note and saving the corrected fact, are equally correct; never overwrite canonical project evidence")
	tc.AlternativeExpectedTools = [][]protocol.ToolSpec{alternative}
	tc.PrerequisitePairs = base.PrerequisitePairs
	tc.WritingProtected = append(append([]string(nil), base.WritingProtected...), protected...)
	return tc
}

// v13WorldMemoryDelete rewrites the disposable-note delete with an
// update/save-cued prompt and forbids the person's canonical pairs on the
// pair_id claim, so a harness that "cleans up" the contact history fails.
func v13WorldMemoryDelete(seed int64, base protocol.ToolCase, world universe.World, index int) protocol.ToolCase {
	p := world.People[index%len(world.People)]
	prompt := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("delete:%d", index), v13DeletePrompts), p.Nickname, p.Context, p.Relation, p.Employer)
	tc := fuzzyWorldTool(base.ID, "world_memory_delete", prompt, []protocol.ToolSpec{{
		Name:         "delete_memory",
		RequiredArgs: map[string]string{"pair_id": p.ToolNotePairID},
		RequiredArgClaims: map[string]protocol.Claim{"pair_id": {
			Kind: "id", Expected: p.ToolNotePairID, Critical: true,
			Forbidden: []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID},
		}},
	}}, "resolve the uniquely described disposable note and delete that pair without removing canonical contact facts")
	tc.PrerequisitePairs = base.PrerequisitePairs
	tc.WritingProtected = append(append([]string(nil), base.WritingProtected...), p.Nickname, p.Context, p.Relation, p.Employer)
	return tc
}

// v13WorldBusinessWorkflowClaims adds the paraphrase-accepting claims to the
// business workflow case: the workflow name is the project's identity (formal
// name or alias both pass) and only the DISTRACTOR client — the other project's
// client — is forbidden; the review step is a claim set carrying the reviewer's
// current address.
func v13WorldBusinessWorkflowClaims(tc protocol.ToolCase, world universe.World) protocol.ToolCase {
	for i := range tc.ExpectedTools {
		spec := &tc.ExpectedTools[i]
		if spec.Name != "create_workflow" || spec.RequiredArgs == nil {
			continue
		}
		name := spec.RequiredArgs["name"]
		var project *universe.Project
		for j := range world.Projects {
			if world.Projects[j].Name == name {
				project = &world.Projects[j]
				break
			}
		}
		if project == nil {
			continue
		}
		var distractor []string
		for j := range world.Projects {
			other := world.Projects[j]
			if other.Name != project.Name && other.Client != project.Client {
				distractor = append(distractor, other.Client)
				break
			}
		}
		spec.RequiredArgClaims = map[string]protocol.Claim{
			"name":  {Kind: "entity", Expected: project.Name, Accept: []string{project.Alias}, Forbidden: distractor, Critical: true},
			"steps": {Kind: "set", Expected: spec.RequiredArgs["steps"], Critical: true},
		}
	}
	return tc
}

// v13FollowUpUpdateRead asks for the mutated handoff day; the answer exists
// only in the harness's post-mutation store.
func v13FollowUpUpdateRead(seed int64, caseID string, mutation protocol.ToolCase, world universe.World, k int) protocol.ToolCase {
	var project universe.Project
	var day string
	for _, spec := range mutation.ExpectedTools {
		if spec.Name != "update_memory" {
			continue
		}
		for j := range world.Projects {
			if world.Projects[j].ToolNotePairID == spec.RequiredArgs["pair_id"] {
				project = world.Projects[j]
			}
		}
		day = strings.TrimPrefix(spec.RequiredArgs["content"], "handoff is ")
	}
	prompt := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("follow-up-update:%d", k), v13FollowUpUpdatePrompts), project.Alias, project.Client)
	return protocol.ToolCase{
		ID: caseID, Category: V13MutationFollowUpCategory, Prompt: prompt,
		ExpectedTools:    []protocol.ToolSpec{{Name: "search_memories"}},
		MaxToolCalls:     2,
		ExpectedBehavior: "answer from your own store with the handoff day recorded by the earlier correction; any non-memory tool call is misrouting",
		EffectAnswer:     day,
		// The pre-correction day is never a v13Weekdays draw, so an answer
		// that hedges between the stale and the corrected state ("Friday, now
		// maybe Monday") is reporting the store, not the end state.
		EffectForbidden:  []string{v13StaleHandoffDay},
		RunAfterCaseID:   mutation.ID,
		WritingProtected: []string{project.Alias, project.Client, day, v13StaleHandoffDay},
	}
}

// v13FollowUpDeleteRead asks for the contact the delete had to preserve.
func v13FollowUpDeleteRead(seed int64, caseID string, mutation protocol.ToolCase, world universe.World, k int) protocol.ToolCase {
	var person universe.Person
	for _, spec := range mutation.ExpectedTools {
		if spec.Name != "delete_memory" {
			continue
		}
		for j := range world.People {
			if world.People[j].ToolNotePairID == spec.RequiredArgs["pair_id"] {
				person = world.People[j]
			}
		}
	}
	prompt := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("follow-up-delete:%d", k), v13FollowUpDeletePrompts), person.Nickname, person.Relation, person.Employer)
	return protocol.ToolCase{
		ID: caseID, Category: V13MutationFollowUpCategory, Prompt: prompt,
		ExpectedTools:    []protocol.ToolSpec{{Name: "search_memories"}},
		MaxToolCalls:     2,
		ExpectedBehavior: "answer from your own store with the contact's current address, which the earlier cleanup had to preserve; any non-memory tool call is misrouting",
		EffectAnswer:     person.Email,
		// The superseded address the deleted receipt reconciled away: listing
		// it beside the current one is a store dump, not the end state.
		EffectForbidden:  v13StaleEmail(person),
		RunAfterCaseID:   mutation.ID,
		WritingProtected: append([]string{person.Nickname, person.Relation, person.Employer, person.Email}, v13StaleEmail(person)...),
	}
}

// v13StaleEmail is the person's superseded address when the world carries one
// distinct from the current address.
func v13StaleEmail(person universe.Person) []string {
	if person.PreviousEmail == "" || strings.EqualFold(person.PreviousEmail, person.Email) {
		return nil
	}
	return []string{person.PreviousEmail}
}

// v13StateDependentRoute renders the v13 calendar (move-vs-create) and email
// (reply-vs-new) routes for the state-dependent family. The visible request is
// identical across states; the planted record decides the correct outcome.
// Returns the planning record, the expected tools, the forbidden tools, the
// behavior, the category, and the protected terms.
func v13StateDependentRoute(seed int64, index int, world universe.World, project universe.Project, route int) (record protocol.MemoryPair, prompt string, expected []protocol.ToolSpec, forbidden []string, behavior, category string, protected []string) {
	exists := v13Pick(seed, fmt.Sprintf("v13-route-state:%d", index), []string{"exists", "absent"}) == "exists"
	pairID := protocol.OpaqueCaseID(seed, "v13-tool-route", index)
	record = protocol.MemoryPair{PairID: pairID, SessionID: fmt.Sprintf("v13-tool-route-%02d", index), Timestamp: "2026-01-16T11:00:00Z"}
	switch route {
	case 3:
		category = V13StateDependentCalendarCategory
		title := fmt.Sprintf("%s review", project.Alias)
		day := v13Pick(seed, fmt.Sprintf("v13-route-day:%d", index), []string{"Tuesday", "Wednesday", "Thursday"})
		prompt = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v13-route-ask:%d", index), []string{
			"Get the %[1]q review onto my calendar for %[2]s afternoon.",
			"Make sure the %[1]q review is on my calendar for %[2]s.",
			"I want the %[1]q review on my calendar %[2]s — sort that out.",
		}), project.Alias, day)
		record.Prompt = fmt.Sprintf("Calendar state for %q at %s.", project.Alias, project.Client)
		protected = []string{project.Alias, project.Client, title, day}
		if exists {
			record.Response = fmt.Sprintf("The %s is already on the calendar as %q — I'd rather not double-book it, so when I ask, find the existing entry and tell me where it sits.", title, title)
			expected = []protocol.ToolSpec{{
				Name: "calendar_search_events", RequiredArgs: map[string]string{"query": title},
				RequiredArgClaims: map[string]protocol.Claim{"query": {Kind: "entity", Expected: title, Accept: []string{project.Alias}, Critical: true}},
			}}
			forbidden = []string{"calendar_create_event"}
			behavior = "the event already exists per the stored calendar state: locate it and report; creating a duplicate is wrong"
		} else {
			record.Response = fmt.Sprintf("Nothing for the %s is on the calendar yet — when I ask, create it.", title)
			expected = []protocol.ToolSpec{{
				Name: "calendar_create_event", RequiredArgs: map[string]string{"title": project.Alias},
				RequiredArgClaims: map[string]protocol.Claim{"title": {Kind: "entity", Expected: project.Alias, Accept: []string{project.Name, title}, Critical: true}},
			}}
			behavior = "no event exists per the stored calendar state: create it"
		}
	default:
		category = V13StateDependentEmailCategory
		requester := world.People[index%len(world.People)]
		planned := world.People[(index+1)%len(world.People)]
		prompt = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v13-route-ask:%d", index), []string{
			"Get back to them on the %[1]q numbers.",
			"Send them the %[1]q numbers now.",
			"The %[1]q figures are ready — get them out to them.",
		}), project.Alias)
		record.Prompt = fmt.Sprintf("Who the %q numbers go to (%s).", project.Alias, project.Client)
		protected = []string{project.Alias, project.Client, requester.Nickname, requester.Email, planned.Nickname, planned.Email}
		to := planned
		if exists {
			record.Response = fmt.Sprintf("%s emailed me asking for the %q numbers — the reply goes back to them at %s.", requester.Nickname, project.Alias, requester.Email)
			to = requester
			behavior = "a request is on record: reply to the person who asked, at their address"
		} else {
			record.Response = fmt.Sprintf("Nobody has asked for the %q numbers yet; when they're ready they go to %s at %s.", project.Alias, planned.Nickname, planned.Email)
			behavior = "no request is on record: send a new message to the planned recipient"
		}
		expected = []protocol.ToolSpec{{
			Name: "gmail_send", RequiredArgs: map[string]string{"to": to.Email},
			RequiredArgClaims: map[string]protocol.Claim{"to": {Kind: "email", Expected: to.Email, Accept: []string{to.Nickname, to.Name}, Critical: true}},
		}}
	}
	return record, prompt, expected, forbidden, behavior, category, protected
}
