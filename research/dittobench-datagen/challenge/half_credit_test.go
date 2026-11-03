package challenge

import (
	"fmt"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// halfCreditCase is a synthetic case with the shape SN-352 found behind the
// generator competition's 0.5 per-case ceiling (generator B in
// docs/v14-generator-credits.md). The question asks for two items about one
// person by full name. One item is recorded under the full name and the other
// under a nickname that only an identity record binds. Look-alike nicknames,
// one suffix away, each carry an item in the same frame, and those items are
// distractors. The values and wording here are written for this test.
var halfCreditCase = func() protocol.MemoryCase {
	mc := protocol.MemoryCase{
		BenchVersion:      GradingBenchVersion,
		QuestionType:      "two-item-nickname-list",
		Question:          "Which two bakeries does Imogen Vale love?",
		AnswerKind:        protocol.AnswerList,
		ExpectedAnswer:    "Crumbwell Loaves; Thistle Oven",
		AnswerItems:       []string{"Crumbwell Loaves", "Thistle Oven"},
		DistractorAnswers: []string{"Saltbarrow Bakehouse", "Pennycress Bread", "Larkspur Crust"},
	}
	return mc
}()

// TestHalfCreditCeilingIsListPartialCredit pins the SN-352 explanation. A
// two-item list is graded by the fraction of items present, so an answer that
// gives only the full-name item earns exactly 0.5. Asserting a look-alike's
// item zeroes the whole case, and only both correct items earn 1. These three
// outcomes are exactly the observed panel distribution: in round 25955 every
// solving agent scored 0.5 on every case of the column, one result scored 1,
// and the rest scored 0. So each case's mean was 0.5 x solvers / panel.
func TestHalfCreditCeilingIsListPartialCredit(t *testing.T) {
	grade1 := func(answer string) float64 {
		return grade.Memory(halfCreditCase, protocol.RunResponse{Answer: answer, FinalText: answer}).Score
	}
	for answer, want := range map[string]float64{
		"Crumbwell Loaves and Thistle Oven":   1,
		"Crumbwell Loaves":                    0.5,
		"Crumbwell Loaves and Larkspur Crust": 0,
		"Larkspur Crust":                      0,
	} {
		if got := grade1(answer); got != want {
			t.Errorf("%q scored %.2f, want %.2f", answer, got, want)
		}
	}
}

// TestHalfCreditCasesCountAsSolvedByTheGenerator records the generator-
// scoring consequence. With solve_threshold 0.5, a case on which every panel
// agent earns only the easy half is "trivial", and one where two owners earn
// it is "discriminating", although no agent answered the hard half. The
// generator's credit therefore measures who avoided a look-alike's item, not
// who resolved the nickname. A family ported into v14 must be solvable at full
// credit (internal/familygate holds families whose solves sit at the
// threshold).
func TestHalfCreditCasesCountAsSolvedByTheGenerator(t *testing.T) {
	member := func(id string, score float64) PanelMember {
		return PanelMember{AgentID: id, OwnerKey: "owner-" + id, Results: []PanelCaseResult{{CaseID: "half", Score: score}}}
	}
	refs := []CaseRef{{CaseID: "half", Fingerprint: "fp-half"}}
	params := DefaultScoreParams()
	for name, tc := range map[string]struct {
		scores []float64
		status string
	}{
		"everyone earns the easy half":  {[]float64{0.5, 0.5, 0.5, 0.5, 0.5}, CaseTrivial},
		"two owners earn the easy half": {[]float64{0.5, 0.5, 0, 0, 0}, CaseDiscriminating},
		"one full answer":               {[]float64{1, 0, 0, 0, 0}, CaseUnsolved},
	} {
		var panel []PanelMember
		for i, s := range tc.scores {
			panel = append(panel, member(fmt.Sprintf("a%d", i), s))
		}
		ps, err := ScoreCases(refs, ScoreInput{GeneratorOwnerKey: "gen", Panel: panel}, params)
		if err != nil {
			t.Fatal(err)
		}
		if got := ps.Cases[0].Status; got != tc.status {
			t.Errorf("%s: status %s, want %s", name, got, tc.status)
		}
	}
}
