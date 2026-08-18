package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestParserDivergenceTrapGrades is the core proof of the C11 canary: for every
// parser-divergence case, (a) the surface/template value is a registered
// distractor, so a parser that emits it scores 0, and (b) the deterministic
// correct answer grades to 1. It also guards the two fairness invariants the
// trap depends on: the surface value must be present in the seeded memory (so a
// surface parser really can reach it) and the correct value must differ from it.
func TestParserDivergenceTrapGrades(t *testing.T) {
	for seed := int64(1); seed <= 30; seed++ {
		cases, pairs := buildParserDivergence(seed, 12)
		if len(cases) != 12 || len(pairs) != 12 {
			t.Fatalf("seed %d: got %d cases / %d pairs, want 12/12", seed, len(cases), len(pairs))
		}
		byPair := map[string]protocol.MemoryPair{}
		for _, p := range pairs {
			byPair[p.PairID] = p
		}
		for _, sc := range cases {
			mc := sc.Case
			if len(mc.DistractorAnswers) != 1 {
				t.Fatalf("seed %d case %s: want exactly one surface distractor, got %v", seed, mc.ID, mc.DistractorAnswers)
			}
			surface := mc.DistractorAnswers[0]

			// The surface value and the correct value are genuinely different.
			if grade.Normalize(surface) == grade.Normalize(mc.ExpectedAnswer) {
				t.Fatalf("seed %d case %s: surface value equals the correct answer %q", seed, mc.ID, surface)
			}

			// The surface value really is reachable from the seeded memory (the
			// pair carries both readings), so the trap is a genuine divergence and
			// not an unreachable straw distractor.
			if len(sc.RequiredPairIDs) != 1 {
				t.Fatalf("seed %d case %s: want one seeded pair, got %v", seed, mc.ID, sc.RequiredPairIDs)
			}
			body := byPair[sc.RequiredPairIDs[0]].Prompt
			if !grade.Hit(surface, body) && !grade.ContainedInAny(surface, []string{body}) && !containsMoneyOrNumber(mc, surface, body) {
				t.Fatalf("seed %d case %s: surface value %q not present in seeded memory %q", seed, mc.ID, surface, body)
			}

			// (a) A parser that emits the surface/template value scores 0.
			parser := grade.Memory(mc, protocol.RunResponse{Answer: surface, FinalText: "The answer is " + surface + "."})
			if parser.Score != 0 {
				t.Fatalf("seed %d case %s (%s): parser emitting surface %q scored %.2f, want 0 (%v)",
					seed, mc.ID, mc.QuestionType, surface, parser.Score, parser.Notes)
			}

			// (b) The deterministic correct answer grades to 1.
			correct := correctResponse(mc)
			honest := grade.Memory(mc, correct)
			if honest.Score != 1 {
				t.Fatalf("seed %d case %s (%s): correct answer %q scored %.2f, want 1 (%v)",
					seed, mc.ID, mc.QuestionType, mc.ExpectedAnswer, honest.Score, honest.Notes)
			}
		}
	}
}

// correctResponse builds the honest short-slot answer for a case in the surface
// form a genuine reader would emit (dollars for money, the value otherwise).
func correctResponse(mc protocol.MemoryCase) protocol.RunResponse {
	switch mc.AnswerKind {
	case protocol.AnswerMoney:
		cents := mustAtoi(mc.ExpectedAnswer)
		return protocol.RunResponse{Answer: "$" + itoa(cents/100)}
	default:
		return protocol.RunResponse{Answer: mc.ExpectedAnswer}
	}
}

// containsMoneyOrNumber checks the money/number surface value appears in the
// seeded prose in its human form ($dollars / bare digits), since the distractor
// is stored as minor units / a plain number that the prose does not print raw.
func containsMoneyOrNumber(mc protocol.MemoryCase, surface, body string) bool {
	switch mc.AnswerKind {
	case protocol.AnswerMoney:
		dollars := mustAtoi(surface) / 100
		return strings.Contains(body, "$"+itoa(dollars))
	case protocol.AnswerNumber:
		return strings.Contains(body, surface)
	default:
		return false
	}
}

// TestParserDivergenceIsWireOpaque proves nothing marks these cases as a canary
// on the surfaces the harness actually sees: the question text carries no
// divergence-type label, and the answer key (QuestionType / ExpectedAnswer /
// DistractorAnswers) is validator-internal and never part of RunRequest.
func TestParserDivergenceIsWireOpaque(t *testing.T) {
	cases, _ := buildParserDivergence(123456789, 12)
	banned := []string{"parser-divergence", "canary", "distractor", "surface", "template", "expected_answer", "divergence"}
	for _, sc := range cases {
		q := strings.ToLower(sc.Case.Question)
		for _, b := range banned {
			if strings.Contains(q, b) {
				t.Fatalf("case %s question leaks a canary marker %q: %s", sc.Case.ID, b, sc.Case.Question)
			}
		}
		// The only fields RunRequest carries are CaseID + Question (+ static
		// scaffolding). The case id is an opaque hash with no type prefix.
		if strings.Contains(sc.Case.ID, "divergence") || strings.Contains(sc.Case.ID, "parser") {
			t.Fatalf("case id is not opaque: %s", sc.Case.ID)
		}
	}
}

// TestParserDivergenceDeterministic proves the family regenerates identically
// from the same seed (a prerequisite for the pinned known-vector).
func TestParserDivergenceDeterministic(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789} {
		a, ap := buildParserDivergence(seed, 12)
		b, bp := buildParserDivergence(seed, 12)
		if len(a) != len(b) || len(ap) != len(bp) {
			t.Fatalf("seed %d: length mismatch", seed)
		}
		for i := range a {
			// MemoryCase contains slices, so compare through the load-bearing fields.
			if a[i].Case.Question != b[i].Case.Question ||
				a[i].Case.ExpectedAnswer != b[i].Case.ExpectedAnswer ||
				strings.Join(a[i].Case.DistractorAnswers, "|") != strings.Join(b[i].Case.DistractorAnswers, "|") {
				t.Fatalf("seed %d case %d not deterministic", seed, i)
			}
		}
		for i := range ap {
			if ap[i] != bp[i] {
				t.Fatalf("seed %d pair %d not deterministic", seed, i)
			}
		}
	}
}

func mustAtoi(s string) int {
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return n
		}
		n = n*10 + int(r-'0')
	}
	return n
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
