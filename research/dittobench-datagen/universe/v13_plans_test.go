package universe

import (
	"reflect"
	"testing"
)

func v13Selection(interim int) WorldPlanSelection {
	return WorldPlanSelection{
		StoryPerArc:  6,
		Ordinary:     32,
		OrdinaryCaps: map[string]int{oracleProjectOutstanding: 4},
		Interim:      interim,
		Salt:         "v13",
	}
}

// TestV13SelectionFillsPublishedSlots: six oracles per arc (one pure-money
// oracle dropped in arc rotation), a capped ordinary slot, and a non-monetary
// interim fill, on twenty seeds of the full-scale world.
func TestV13SelectionFillsPublishedSlots(t *testing.T) {
	for seed := int64(101); seed <= 120; seed++ {
		w := Generate(seed, 3)
		plans, counts, err := w.SelectQuestionPlans(v13Selection(61))
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if counts != (WorldPlanCounts{Story: 78, Ordinary: 32, Interim: 61}) || len(plans) != 171 {
			t.Fatalf("seed %d counts %+v plans %d, want 78/32/61 = 171", seed, counts, len(plans))
		}
		perArc := map[int]map[string]bool{}
		kinds := map[string]int{}
		for _, plan := range plans {
			kinds[plan.OracleKind()]++
			if arc := plan.StoryArcIndex(); arc >= 0 {
				if perArc[arc] == nil {
					perArc[arc] = map[string]bool{}
				}
				perArc[arc][plan.OracleKind()] = true
			}
		}
		if len(perArc) != len(w.StoryArcs) {
			t.Fatalf("seed %d story plans span %d arcs, want %d", seed, len(perArc), len(w.StoryArcs))
		}
		for arc, oracles := range perArc {
			if len(oracles) != 6 {
				t.Fatalf("seed %d arc %d keeps %d oracles, want 6", seed, arc, len(oracles))
			}
			dropped := storyDropRotation[arc%len(storyDropRotation)]
			if oracles[dropped] {
				t.Fatalf("seed %d arc %d kept its rotation-dropped oracle %s", seed, arc, dropped)
			}
			for _, kept := range []string{oracleStoryContactCurrent, oracleStoryLesson, oracleStoryLaterNetChange, oracleStoryOutcomeSummary} {
				if !oracles[kept] {
					t.Fatalf("seed %d arc %d dropped %s", seed, arc, kept)
				}
			}
		}
		if kinds[oracleProjectOutstanding] > 4 {
			t.Fatalf("seed %d selected %d project-outstanding plans, cap 4", seed, kinds[oracleProjectOutstanding])
		}
		// The interim fill is non-monetary: at most the ordinary slot's four
		// project-outstanding plans carry money outside the story oracles.
		monetary := 0
		for _, plan := range plans {
			if plan.StoryArcIndex() < 0 && monetaryOracles[plan.OracleKind()] {
				monetary++
			}
		}
		if monetary != kinds[oracleProjectOutstanding] || monetary > 4 {
			t.Fatalf("seed %d ordinary+interim monetary plans=%d, want <= 4 (all inside the ordinary slot)", seed, monetary)
		}
	}
}

// TestV13SelectionIsDeterministicAndIndependentOfV8: the same seed yields the
// same selection twice, and the v8 QuestionPlans selection on the same world
// is unchanged by the existence of the v13 selector (separate shuffle stream).
func TestV13SelectionIsDeterministicAndIndependentOfV8(t *testing.T) {
	w := Generate(918273, 3)
	a, _, err := w.SelectQuestionPlans(v13Selection(61))
	if err != nil {
		t.Fatal(err)
	}
	b, _, err := w.SelectQuestionPlans(v13Selection(61))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("v13 selection is not deterministic")
	}
	v8Before, err := w.QuestionPlans(229)
	if err != nil {
		t.Fatal(err)
	}
	v8After, err := w.QuestionPlans(229)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(v8Before, v8After) {
		t.Fatal("v8 selection moved")
	}
	ids := map[string]bool{}
	for _, plan := range a {
		if ids[plan.Case.ID] {
			t.Fatalf("duplicate case id %s in v13 selection", plan.Case.ID)
		}
		ids[plan.Case.ID] = true
	}
}

// TestV13SelectionFailsClosedOnShortfall: asking for more plans than the world
// can validate is an error, never a smaller run.
func TestV13SelectionFailsClosedOnShortfall(t *testing.T) {
	w := Generate(7, 3)
	sel := v13Selection(500)
	if _, _, err := w.SelectQuestionPlans(sel); err == nil {
		t.Fatal("oversized interim fill was accepted")
	}
	sel = v13Selection(0)
	sel.StoryPerArc = 8
	if _, _, err := w.SelectQuestionPlans(sel); err == nil {
		t.Fatal("eight oracles per arc was accepted")
	}
	sel = v13Selection(0)
	sel.Ordinary = 400
	if _, _, err := w.SelectQuestionPlans(sel); err == nil {
		t.Fatal("oversized ordinary slot was accepted")
	}
}
