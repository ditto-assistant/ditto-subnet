package textnoise

import (
	"math/rand"
	"strings"
	"testing"
)

func TestTokenNoiseNeverChangesWordEdges(t *testing.T) {
	const word = "remembered"
	for seed := int64(1); seed <= 1_000; seed++ {
		got, _ := mutateWord(word, rand.New(rand.NewSource(seed)))
		if got[0] != word[0] || got[len(got)-1] != word[len(word)-1] {
			t.Fatalf("seed %d changed semantic word edge: %q -> %q", seed, word, got)
		}
	}
}

func TestProjectIsDeterministicSeedVariedAndBounded(t *testing.T) {
	text := "I should have remembered the corrected business address before the project meeting, but apparently the current schedule changed again."
	one, stats := Project(text, 42, "personal:case", Options{MaxEdits: 3, Grammar: true})
	two, stats2 := Project(text, 42, "personal:case", Options{MaxEdits: 3, Grammar: true})
	if one != two || stats != stats2 {
		t.Fatalf("projection is not deterministic: %q %+v vs %q %+v", one, stats, two, stats2)
	}
	if one == text || stats.Total() == 0 || stats.Total() > 3 {
		t.Fatalf("projection=%q stats=%+v", one, stats)
	}
	varied, _ := Project(text, 43, "personal:case", Options{MaxEdits: 3, Grammar: true})
	if varied == one {
		t.Fatalf("adjacent seeds produced the same projection: %q", one)
	}
}

func TestProjectPreservesProtectedAndMachineLikeValues(t *testing.T) {
	text := "Send the corrected decision to moss.harbor61@mail.studio under REF-C09A771 for $12,500.00 and remember keep the client story and ledger aligned."
	protected := []string{"moss.harbor61@mail.studio", "REF-C09A771", "$12,500.00", "keep the client story and ledger aligned"}
	for seed := int64(1); seed <= 200; seed++ {
		got, _ := Project(text, seed, "business", Options{MaxEdits: 5, Grammar: true, Protected: protected})
		for _, want := range protected {
			if !strings.Contains(got, want) {
				t.Fatalf("seed %d altered protected value %q: %q", seed, want, got)
			}
		}
	}
}

func TestProjectionExercisesEveryErrorFamily(t *testing.T) {
	text := "I should have remembered the corrected business address because the apparently separate project schedule and personal relationship were relevant."
	var totals Stats
	for seed := int64(1); seed <= 600; seed++ {
		_, got := Project(text, seed, "mixed", Options{MaxEdits: 4, Grammar: true})
		totals.Keyboard += got.Keyboard
		totals.Transpose += got.Transpose
		totals.Omission += got.Omission
		totals.Duplication += got.Duplication
		totals.CommonSpelling += got.CommonSpelling
		totals.Grammar += got.Grammar
	}
	if totals.Keyboard == 0 || totals.Transpose == 0 || totals.Omission == 0 || totals.Duplication == 0 || totals.CommonSpelling == 0 || totals.Grammar == 0 {
		t.Fatalf("not all projection families were exercised: %+v", totals)
	}
}

func TestProjectionSurfaceSpaceOutgrowsLookupTables(t *testing.T) {
	inputs := []string{
		"My colleague remembered the corrected personal address after the neighborhood fundraiser and should have updated the current schedule.",
		"The business project decision was apparently separate from the original financial approval and its later correction.",
		"During the research trip, my friend described the relationship and the itinerary before the museum meeting.",
		"Please search the relevant memories, fetch the complete record, and then create the requested workflow.",
	}
	seen := map[string]bool{}
	for seed := int64(1); seed <= 2_000; seed++ {
		var projected []string
		for i, input := range inputs {
			got, _ := Project(input, seed, inputs[i], Options{MaxEdits: 3, Grammar: true})
			projected = append(projected, got)
		}
		seen[strings.Join(projected, "|")] = true
	}
	if len(seen) < 1_850 {
		t.Fatalf("only %d distinct four-domain surfaces from 2000 seeds", len(seen))
	}
}

func TestSelectIsExactOrderIndependentAndCoversDomain(t *testing.T) {
	ids := []string{"a", "b", "c", "d", "e", "f", "g"}
	selected := Select(9, "business", ids, 6_500)
	if len(selected) != 5 {
		t.Fatalf("selected %d ids, want ceil(7*0.65)=5", len(selected))
	}
	reversed := []string{"g", "f", "e", "d", "c", "b", "a"}
	other := Select(9, "business", reversed, 6_500)
	if len(other) != len(selected) {
		t.Fatalf("order changed quota: %d vs %d", len(selected), len(other))
	}
	for id := range selected {
		if !other[id] {
			t.Fatalf("order changed selected id %s", id)
		}
	}
}
