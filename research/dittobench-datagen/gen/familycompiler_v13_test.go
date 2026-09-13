package gen

import (
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// familyV2Answer renders the honest answer for a case: the standing figure in
// the requested unit (a decimal currency form for money, a bare count
// otherwise), with the direction word for direction cases.
func familyV2Answer(fc FamilyV2Case, convention string) protocol.RunResponse {
	figure := FamilyV2Effect(convention, fc.Opening, fc.Magnitude, fc.Settled)
	quantity := strconv.Itoa(figure)
	if fc.Unit.Monetary {
		quantity = fmt.Sprintf("%s%d.00", fc.Unit.Symbol, figure)
	}
	if fc.Shape != familyV2Direction {
		return protocol.RunResponse{Answer: quantity, FinalText: "The figure is " + quantity + "."}
	}
	direction := map[string]string{ConventionAdds: "increase", ConventionReduces: "decrease", ConventionNone: "unchanged"}[convention]
	return protocol.RunResponse{Answer: direction + "; " + quantity, FinalText: "It was a " + direction + "; the figure is " + quantity + "."}
}

// TestFamilyCompilerV13Composition: 16 cases per full seed — 10 money, 6
// non-monetary across nights/seats/hours/licences/percentage points — with six
// counterfactual pairs and six direction cases (four money singles plus one
// non-monetary pair); every record states its sign
// convention in so many words; unit claims name the requested unit.
func TestFamilyCompilerV13Composition(t *testing.T) {
	unitsSeen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		cases := BuildFamilyCompilerV13(seed, v13FamilyCompilerCaseCount(225))
		if len(cases) != 16 {
			t.Fatalf("seed %d: %d cases, want 16", seed, len(cases))
		}
		money, units, pairs, direction := 0, 0, 0, 0
		for i, fc := range cases {
			mc := fc.Staged.Case
			if fc.Unit.Monetary {
				money++
				if mc.AnswerKind != protocol.AnswerMoney && !(fc.Shape == familyV2Direction && mc.AnswerItemKinds[1] == protocol.AnswerMoney) {
					t.Fatalf("seed %d case %d: money case graded as %q", seed, i, mc.AnswerKind)
				}
			} else {
				units++
				unitsSeen[fc.Unit.Name] = true
				if mc.AnswerKind == protocol.AnswerMoney {
					t.Fatalf("seed %d case %d: unit case graded as money", seed, i)
				}
			}
			if fc.Shape == familyV2Direction {
				direction++
			}
			if mc.QuestionType == QTRecordQuantityCFVariant {
				pairs++
				if fc.Linked == 0 || cases[i-1].Linked != fc.Correct || cases[i-1].Correct != fc.Linked {
					t.Fatalf("seed %d case %d: counterfactual pair not cross-linked", seed, i)
				}
				if cases[i-1].Convention == fc.Convention || cases[i-1].Opening != fc.Opening || cases[i-1].Magnitude != fc.Magnitude || cases[i-1].Settled != fc.Settled {
					t.Fatalf("seed %d case %d: variant must change only the stated convention", seed, i)
				}
			}
			// The record states its own convention: one of the bank's rendered
			// forms for the applied convention appears verbatim.
			record := fc.Pairs[0].Prompt
			stated := false
			for _, form := range familyV2ConventionForms[fc.Convention] {
				if strings.Contains(record, fmt.Sprintf(form, fc.Cue.Phrase, fc.Unit.Noun)) {
					stated = true
				}
			}
			if !stated {
				t.Fatalf("seed %d case %d: record does not state its convention (%s): %s", seed, i, fc.Convention, record)
			}
			if !strings.Contains(record, fc.Cue.Phrase) {
				t.Fatalf("seed %d case %d: record lacks its cue %q", seed, i, fc.Cue.Phrase)
			}
			// Unit claims are graded in the requested unit.
			found := false
			for _, c := range mc.Claims {
				if c.Kind == protocol.ClaimKindQuantity {
					found = true
					if c.Unit != fc.Unit.Name || c.Expected != strconv.Itoa(fc.Correct) {
						t.Fatalf("seed %d case %d: quantity claim %+v does not match %d %s", seed, i, c, fc.Correct, fc.Unit.Name)
					}
				}
			}
			if !found || !strings.Contains(mc.Question, fc.Unit.Name) {
				t.Fatalf("seed %d case %d: no quantity claim or unit missing from the question: %s", seed, i, mc.Question)
			}
			for _, special := range []string{"injection", "canary", "isolation"} {
				if strings.Contains(mc.QuestionType, special) {
					t.Fatalf("question type %q carries grader-special substring", mc.QuestionType)
				}
			}
		}
		if money != 10 || units != 6 || pairs != 6 || direction != 6 {
			t.Fatalf("seed %d: money=%d units=%d pairs=%d direction=%d, want 10/6/6/6", seed, money, units, pairs, direction)
		}
	}
	for _, unit := range FamilyV2Units {
		if !unitsSeen[unit.Name] {
			t.Fatalf("unit %q never appeared across 40 seeds", unit.Name)
		}
	}
}

// TestFamilyCompilerV13OracleAndOtherRecipesGrade: the honest reader scores 1
// in the requested unit; every other stated convention's figure is a
// registered distractor that scores 0; the counterfactual sibling's figure is
// also a distractor; totals are never verbatim in the record.
func TestFamilyCompilerV13OracleAndOtherRecipesGrade(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for i, fc := range BuildFamilyCompilerV13(seed, 16) {
			mc := fc.Staged.Case
			if v := grade.Memory(mc, familyV2Answer(fc, fc.Convention)); v.Score != 1 {
				t.Fatalf("seed %d case %d (%s): oracle scored %.2f (%v) answer=%q", seed, i, mc.QuestionType, v.Score, v.Notes, familyV2Answer(fc, fc.Convention).Answer)
			}
			for _, other := range familyV2Conventions {
				if other == fc.Convention {
					continue
				}
				// A solver that applied a different convention but got the direction
				// word right for its own reading still scores 0: the figure is a
				// registered distractor.
				if v := grade.Memory(mc, familyV2Answer(fc, other)); v.Score != 0 {
					t.Fatalf("seed %d case %d: other-recipe (%s) answer scored %.2f (%v)", seed, i, other, v.Score, v.Notes)
				}
			}
			if fc.Linked != 0 {
				sibling := fc
				if v := grade.Memory(mc, protocol.RunResponse{Answer: familyV2Quantity(sibling.Unit, fc.Linked)}); v.Score != 0 {
					t.Fatalf("seed %d case %d: sibling's figure scored %.2f", seed, i, v.Score)
				}
			}
			body := fc.Pairs[0].Prompt + " " + fc.Pairs[0].Response
			if AnswerVerbatimInEvidence(body, strconv.Itoa(fc.Correct)) {
				t.Fatalf("seed %d case %d: derived figure %d appears verbatim in the record: %s", seed, i, fc.Correct, body)
			}
		}
	}
}

func familyV2Quantity(unit FamilyV2Unit, figure int) string {
	if unit.Monetary {
		return fmt.Sprintf("%s%d.00", unit.Symbol, figure)
	}
	return strconv.Itoa(figure)
}

// TestFamilyCompilerV13ThreeValuedDirectionVectors pins the direction grading:
// the direction word is the sign of the quantity effect; "unchanged" is a
// first-class value with its own accept cluster; a wrong direction with the
// right figure earns only the figure half.
func TestFamilyCompilerV13ThreeValuedDirectionVectors(t *testing.T) {
	seenNone := false
	for seed := int64(1); seed <= 40; seed++ {
		for i, fc := range BuildFamilyCompilerV13(seed, 16) {
			if fc.Shape != familyV2Direction {
				continue
			}
			mc := fc.Staged.Case
			quantity := familyV2Quantity(fc.Unit, fc.Correct)
			var wrongDirection string
			switch fc.Convention {
			case ConventionAdds:
				wrongDirection = "decrease"
			case ConventionReduces:
				wrongDirection = "increase"
			default:
				seenNone = true
				wrongDirection = "increase"
				for _, form := range FamilyV2DirectionAccept {
					if v := grade.Memory(mc, protocol.RunResponse{Answer: form + "; " + quantity}); v.Score != 1 {
						t.Fatalf("seed %d case %d: unchanged form %q scored %.2f (%v)", seed, i, form, v.Score, v.Notes)
					}
				}
			}
			if v := grade.Memory(mc, protocol.RunResponse{Answer: wrongDirection + "; " + quantity}); v.Score != 0.5 {
				t.Fatalf("seed %d case %d: wrong direction with right figure scored %.2f, want 0.5 (%v)", seed, i, v.Score, v.Notes)
			}
			if mc.Claims[0].Kind != protocol.ClaimKindDirection || mc.Claims[1].Kind != protocol.ClaimKindQuantity {
				t.Fatalf("seed %d case %d: direction claims %+v", seed, i, mc.Claims)
			}
		}
	}
	if !seenNone {
		t.Fatal("no unchanged-direction case generated across 40 seeds")
	}
}

// TestFamilyCompilerV13CueReadingSolverBelowHalf is the acceptance adversary.
// A cue-reading solver built from the renderer bank (FamilyV2Cues) finds the
// cue in the record and applies that cue's conventional bookkeeping direction
// without reading the stated convention; a fixed-recipe solver applies
// opening minus settled regardless. Both must score below 50% on the quantity
// slice over 40 seeds, and the cue reader must score above zero so the trap is
// a genuine divergence rather than an unreachable straw solver.
//
// The dataset has no surface pass yet (the private v13 pass is a separate
// change); the measurement here is on the generated bytes, which the pass
// only perturbs further.
func TestFamilyCompilerV13CueReadingSolverBelowHalf(t *testing.T) {
	cueTotal, cueScore := 0, 0.0
	fixedScore := 0.0
	for seed := int64(1); seed <= 40; seed++ {
		for _, fc := range BuildFamilyCompilerV13(seed, 16) {
			mc := fc.Staged.Case
			cueTotal++
			cue, ok := FamilyV2CueIn(fc.Pairs[0].Prompt)
			if !ok {
				t.Fatalf("seed %d: no bank cue in record %q", seed, fc.Pairs[0].Prompt)
			}
			cueScore += grade.Memory(mc, familyV2Answer(fc, cue.Conventional)).Score
			fixedScore += grade.Memory(mc, familyV2Answer(fc, ConventionNone)).Score
		}
	}
	cueRate := cueScore / float64(cueTotal)
	fixedRate := fixedScore / float64(cueTotal)
	t.Logf("cue-reading solver %.3f, fixed recipe %.3f over %d cases", cueRate, fixedRate, cueTotal)
	if cueRate >= 0.5 {
		t.Fatalf("cue-reading solver scored %.3f, want < 0.5", cueRate)
	}
	if cueRate == 0 {
		t.Fatal("cue-reading solver scored 0: the bank convention never applies, so the cue is a straw")
	}
	if fixedRate >= 0.5 {
		t.Fatalf("fixed-recipe solver scored %.3f, want < 0.5", fixedRate)
	}
}

// TestFamilyCompilerV13DeterministicAndOpaque: same seed, same bytes; ids are
// opaque; the question carries no operation or convention cue.
func TestFamilyCompilerV13DeterministicAndOpaque(t *testing.T) {
	for _, seed := range []int64{1, 42, 123456789} {
		a, b := BuildFamilyCompilerV13(seed, 16), BuildFamilyCompilerV13(seed, 16)
		if !reflect.DeepEqual(a, b) {
			t.Fatalf("seed %d: not deterministic", seed)
		}
		for _, fc := range a {
			q := strings.ToLower(fc.Staged.Case.Question)
			for _, banned := range []string{"adds", "reduces", "convention", "recipe", "subtract", "memo entry", "billed on top", "deducted"} {
				if strings.Contains(q, banned) {
					t.Fatalf("seed %d: question leaks %q: %s", seed, banned, fc.Staged.Case.Question)
				}
			}
			if strings.Contains(fc.Staged.Case.ID, "family") || strings.Contains(fc.Pairs[0].SessionID, "family") {
				t.Fatalf("seed %d: ids are not opaque", seed)
			}
		}
	}
	if got := len(BuildFamilyCompilerV13(3, 8)); got != 8 {
		t.Fatalf("medium prefix produced %d cases, want 8", got)
	}
	// A prefix that would split a counterfactual pair drops the orphaned base.
	if got := len(BuildFamilyCompilerV13(3, 5)); got != 4 {
		t.Fatalf("prefix of 5 produced %d cases, want 4 (no orphaned base member)", got)
	}
}
