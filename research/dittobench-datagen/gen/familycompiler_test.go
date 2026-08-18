package gen

import (
	"fmt"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// dollarAnswer renders a cents value as the human dollar form a genuine reader
// emits ("$1234" for 123400 cents). All family-compiler amounts are whole
// dollars, so cents is always a multiple of 100.
func dollarAnswer(cents int) string { return "$" + strconv.Itoa(cents/100) }

// TestFamilyCompilerSurfaceClassifierScoresZero is the core proof for families 1
// and 2: a surface-only classifier that routes on the (generic, identical)
// question and applies ONE fixed recipe — (first-approved − paid) over the
// subject's own figures — scores 0 on every case whose record structure implies a
// different operation (its naive result is a registered distractor), while the
// correct record-derived answer grades 1. On the plain-subtract shape (the
// compiler's home recipe) the naive result IS the correct answer, so it scores 1
// there — which is exactly why the family breaks surface classification rather
// than a single recipe: no fixed recipe is right across the record shapes.
func TestFamilyCompilerSurfaceClassifierScoresZero(t *testing.T) {
	for seed := int64(1); seed <= 30; seed++ {
		cases := buildFamilyCompiler(seed, 24)
		if len(cases) == 0 {
			t.Fatalf("seed %d: no family-compiler cases", seed)
		}
		sawMismatch := false
		for _, fc := range cases {
			if fc.Family == familyCounterfactual {
				continue // covered by the cache test below
			}
			mc := fc.Staged.Case

			// (a) the correct record-derived answer grades to 1.
			honest := protocol.RunResponse{Answer: dollarAnswer(fc.CorrectCents)}
			if v := grade.Memory(mc, honest); v.Score != 1 {
				t.Fatalf("seed %d case %s (%s): correct answer %s scored %.2f, want 1 (%v)",
					seed, mc.ID, mc.QuestionType, dollarAnswer(fc.CorrectCents), v.Score, v.Notes)
			}

			// (b) the fixed-recipe compiler emits its naive (own approved − paid) value.
			naive := protocol.RunResponse{Answer: dollarAnswer(fc.NaiveSubtractCents), FinalText: "The balance is " + dollarAnswer(fc.NaiveSubtractCents) + "."}
			v := grade.Memory(mc, naive)
			if fc.NaiveSubtractCents == fc.CorrectCents {
				// The plain-subtract shape: the compiler's home recipe is correct here.
				if v.Score != 1 {
					t.Fatalf("seed %d case %s (%s): plain-shape naive==correct should score 1, got %.2f (%v)",
						seed, mc.ID, mc.QuestionType, v.Score, v.Notes)
				}
				continue
			}
			sawMismatch = true
			if v.Score != 0 {
				t.Fatalf("seed %d case %s (%s): surface classifier emitting naive %s scored %.2f, want 0 (naive must be a registered distractor) (%v)",
					seed, mc.ID, mc.QuestionType, dollarAnswer(fc.NaiveSubtractCents), v.Score, v.Notes)
			}
			// The naive value really is a registered distractor (the trap is real, not
			// an unreachable straw value).
			if !containsCents(mc.DistractorAnswers, fc.NaiveSubtractCents) {
				t.Fatalf("seed %d case %s (%s): naive value %d cents not registered as a distractor %v",
					seed, mc.ID, mc.QuestionType, fc.NaiveSubtractCents, mc.DistractorAnswers)
			}
		}
		if !sawMismatch {
			t.Fatalf("seed %d: no record-op/ambiguity case where the fixed recipe differs from the correct operation", seed)
		}
	}
}

// TestFamilyCompilerCounterfactualCacheScoresZero is the proof for family 3: a
// cache / pre-compiled-total classifier that reuses one member's answer for the
// surface-identical sibling scores 0 on the sibling (each member registers the
// other's answer as a distractor), while a reader that re-reads the one changed
// record fact answers both members correctly.
func TestFamilyCompilerCounterfactualCacheScoresZero(t *testing.T) {
	seen := 0
	for seed := int64(1); seed <= 30; seed++ {
		for _, fc := range buildFamilyCompiler(seed, 24) {
			if fc.Family != familyCounterfactual {
				continue
			}
			seen++
			mc := fc.Staged.Case

			// A reader that reads THIS member's record answers it correctly.
			honest := protocol.RunResponse{Answer: dollarAnswer(fc.CorrectCents)}
			if v := grade.Memory(mc, honest); v.Score != 1 {
				t.Fatalf("seed %d case %s: correct answer %s scored %.2f, want 1 (%v)",
					seed, mc.ID, dollarAnswer(fc.CorrectCents), v.Score, v.Notes)
			}

			// A cache that reuses the SIBLING's answer (the stale pre-computed total)
			// scores 0: the sibling's answer is a registered distractor here.
			stale := protocol.RunResponse{Answer: dollarAnswer(fc.LinkedCents), FinalText: "The balance is " + dollarAnswer(fc.LinkedCents) + "."}
			if v := grade.Memory(mc, stale); v.Score != 0 {
				t.Fatalf("seed %d case %s: cache emitting the sibling's answer %s scored %.2f, want 0 (%v)",
					seed, mc.ID, dollarAnswer(fc.LinkedCents), v.Score, v.Notes)
			}
			if fc.LinkedCents == fc.CorrectCents {
				t.Fatalf("seed %d case %s: counterfactual sibling answer equals this member's answer — the perturbation did not change the answer", seed, mc.ID)
			}
			if !containsCents(mc.DistractorAnswers, fc.LinkedCents) {
				t.Fatalf("seed %d case %s: sibling answer %d cents not registered as a distractor %v", seed, mc.ID, fc.LinkedCents, mc.DistractorAnswers)
			}
		}
	}
	if seen == 0 {
		t.Fatal("no counterfactual cases were generated at count 24")
	}
}

// TestFamilyCompilerWireOpaque proves nothing marks these cases on the surfaces
// the harness actually sees: the question carries no operation/shape cue and no
// canary marker, the wire case id is opaque, and the balance question template is
// identical across every record shape (so a surface classifier cannot tell them
// apart).
func TestFamilyCompilerWireOpaque(t *testing.T) {
	cases := buildFamilyCompiler(123456789, 24)
	banned := []string{
		"subtract", "adjust", "supersed", "latest", "larger", "recipe", "distractor",
		"expected_answer", "family", "compiler", "canary", "counterfactual", "forgiven twist",
		"record-balance",
	}
	tails := map[string]int{}
	for _, fc := range cases {
		q := strings.ToLower(fc.Staged.Case.Question)
		for _, b := range banned {
			if strings.Contains(q, b) {
				t.Fatalf("case %s question leaks a marker %q: %s", fc.Staged.Case.ID, b, fc.Staged.Case.Question)
			}
		}
		// The QuestionType must avoid the substrings that trigger special grader
		// excusing (injection refuse-and-answer, canary bait, cross-graph isolation).
		for _, special := range []string{"injection", "canary", "isolation"} {
			if strings.Contains(fc.Staged.Case.QuestionType, special) {
				t.Fatalf("case %s QuestionType %q contains grader-special substring %q", fc.Staged.Case.ID, fc.Staged.Case.QuestionType, special)
			}
		}
		// The wire case id is an opaque hash with no shape/family prefix.
		id := fc.Staged.Case.ID
		if strings.Contains(id, "family") || strings.Contains(id, "balance") || strings.Contains(id, "record") {
			t.Fatalf("case id is not opaque: %s", id)
		}
		// The question is the generic balance template regardless of record shape:
		// strip the subject and record the invariant tail.
		tail := fc.Staged.Case.Question
		if i := strings.Index(tail, "current balance owed on"); i >= 0 {
			tail = tail[i:]
		}
		// The part after the subject name ("'s account? Answer ...") is a fixed suffix.
		if j := strings.Index(tail, "'s account?"); j >= 0 {
			tails[tail[j:]]++
		}
	}
	if len(tails) != 1 {
		t.Fatalf("balance question template is not identical across shapes: %v", tails)
	}
}

// TestFamilyCompilerDeterministic proves the family regenerates identically from
// the same seed (a prerequisite for the pinned known-vector).
func TestFamilyCompilerDeterministic(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789} {
		a := buildFamilyCompiler(seed, 24)
		b := buildFamilyCompiler(seed, 24)
		if len(a) != len(b) {
			t.Fatalf("seed %d: length mismatch %d vs %d", seed, len(a), len(b))
		}
		for i := range a {
			ac, bc := a[i].Staged.Case, b[i].Staged.Case
			if ac.Question != bc.Question || ac.ExpectedAnswer != bc.ExpectedAnswer ||
				strings.Join(ac.DistractorAnswers, "|") != strings.Join(bc.DistractorAnswers, "|") ||
				a[i].CorrectCents != b[i].CorrectCents {
				t.Fatalf("seed %d case %d not deterministic", seed, i)
			}
			if len(a[i].Pairs) != len(b[i].Pairs) || a[i].Pairs[0] != b[i].Pairs[0] {
				t.Fatalf("seed %d pair %d not deterministic", seed, i)
			}
		}
	}
}

// TestFamilyCompilerAnswersPositiveAndDistractorFree guards the two hard
// invariants: every graded answer is a valid money value (>= 0) and no distractor
// ever collides with the correct answer.
func TestFamilyCompilerAnswersPositiveAndDistractorFree(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		for _, fc := range buildFamilyCompiler(seed, 24) {
			mc := fc.Staged.Case
			answer, err := strconv.Atoi(mc.ExpectedAnswer)
			if err != nil || answer < 0 || answer != fc.CorrectCents {
				t.Fatalf("seed %d case %s expected answer %q (CorrectCents=%d)", seed, mc.ID, mc.ExpectedAnswer, fc.CorrectCents)
			}
			for _, d := range mc.DistractorAnswers {
				dc, err := strconv.Atoi(d)
				if err != nil || dc <= 0 {
					t.Fatalf("seed %d case %s non-positive distractor %q", seed, mc.ID, d)
				}
				if dc == answer {
					t.Fatalf("seed %d case %s distractor %q collides with the answer", seed, mc.ID, d)
				}
			}
		}
	}
}

// TestFamilyCompiledTotalsAbsentFromSeededEvidence is the family-4 validation: it
// confirms the v12 answer-stuffing gate's PROVABLE path would fire on an
// A11-style compiled-total injection. Every correct total is DERIVED — it appears
// NOWHERE verbatim in the case's own seeded records (neither the minor-unit form
// the answer key stores nor the major-unit dollar form the compiler would inject),
// so each case satisfies memoryAnswerIsComputed via the exact
// gen.AnswerVerbatimInEvidence check the scorer uses. A harness that pre-computes
// the total and feeds it into the model input therefore feeds an
// absent-from-memory value, which the gate flags. The operands, by contrast, ARE
// present, so a genuine reader has everything it needs.
func TestFamilyCompiledTotalsAbsentFromSeededEvidence(t *testing.T) {
	for seed := int64(1); seed <= 25; seed++ {
		for _, fc := range buildFamilyCompiler(seed, 24) {
			mc := fc.Staged.Case
			var evidence strings.Builder
			for _, p := range fc.Pairs {
				evidence.WriteByte(' ')
				evidence.WriteString(p.Prompt)
				evidence.WriteByte(' ')
				evidence.WriteString(p.Response)
			}
			body := evidence.String()

			// The minor-unit answer key is not verbatim in the seeded evidence: this is
			// exactly memoryAnswerIsComputed's condition (the verbatim-recall exclusion).
			if AnswerVerbatimInEvidence(body, mc.ExpectedAnswer) {
				t.Fatalf("seed %d case %s: minor-unit total %q is verbatim in the seeded records (not a COMPUTED case): %s",
					seed, mc.ID, mc.ExpectedAnswer, body)
			}
			// The major-unit dollar form the compiler would inject is also absent (for a
			// non-zero balance; a $0 forgiven balance is a special case with no digits to
			// leak). This is the value that would show up in a model input under an
			// A11-style stuffing injection.
			if fc.CorrectCents > 0 {
				major := strconv.Itoa(fc.CorrectCents / 100)
				if AnswerVerbatimInEvidence(body, major) {
					t.Fatalf("seed %d case %s: major-unit total %q appears verbatim in the seeded records: %s",
						seed, mc.ID, major, body)
				}
			}
			// Sanity: the operands ARE in the records, so a genuine reader can derive the
			// answer (the case is fair, not a needle-absent decline).
			if !strings.Contains(body, "$") {
				t.Fatalf("seed %d case %s: seeded records carry no operands: %s", seed, mc.ID, body)
			}
		}
	}
}

// TestFamilyCompilerCarvedFromWorldBudget proves the anti-family-compiler family
// is carved out of the world-question budget: enabling it leaves the total v12
// memory-case count identical to v11's (the envelope is unchanged), and the
// family is present at the expected count.
func TestFamilyCompilerCarvedFromWorldBudget(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, ok := ProfileForVersion(runSize, protocol.BenchVersionV12)
		if !ok {
			t.Fatalf("%s: no v12 profile", runSize)
		}
		v11, err := GenerateDataset(123456789, prof, protocol.BenchVersionV11)
		if err != nil {
			t.Fatalf("%s v11: %v", runSize, err)
		}
		v12, err := GenerateDataset(123456789, prof, protocol.BenchVersionV12)
		if err != nil {
			t.Fatalf("%s v12: %v", runSize, err)
		}
		if len(v11.MemoryCases) != len(v12.MemoryCases) {
			t.Fatalf("%s: v12 memory-case count %d != v11 %d (envelope changed)", runSize, len(v12.MemoryCases), len(v11.MemoryCases))
		}
		want := v12FamilyCompilerCaseCount(prof.Mem)
		got := 0
		for _, c := range v12.MemoryCases {
			if strings.HasPrefix(c.QuestionType, "record-balance-") {
				got++
			}
		}
		if got != want {
			t.Fatalf("%s: found %d family-compiler cases, want %d", runSize, got, want)
		}
	}
}

func containsCents(distractors []string, cents int) bool {
	target := fmt.Sprintf("%d", cents)
	for _, d := range distractors {
		if d == target {
			return true
		}
	}
	return false
}
