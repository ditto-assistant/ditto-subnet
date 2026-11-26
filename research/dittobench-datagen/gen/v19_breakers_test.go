package gen

import (
	"fmt"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// The shortcut readers below are the rules released v18 agents encode
// (universe/v19_breakers.go). Each answers from the same records an oracle
// reader does; only the rule differs.

// v19LessonRE is LESSON_RE: a trigger word routes the question to the saying.
var v19LessonRE = regexp.MustCompile(`(?i)\b(lesson|teach|learn|moral|stuck with me|remember next time)`)

const (
	v19LettersPerTypoEdit = 3 // LETTERS_PER_TYPO_EDIT
	v19MaxTypoEdits       = 3 // MAX_TYPO_EDITS
)

// v19SameNameByTypoBudget is the typo budget: a word within its edit budget
// of a name is the same name.
func v19SameNameByTypoBudget(a, b string) bool {
	budget := min(v19MaxTypoEdits, (len(b)+v19LettersPerTypoEdit-1)/v19LettersPerTypoEdit)
	return v19Levenshtein(strings.ToLower(a), strings.ToLower(b)) <= budget
}

func v19Levenshtein(a, b string) int {
	prev := make([]int, len(b)+1)
	for j := range prev {
		prev[j] = j
	}
	for i := 1; i <= len(a); i++ {
		cur := make([]int, len(b)+1)
		cur[0] = i
		for j := 1; j <= len(b); j++ {
			cost := 1
			if a[i-1] == b[j-1] {
				cost = 0
			}
			cur[j] = min(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
		}
		prev = cur
	}
	return prev[len(b)]
}

// latest returns the value of the last fact by key that passes keep.
func v19Latest(facts []universe.V19BreakerFact, keep func(universe.V19BreakerFact) bool, by func(universe.V19BreakerFact) time.Time) (universe.V19BreakerFact, bool) {
	var best universe.V19BreakerFact
	found := false
	for _, f := range facts {
		if keep(f) && (!found || by(f).After(by(best))) {
			best, found = f, true
		}
	}
	return best, found
}

func v19Saved(f universe.V19BreakerFact) time.Time     { return f.Saved }
func v19Effective(f universe.V19BreakerFact) time.Time { return f.Effective }

// v19OracleAnswer is a general reader: it reads what each record says.
func v19OracleAnswer(c universe.V19BreakerCase) string {
	switch c.Family {
	case universe.V19BreakerTriggerFact:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "fact" && f.About == c.About }, v19Saved)
		return f.Value
	case universe.V19BreakerLateRecorded:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.About == c.About && !f.Effective.After(c.AsOf) }, v19Effective)
		return f.Value
	case universe.V19BreakerCompanyHolder:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "holder" }, v19Saved)
		return f.Value
	case universe.V19BreakerNearTwin:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.About == c.About }, v19Saved)
		return f.Value
	case universe.V19BreakerConditionalNext:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "next" }, v19Saved)
		return strings.Join([]string{f.Who, f.Task + " " + f.Condition, f.Channel}, "; ")
	}
	return ""
}

// v19ShortcutAnswer applies the family's v18 rule.
func v19ShortcutAnswer(c universe.V19BreakerCase) (string, string) {
	switch c.Family {
	case universe.V19BreakerTriggerFact:
		if !v19LessonRE.MatchString(c.Plan.Case.Question) {
			return v19OracleAnswer(c), "LESSON_RE did not fire"
		}
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "saying" }, v19Saved)
		return f.Value, "LESSON_RE"
	case universe.V19BreakerLateRecorded:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.About == c.About && !f.Saved.After(c.AsOf) }, v19Saved)
		return f.Value, "DATED_NOTE"
	case universe.V19BreakerCompanyHolder:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "holder" && !f.Company }, v19Saved)
		return f.Value, "HOLDER_CHECK"
	case universe.V19BreakerNearTwin:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return v19SameNameByTypoBudget(f.About, c.About) }, v19Saved)
		return f.Value, "typo budget"
	case universe.V19BreakerConditionalNext:
		f, _ := v19Latest(c.Facts, func(f universe.V19BreakerFact) bool { return f.Kind == "next" }, v19Saved)
		return strings.Join([]string{f.Who, f.Task, f.Channel}, "; "), "TASK_CHECK"
	}
	return "", ""
}

// v19Reply renders an answer the way an agent writes it: a date in prose.
func v19Reply(answer string) protocol.RunResponse {
	if at, err := time.Parse("2006-01-02", answer); err == nil {
		answer = fmt.Sprintf("%s %d, %d", at.Month(), at.Day(), at.Year())
	}
	return protocol.RunResponse{Answer: answer, FinalText: "From your notes: " + answer + "."}
}

func v19Breakers(t *testing.T, seed int64) []universe.V19BreakerCase {
	t.Helper()
	scale, _ := v8WorldProfile(V19FullEnvelope.Total())
	world := universe.GenerateForVersion(seed, scale, protocol.BenchVersionV19)
	cases, err := universe.GenerateV19Breakers(seed, world, V19FullEnvelope.Breakers)
	if err != nil {
		t.Fatalf("seed %d: %v", seed, err)
	}
	return cases
}

// TestV19ShortcutRulesLoseOnBreakerFamilies is the regression for DITTO-2737:
// on every breaker family a general reader scores 1.0 and the v18 shortcut
// rule scores lower, graded by the shipped claim grader.
func TestV19ShortcutRulesLoseOnBreakerFamilies(t *testing.T) {
	t.Parallel()
	oracle, shortcut, cases := map[string]float64{}, map[string]float64{}, map[string]int{}
	for seed := int64(1); seed <= 40; seed++ {
		for _, c := range v19Breakers(t, seed) {
			mc := c.Plan.Case
			mc.BenchVersion = protocol.BenchVersionV19
			honest := grade.Memory(mc, v19Reply(v19OracleAnswer(c)))
			if honest.Score != 1 {
				t.Fatalf("seed %d %s %q: oracle reply %q scored %.3f %v", seed, c.Family, mc.Question, v19OracleAnswer(c), honest.Score, honest.Notes)
			}
			answer, rule := v19ShortcutAnswer(c)
			if rule == "LESSON_RE did not fire" {
				t.Fatalf("seed %d: trigger-fact question %q carries no trigger word", seed, mc.Question)
			}
			if answer == v19OracleAnswer(c) {
				t.Fatalf("seed %d %s: %s gives the oracle answer %q", seed, c.Family, rule, answer)
			}
			oracle[c.Family] += honest.Score
			shortcut[c.Family] += grade.Memory(mc, v19Reply(answer)).Score
			cases[c.Family]++
		}
	}
	for _, family := range universe.V19BreakerFamilies {
		n := float64(cases[family])
		if cases[family] != 80 {
			t.Fatalf("%s: %d cases over 40 seeds, want 80", family, cases[family])
		}
		t.Logf("%-30s oracle %.3f shortcut %.3f (%d cases)", family, oracle[family]/n, shortcut[family]/n, cases[family])
		if shortcut[family]/n >= oracle[family]/n {
			t.Errorf("%s: shortcut %.3f does not score below the oracle %.3f", family, shortcut[family]/n, oracle[family]/n)
		}
	}
}

// The expected task of a conditional next step is "contains the expected
// phrase": the correct sentence passes in either clause order, and a reply
// that keeps the bare task alone does not.
func TestV19ConditionalNextGradesTheFullSentence(t *testing.T) {
	t.Parallel()
	oneWord := 0
	defer func() {
		if !t.Failed() && oneWord == 0 {
			t.Fatal("no swept case accepts a one-word task synonym with its condition")
		}
	}()
	for seed := int64(1); seed <= 20; seed++ {
		for _, c := range v19Breakers(t, seed) {
			if c.Family != universe.V19BreakerConditionalNext {
				continue
			}
			f := c.Facts[len(c.Facts)-1]
			mc := c.Plan.Case
			mc.BenchVersion = protocol.BenchVersionV19
			for _, reply := range []string{
				fmt.Sprintf("%s will %s %s, by %s.", f.Who, f.Task, f.Condition, f.Channel),
				fmt.Sprintf("%s: %s, %s %s.", f.Who, f.Channel, strings.ToUpper(f.Condition[:1])+f.Condition[1:], f.Task),
			} {
				if v := grade.Memory(mc, protocol.RunResponse{FinalText: reply}); v.Score != 1 {
					t.Fatalf("seed %d: correct sentence %q scored %.3f %v", seed, reply, v.Score, v.Notes)
				}
			}
			// Every accepted wording of the task, one-word synonyms included,
			// passes when it keeps the condition.
			for _, alt := range mc.AnswerItemAcceptAny[1] {
				if len(strings.Fields(strings.TrimSuffix(alt, " "+f.Condition))) == 1 {
					oneWord++
				}
				reply := fmt.Sprintf("%s will %s, by %s.", f.Who, alt, f.Channel)
				if v := grade.Memory(mc, protocol.RunResponse{FinalText: reply}); v.Score != 1 {
					t.Fatalf("seed %d: accepted wording %q scored %.3f %v", seed, reply, v.Score, v.Notes)
				}
			}
			bare := fmt.Sprintf("%s will %s, by %s.", f.Who, f.Task, f.Channel)
			if v := grade.Memory(mc, protocol.RunResponse{FinalText: bare}); v.Score != 0 {
				t.Fatalf("seed %d: reply without the condition %q scored %.3f", seed, bare, v.Score)
			}
		}
	}
}

// Breakers are deterministic per seed and every graded value is on a
// record the case requires.
func TestV19BreakersAreDeterministicAndGrounded(t *testing.T) {
	t.Parallel()
	for _, seed := range []int64{7, 123456789} {
		a, b := v19Breakers(t, seed), v19Breakers(t, seed)
		if fmt.Sprintf("%+v", a) != fmt.Sprintf("%+v", b) {
			t.Fatalf("seed %d: breakers differ between runs", seed)
		}
		for _, c := range a {
			required := map[string]bool{}
			for _, id := range c.Plan.RequiredPairIDs {
				required[id] = true
			}
			for _, f := range c.Facts {
				if !required[f.PairID] {
					t.Fatalf("seed %d %s: fact on %s is not a required record", seed, c.Family, f.PairID)
				}
			}
			if c.Plan.Case.BenchVersion != protocol.BenchVersionV19 || len(c.Plan.Case.Claims) == 0 {
				t.Fatalf("seed %d %s: case version %d with %d claims", seed, c.Family, c.Plan.Case.BenchVersion, len(c.Plan.Case.Claims))
			}
		}
	}
}

// Near twins stay 1-2 edits apart and never share a name with the world.
func TestV19NearTwinsAreNearButDistinct(t *testing.T) {
	t.Parallel()
	for seed := int64(1); seed <= 40; seed++ {
		scale, _ := v8WorldProfile(V19FullEnvelope.Total())
		world := universe.GenerateForVersion(seed, scale, protocol.BenchVersionV19)
		cases, err := universe.GenerateV19Breakers(seed, world, V19FullEnvelope.Breakers)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range cases {
			if c.Family != universe.V19BreakerNearTwin {
				continue
			}
			a, b := c.Facts[0].About, c.Facts[1].About
			if d := v19Levenshtein(strings.ToLower(a), strings.ToLower(b)); d < 1 || d > 2 {
				t.Fatalf("seed %d: twins %s/%s are %d edits apart", seed, a, b, d)
			}
			for _, p := range world.People {
				for _, name := range []string{a, b} {
					if strings.EqualFold(strings.Fields(p.Name)[0], name) || strings.EqualFold(p.Nickname, name) {
						t.Fatalf("seed %d: twin %s collides with world person %s (%s)", seed, name, p.Name, p.Nickname)
					}
				}
			}
		}
	}
}

// The v19 full table is 125 memory cases with two cases per breaker family,
// on v18's tool half.
func TestV19FullTableCarriesTheBreakers(t *testing.T) {
	t.Parallel()
	if V19FullEnvelope.Total() != 125 || V18FullEnvelope.Total() != 125 || V18FullEnvelope.Breakers != 0 {
		t.Fatalf("v19 table %d, v18 table %d with %d breakers", V19FullEnvelope.Total(), V18FullEnvelope.Total(), V18FullEnvelope.Breakers)
	}
	for _, seed := range []int64{1, 42, 123456789} {
		prof, _ := ProfileForVersion("full", protocol.BenchVersionV19)
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV19)
		if err != nil {
			t.Fatal(err)
		}
		hist := map[string]int{}
		for _, c := range a.MemoryCases {
			hist[c.QuestionType]++
			if c.BenchVersion != protocol.BenchVersionV19 {
				t.Fatalf("seed %d: case %s version %d", seed, c.ID, c.BenchVersion)
			}
		}
		for _, family := range universe.V19BreakerFamilies {
			if hist[family] != 2 {
				t.Fatalf("seed %d: %s asked %d times, want 2", seed, family, hist[family])
			}
		}
		if len(a.ToolCases) != 50 || len(a.MemoryCases) != 125 {
			t.Fatalf("seed %d: %d tool + %d memory cases", seed, len(a.ToolCases), len(a.MemoryCases))
		}
		for _, medium := range []string{"small", "medium"} {
			mprof, _ := ProfileForVersion(medium, protocol.BenchVersionV19)
			if env, _ := memoryEnvelopeForVersion(mprof.Mem, protocol.BenchVersionV19); env.Breakers != 0 {
				t.Fatalf("%s v19 table asks %d breakers", medium, env.Breakers)
			}
		}
	}
}

// V19 moves ten story slots (two arcs) into the breakers and changes nothing
// else in the v18 tables.
func TestV19EnvelopeMovesStorySlotsToBreakers(t *testing.T) {
	t.Parallel()
	full, ok := MemoryEnvelopeForRunSize("full", protocol.BenchVersionV19)
	if !ok || full != V19FullEnvelope || full.Story != full.StoryArcs*full.StoryPerArc {
		t.Fatalf("full v19 envelope %+v (ok %v)", full, ok)
	}
	want := V18FullEnvelope
	want.Story, want.StoryArcs, want.Breakers = 25, 5, 10
	if full != want {
		t.Fatalf("v19 table %+v, want the v18 table with story 25 (5 arcs) and 10 breakers", full)
	}
	for _, runSize := range []string{"small", "medium"} {
		v18, _ := MemoryEnvelopeForRunSize(runSize, protocol.BenchVersionV18)
		v19, _ := MemoryEnvelopeForRunSize(runSize, protocol.BenchVersionV19)
		if v18 != v19 {
			t.Fatalf("%s: v19 table %+v differs from v18 %+v", runSize, v19, v18)
		}
	}
}

func TestV19MixGate(t *testing.T) {
	if testing.Short() {
		t.Skip("mix gate sweep")
	}
	t.Parallel()
	gate := MixGateForVersion(protocol.BenchVersionV19)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV19)
	for seed := int64(1); seed <= 16; seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV19)
		if err != nil {
			t.Fatal(err)
		}
		audit, err := AuditMix(a)
		if err != nil {
			t.Fatal(err)
		}
		if v := audit.Summarize(gate).Violations; len(v) != 0 {
			t.Fatalf("seed %d violates the v19 mix gate: %v", seed, v)
		}
		for _, slot := range V13SlotOrder {
			if audit.Slots[slot] != V19FullEnvelope.slot(slot) {
				t.Fatalf("seed %d slot %s: %d, want %d", seed, slot, audit.Slots[slot], V19FullEnvelope.slot(slot))
			}
		}
	}
}
