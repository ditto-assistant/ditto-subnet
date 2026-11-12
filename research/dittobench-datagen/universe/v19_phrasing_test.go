package universe

import (
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v19CopyingMinerCeiling is the most a copying miner's regex suite may match
// of any family's v19 renderings (DITTO-2738 acceptance 1).
const v19CopyingMinerCeiling = 0.40

// v19BriefRoutingTerms are terms quoted from a top v18 agent's routing
// regexes, all verbatim from the v18 fixed lists. No v19 rendering carries one.
var v19BriefRoutingTerms = []string{
	"rule did i set", "owes the next", "has the ball", "state of play", "advice i kept", "as far as the records go",
	"how many days did we spend in", "after every recorded handover", "after every recorded reassignment", "where are we with",
}

// v19Question is one rendered question and the slot values it carries,
// which the copying-miner suite masks: a regex keyed on a value bank (a trip
// name, a schema label) is not keyed on wording, and v19 keeps every value.
type v19Question struct {
	text string
	mask []string
}

// v19Corpus renders every phrase-grammar family of the given seeds at
// benchVersion, keyed by family: story oracles by oracle kind, programs by
// phrase family, abstention probes and their twins by oracle kind.
func v19Corpus(t *testing.T, benchVersion int, seeds []int64) map[string][]v19Question {
	t.Helper()
	out := map[string][]v19Question{}
	add := func(family, question string, mask ...[]string) {
		var all []string
		for _, m := range mask {
			all = append(all, m...)
		}
		out[family] = append(out[family], v19Question{question, all})
	}
	for _, seed := range seeds {
		w := GenerateForVersion(seed, 3, benchVersion)
		for _, plan := range w.storyQuestionCandidatesV13() {
			v2 := w.StoryArcs[plan.oracleIndex].V2
			unit := []string{v2.SequenceNoun}
			if v2.Quantity != nil {
				unit = append(unit, v2.Quantity.Kind)
			}
			add(plan.oracleKind, plan.Case.Question, plan.Constraints, unit)
		}
		business, err := GenerateV13ProgramsForVersion(seed, 32, benchVersion)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range business {
			if c.Plan.phrase != "" {
				add(c.Plan.phrase, v19ProgramBody(c.Plan.Case.Question), c.Plan.Constraints, c.Plan.Case.WritingProtected)
			}
		}
		personal, err := GenerateV13PersonalProgramsForVersion(seed, 28, benchVersion)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range personal {
			add(c.Plan.phrase, v19ProgramBody(c.Plan.Case.Question), c.Plan.Constraints, c.Plan.Case.WritingProtected)
		}
		for _, plan := range v19AbstentionPlans(w, GenerateForVersion(seed^0x5eed, 3, benchVersion)) {
			add(plan.oracleKind, plan.Case.Question, plan.Constraints, plan.Case.GroundingTokens)
		}
	}
	return out
}

// v19ProgramBody drops the "Use only case file <scope>." prefix every program
// question carries, which no family owns.
func v19ProgramBody(question string) string {
	if i := strings.Index(question, ". "); strings.HasPrefix(question, "Use only case file ") && i > 0 {
		return question[i+2:]
	}
	return question
}

// v19AbstentionPlans renders each abstention probe and its answerable twin
// over the world's probe entities; other supplies cross-user identities.
func v19AbstentionPlans(w, other World) []QuestionPlan {
	var out []QuestionPlan
	for k, probe := range w.Probes.Absent {
		out = append(out, w.pureAbsencePlan(probe.Person, k), w.contactCurrentSurface(probe.Person, k))
	}
	for k := range w.Probes.NearMiss {
		out = append(out, w.nearMissPlan(k))
	}
	for k := range w.Probes.Handles {
		out = append(out, w.staleHandlePlan(k, k), w.handleCurrentPlan(k, k))
	}
	for i := range w.Trips {
		out = append(out, w.falsePremisePlan(i, i), w.tripChangedLegCurrentSurface(i, i+1))
	}
	for k := range w.Probes.Threads {
		out = append(out, w.insufficientPlan(k))
	}
	for i := range w.Projects {
		out = append(out, w.projectOutstandingSurface(i, i+1))
	}
	for i := 0; i < len(w.People) && i < len(other.People); i++ {
		p := other.People[i]
		out = append(out, w.crossUserPlan(V13CrossUserFact{Anchor: i, Name: p.Name, Employer: p.Employer, Context: p.Context, Email: p.Email}, i))
	}
	return out
}

var v19Word = regexp.MustCompile(`[a-z0-9]+`)

// v19Trigrams are the question's three-word terms, none spanning a masked
// slot value.
func v19Trigrams(q v19Question) map[string]bool {
	text := strings.ToLower(q.text)
	mask := append([]string(nil), q.mask...)
	sort.Slice(mask, func(i, j int) bool { return len(mask[i]) > len(mask[j]) })
	for _, value := range mask {
		if value = strings.ToLower(strings.TrimSpace(value)); value != "" {
			text = strings.ReplaceAll(text, value, " | ")
		}
	}
	out := map[string]bool{}
	// A term never spans a masked value or a sentence boundary.
	for _, segment := range regexp.MustCompile(`[|.?!:;—]`).Split(text, -1) {
		words := v19Word.FindAllString(segment, -1)
		for i := 0; i+3 <= len(words); i++ {
			out[strings.Join(words[i:i+3], " ")] = true
		}
	}
	return out
}

// v19MinerSuite mimics a miner who copies the v18 lists: for each family, the
// three-word terms of its v18 wording that recur (in >= 3% of its questions,
// at least twice) and route (recurring in at most two other families, so
// boilerplate every program shares, such as the schema instruction, drops
// out). Slot values are masked first (v19Trigrams).
func v19MinerSuite(corpus map[string][]v19Question, family string) map[string]bool {
	const recur = 0.03
	recurring := map[string]map[string]bool{}
	for name, questions := range corpus {
		counts := map[string]int{}
		for _, q := range questions {
			for g := range v19Trigrams(q) {
				counts[g]++
			}
		}
		recurring[name] = map[string]bool{}
		for g, count := range counts {
			if count >= 2 && float64(count) >= recur*float64(len(questions)) {
				recurring[name][g] = true
			}
		}
	}
	suite := map[string]bool{}
	for g := range recurring[family] {
		shared := 0
		for name, terms := range recurring {
			if name != family && terms[g] {
				shared++
			}
		}
		if shared <= 2 {
			suite[g] = true
		}
	}
	return suite
}

// v19TopMatches lists the suite terms that match v19 renderings most often.
func v19TopMatches(suite map[string]bool, questions []v19Question) string {
	counts := map[string]int{}
	for _, q := range questions {
		for g := range v19Trigrams(q) {
			if suite[g] {
				counts[g]++
			}
		}
	}
	terms := make([]string, 0, len(counts))
	for g := range counts {
		terms = append(terms, g)
	}
	sort.Slice(terms, func(i, j int) bool { return counts[terms[i]] > counts[terms[j]] })
	if len(terms) > 8 {
		terms = terms[:8]
	}
	out := make([]string, 0, len(terms))
	for _, g := range terms {
		out = append(out, fmt.Sprintf("%q x%d", g, counts[g]))
	}
	return strings.Join(out, ", ")
}

func v19MatchRate(suite map[string]bool, questions []v19Question) float64 {
	hits := 0
	for _, q := range questions {
		for g := range v19Trigrams(q) {
			if suite[g] {
				hits++
				break
			}
		}
	}
	return float64(hits) / float64(len(questions))
}

func v19SeedRange(first, n int64) []int64 {
	out := make([]int64, 0, n)
	for i := int64(0); i < n; i++ {
		out = append(out, first+i)
	}
	return out
}

// Acceptance 1: a regex suite built from the v18 fixed lists matches well
// under half of each family's v19 renderings, on seeds the suite never saw.
func TestV19CopyingMinerSuiteMissesV19Wording(t *testing.T) {
	v18 := v19Corpus(t, protocol.BenchVersionV18, v19SeedRange(1, 24))
	v19 := v19Corpus(t, protocol.BenchVersionV19, v19SeedRange(5001, 24))
	// The story anchor is shared by every story oracle; route it as one group.
	v18["story-any"], v19["story-any"] = nil, nil
	for name := range v18 {
		if isStoryV2Oracle(name) {
			v18["story-any"] = append(v18["story-any"], v18[name]...)
			delete(v18, name)
		}
	}
	for name := range v19 {
		if isStoryV2Oracle(name) {
			v19["story-any"] = append(v19["story-any"], v19[name]...)
			delete(v19, name)
		}
	}
	families := make([]string, 0, len(v18))
	for name := range v18 {
		families = append(families, name)
	}
	sort.Strings(families)
	for _, family := range families {
		if len(v19[family]) == 0 {
			t.Errorf("%s: no v19 renderings", family)
			continue
		}
		corpus := v18
		if family == "story-any" {
			corpus = v19StoryGroupCorpus(v18)
		}
		suite := v19MinerSuite(corpus, family)
		before, after := v19MatchRate(suite, v18[family]), v19MatchRate(suite, v19[family])
		t.Logf("%-34s suite %3d terms  v18 %5.1f%%  v19 %5.1f%%  (n=%d)", family, len(suite), 100*before, 100*after, len(v19[family]))
		if len(suite) == 0 {
			t.Errorf("%s: the v18 corpus yields no distinctive terms; the suite is not measuring anything", family)
		}
		if after > v19CopyingMinerCeiling {
			t.Errorf("%s: the v18 copying-miner suite matches %.1f%% of v19 renderings, ceiling %.0f%%; top terms: %s", family, 100*after, 100*v19CopyingMinerCeiling, v19TopMatches(suite, v19[family]))
		}
	}
	for family, questions := range v19 {
		for _, q := range questions {
			lower := strings.ToLower(q.text)
			for _, term := range v19BriefRoutingTerms {
				if strings.Contains(lower, term) {
					t.Errorf("%s: v19 rendering carries the v18 routing term %q: %s", family, term, q.text)
				}
			}
		}
	}
}

// v19StoryGroupCorpus pools every story oracle into one family against the
// rest, so the suite keys on the shared anchor wording.
func v19StoryGroupCorpus(v18 map[string][]v19Question) map[string][]v19Question {
	out := map[string][]v19Question{"story-any": v18["story-any"]}
	for name, questions := range v18 {
		if name != "story-any" && !isStoryV2Oracle(name) {
			out[name] = questions
		}
	}
	return out
}

// v19SentinelSlots fills every slot name with a token no frame text contains.
func v19SentinelSlots() V19Slots {
	slots := V19Slots{}
	for _, g := range v19Grammars {
		for _, frame := range append(append([]string(nil), g.frames...), g.closers...) {
			for _, m := range v19SlotPattern.FindAllStringSubmatch(frame, -1) {
				slots[m[1]] = "qq" + m[1] + "qq"
			}
		}
		for _, options := range g.classes {
			for _, option := range options {
				for _, m := range v19SlotPattern.FindAllStringSubmatch(option, -1) {
					slots[m[1]] = "qq" + m[1] + "qq"
				}
			}
		}
	}
	return slots
}

func v19HasTerm(text, term string) bool {
	pattern := regexp.QuoteMeta(strings.ToLower(term))
	if strings.HasSuffix(term, "*") {
		pattern = regexp.QuoteMeta(strings.ToLower(strings.TrimSuffix(term, "*"))) + `[a-z]*`
	}
	return regexp.MustCompile(`(^|[^a-z0-9])` + pattern + `($|[^a-z0-9])`).MatchString(strings.ToLower(text))
}

// Acceptance 2 and the ambiguity guard: each family yields many distinct
// wordings, and every one keeps the family's slot values verbatim, carries one
// term of each of its meaning markers and none of its forbidden terms.
func TestV19PhraseFamiliesKeepSlotsAndMeaning(t *testing.T) {
	const identities = 400
	slots := v19SentinelSlots()
	statusWords := map[string]bool{}
	for status := range storyStatusVocabulary {
		statusWords[status] = true
	}
	for _, family := range V19PhraseFamilies() {
		g := v19Grammars[family]
		distinct := map[string]bool{}
		for i := 0; i < identities; i++ {
			seed := int64(1 + i%7)
			q := V19Phrase(family, seed, fmt.Sprintf("id-%d", i), slots)
			distinct[q] = true
			for _, name := range g.required {
				if !strings.Contains(q, slots[name]) {
					t.Fatalf("%s: rendering drops slot %s: %q", family, name, q)
				}
			}
			for _, set := range g.markers {
				found := false
				for _, term := range set {
					if v19HasTerm(q, v19Fill(term, slots, family)) {
						found = true
						break
					}
				}
				if !found {
					t.Fatalf("%s: rendering carries none of the markers %v: %q", family, set, q)
				}
			}
			for _, term := range g.forbid {
				if v19HasTerm(q, term) {
					t.Fatalf("%s: rendering carries forbidden %q: %q", family, term, q)
				}
			}
			if isStoryV2Oracle(family) || family == V19PhraseStoryAnchor || family == V19PhraseStoryAnchorTail {
				for status := range statusWords {
					if v19HasTerm(q, status) {
						t.Fatalf("%s: story rendering names the status %q: %q", family, status, q)
					}
				}
			}
			if strings.ContainsAny(q, "<>[]{}") {
				t.Fatalf("%s: unexpanded grammar in %q", family, q)
			}
		}
		// A v18 family has at most 12 task frames (story) or 3 forms x 6
		// openers x 3 closers (programs); the grammar must do far better.
		min := 150
		if g.plain {
			min = 40
		}
		if len(distinct) < min {
			t.Errorf("%s: %d distinct renderings over %d identities, want >= %d", family, len(distinct), identities, min)
		}
		t.Logf("%-34s %3d distinct of %d", family, len(distinct), identities)
	}
}

// Every program family's grammar names the args its hook passes.
func TestV19ProgramGrammarArgsMatchHooks(t *testing.T) {
	for _, family := range V19PhraseFamilies() {
		g := v19Grammars[family]
		if strings.HasPrefix(family, "program-") || strings.HasPrefix(family, "personal-") {
			if len(g.args) == 0 {
				t.Errorf("%s: program family without hook args", family)
			}
			for _, name := range g.required {
				if !contains(g.args, name) {
					t.Errorf("%s: required slot %s is not a hook arg", family, name)
				}
			}
		}
	}
}

// Acceptance 3: the grammar changes only the question. Every rephrased plan
// keeps the oracle's answer, claims and evidence, and the generation-time
// oracle still derives the expected answer from the same world.
func TestV19PhrasingLeavesAnswersUnchanged(t *testing.T) {
	type answer struct {
		Expected  string
		Kind      string
		Items     []string
		AcceptAny []string
		Claims    []protocol.Claim
		Evidence  []string
	}
	of := func(p QuestionPlan) answer {
		return answer{p.Case.ExpectedAnswer, p.Case.AnswerKind, p.Case.AnswerItems, p.Case.AcceptAny, p.Case.Claims, p.RequiredPairIDs}
	}
	for _, seed := range v19SeedRange(41, 30) {
		w19 := GenerateForVersion(seed, 3, protocol.BenchVersionV19)
		w18 := w19
		w18.BenchVersion = protocol.BenchVersionV18
		v18Story := map[string]QuestionPlan{}
		for _, p := range w18.storyQuestionCandidatesV13() {
			v18Story[fmt.Sprintf("%s/%d", p.oracleKind, p.oracleIndex)] = p
		}
		for _, p := range w19.storyQuestionCandidatesV13() {
			key := fmt.Sprintf("%s/%d", p.oracleKind, p.oracleIndex)
			old, ok := v18Story[key]
			if !ok || !reflect.DeepEqual(of(old), of(p)) {
				t.Fatalf("seed %d %s: v19 story answer differs from v18", seed, key)
			}
			if old.Case.Question == p.Case.Question {
				t.Fatalf("seed %d %s: question was not rephrased", seed, key)
			}
			required := map[string]bool{}
			for _, id := range p.RequiredPairIDs {
				required[id] = true
			}
			if got, ok := w19.resolveWithEvidence(p, required); !ok || got != p.Case.ExpectedAnswer {
				t.Fatalf("seed %d %s: oracle derives %q, case expects %q", seed, key, got, p.Case.ExpectedAnswer)
			}
			if err := w19.validatePlan(p); err != nil && !strings.Contains(err.Error(), "lexical") {
				t.Fatalf("seed %d %s: v19 plan fails validation: %v", seed, key, err)
			}
		}
		other := GenerateForVersion(seed^0x5eed, 3, protocol.BenchVersionV19)
		otherV18 := other
		otherV18.BenchVersion = protocol.BenchVersionV18
		p18, p19 := v19AbstentionPlans(w18, otherV18), v19AbstentionPlans(w19, other)
		for i := range p19 {
			if !reflect.DeepEqual(of(p18[i]), of(p19[i])) || !reflect.DeepEqual(p18[i].Case.GroundingTokens, p19[i].Case.GroundingTokens) {
				t.Fatalf("seed %d %s: v19 abstention answer differs from v18", seed, p19[i].oracleKind)
			}
		}
		for _, gen := range []func(int64, int, int) ([]V10GeneratedCase, error){
			func(s int64, n, v int) ([]V10GeneratedCase, error) { return GenerateV13ProgramsForVersion(s, n, v) },
			func(s int64, n, v int) ([]V10GeneratedCase, error) {
				return GenerateV13PersonalProgramsForVersion(s, n, v)
			},
		} {
			c18, err := gen(seed, 28, protocol.BenchVersionV18)
			if err != nil {
				t.Fatal(err)
			}
			c19, err := gen(seed, 28, protocol.BenchVersionV19)
			if err != nil {
				t.Fatal(err)
			}
			for i := range c19 {
				a, b := c18[i], c19[i]
				if a.Plan.Case.ID != b.Plan.Case.ID || !reflect.DeepEqual(of(a.Plan), of(b.Plan)) || !reflect.DeepEqual(a.Pairs, b.Pairs) ||
					!reflect.DeepEqual(a.Plan.Case.AnswerItemAcceptAny, b.Plan.Case.AnswerItemAcceptAny) || !reflect.DeepEqual(a.Provenance, b.Provenance) {
					t.Fatalf("seed %d program %d (%s): v19 changed more than the question", seed, i, b.Plan.phrase)
				}
				if a.Plan.Case.Question == b.Plan.Case.Question {
					t.Fatalf("seed %d program %d (%s): question was not rephrased", seed, i, b.Plan.phrase)
				}
			}
		}
	}
}

// v19SmallestFamilySurface is the fewest distinct renderings any family yields
// over 400 identities with fixed slot values: the composed surface the pool
// canary scores.
func v19SmallestFamilySurface() int {
	slots := v19SentinelSlots()
	smallest := -1
	for _, family := range V19PhraseFamilies() {
		distinct := map[string]bool{}
		for i := 0; i < 400; i++ {
			distinct[V19Phrase(family, int64(1+i%7), fmt.Sprintf("id-%d", i), slots)] = true
		}
		if smallest < 0 || len(distinct) < smallest {
			smallest = len(distinct)
		}
	}
	return smallest
}

// A short question that is mostly its entity's own record words trips the
// lexical-shortcut proof (contentOverlap), and the twin pickers then walk to
// another wording or entity. v19 wordings must trip it no more often than the
// v18 lists do, so the grammar never shifts which entity a seed asks about.
func TestV19WordingKeepsTheLexicalShortcutRate(t *testing.T) {
	rate := func(benchVersion int) float64 {
		trips, total := 0, 0
		for seed := int64(1); seed <= 20; seed++ {
			w := GenerateForVersion(seed, 3, benchVersion)
			var plans []QuestionPlan
			for i := range w.People {
				for k := 0; k < 4; k++ {
					plans = append(plans, w.contactCurrentSurface(i, i+k))
				}
			}
			for i := range w.Trips {
				for k := 0; k < 4; k++ {
					plans = append(plans, w.tripChangedLegCurrentSurface(i, i+k))
				}
			}
			for i := range w.Projects {
				for k := 0; k < 4; k++ {
					plans = append(plans, w.projectOutstandingSurface(i, i+k))
				}
			}
			plans = append(plans, w.storyQuestionCandidatesV13()...)
			for _, plan := range plans {
				total++
				if err := w.validatePlan(plan); errors.Is(err, errLexicalShortcut) {
					trips++
				}
			}
		}
		return float64(trips) / float64(total)
	}
	v18, v19 := rate(protocol.BenchVersionV18), rate(protocol.BenchVersionV19)
	t.Logf("lexical-shortcut trip rate: v18 %.2f%%, v19 %.2f%%", 100*v18, 100*v19)
	if v19 > v18+0.005 {
		t.Fatalf("v19 wordings trip the lexical-shortcut proof at %.2f%%, v18 at %.2f%%", 100*v19, 100*v18)
	}
}

// A family's forbidden terms bind the grammar's own words, never the slot
// values filled in verbatim: a step-actor question about the real step "filed
// the custody report" says "custody" because the record does. Every real
// custody step renders verbatim, and the question carries no forbidden term
// once that step is taken out.
func TestV19StepActorForbidsHoldingWordsOutsideTheStep(t *testing.T) {
	g := v19Grammars[V19PhraseProgramStepActor]
	for i, step := range v14CustodySteps {
		for seed := int64(1); seed <= 40; seed++ {
			q := V19Phrase(V19PhraseProgramStepActor, seed, fmt.Sprintf("step:%d", i), V19Slots{"subject": "the Harbor vault", "step": step})
			if !strings.Contains(q, step) {
				t.Fatalf("step %q not rendered verbatim: %q", step, q)
			}
			rest := strings.ReplaceAll(q, step, " ")
			for _, term := range g.forbid {
				if v19HasTerm(rest, term) {
					t.Fatalf("step %q: the grammar adds forbidden %q: %q", step, term, q)
				}
			}
		}
	}
}
