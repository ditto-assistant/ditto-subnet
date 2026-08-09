package longmemeval

import (
	"math"
	"math/rand"
	"reflect"
	"testing"
)

func TestAggregateEqualMacroMeanAndUncertainty(t *testing.T) {
	profile, selection := selectFixture(t)
	score, err := Aggregate(selection, balancedOutcomes(selection, profile.CasesPerCapability/2))
	if err != nil {
		t.Fatal(err)
	}
	if score.LongMemMean != 0.5 {
		t.Fatalf("mean=%v", score.LongMemMean)
	}
	if score.LongMemStdErr != 0.068041 {
		t.Fatalf("stderr=%v want 0.068041", score.LongMemStdErr)
	}
	if score.CaseCount != 60 || len(score.PerCapability) != 6 {
		t.Fatalf("cardinality=%d/%d", score.CaseCount, len(score.PerCapability))
	}
	for index, capability := range Capabilities() {
		item := score.PerCapability[index]
		if item.Capability != capability || item.Count != 10 || item.Correct != 5 || item.Mean != 0.5 {
			t.Fatalf("bad capability score: %#v", item)
		}
	}
}

func TestAggregateUsesEqualCapabilityWeights(t *testing.T) {
	_, selection := selectFixture(t)
	correctByCapability := map[Capability]int{
		CapabilityExtraction:            10,
		CapabilityMultiSessionReasoning: 8,
		CapabilityTemporalReasoning:     6,
		CapabilityKnowledgeUpdate:       4,
		CapabilityPreference:            2,
		CapabilityAbstention:            0,
	}
	seen := make(map[Capability]int)
	outcomes := make([]Outcome, 0, len(selection.Cases))
	for _, item := range selection.Cases {
		outcomes = append(outcomes, Outcome{
			QuestionID: item.QuestionID,
			Correct:    seen[item.Capability] < correctByCapability[item.Capability],
		})
		seen[item.Capability]++
	}
	score, err := Aggregate(selection, outcomes)
	if err != nil {
		t.Fatal(err)
	}
	if score.LongMemMean != 0.5 {
		t.Fatalf("macro mean=%v", score.LongMemMean)
	}
	wantMeans := []float64{1, 0.8, 0.6, 0.4, 0.2, 0}
	for index, want := range wantMeans {
		if score.PerCapability[index].Mean != want {
			t.Fatalf("capability %d mean=%v want=%v", index, score.PerCapability[index].Mean, want)
		}
	}
}

func TestAggregateIsOutcomeOrderIndependent(t *testing.T) {
	profile, selection := selectFixture(t)
	outcomes := balancedOutcomes(selection, profile.CasesPerCapability/2)
	want, err := Aggregate(selection, outcomes)
	if err != nil {
		t.Fatal(err)
	}
	rand.New(rand.NewSource(19)).Shuffle(len(outcomes), func(i, j int) {
		outcomes[i], outcomes[j] = outcomes[j], outcomes[i]
	})
	got, err := Aggregate(selection, outcomes)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("outcome order changed aggregate: got %#v want %#v", got, want)
	}
}

func TestAggregateZeroVarianceAtExtremes(t *testing.T) {
	profile, selection := selectFixture(t)
	for _, correct := range []int{0, profile.CasesPerCapability} {
		score, err := Aggregate(selection, balancedOutcomes(selection, correct))
		if err != nil {
			t.Fatal(err)
		}
		if score.LongMemStdErr != 0 {
			t.Fatalf("correct=%d stderr=%v", correct, score.LongMemStdErr)
		}
	}
}

func TestAggregateRejectsBadOutcomeCardinality(t *testing.T) {
	profile, selection := selectFixture(t)
	valid := balancedOutcomes(selection, profile.CasesPerCapability/2)
	tests := map[string][]Outcome{
		"missing":    valid[:len(valid)-1],
		"duplicate":  append(append([]Outcome(nil), valid...), valid[0]),
		"unexpected": append(append([]Outcome(nil), valid...), Outcome{QuestionID: "unexpected"}),
	}
	for name, outcomes := range tests {
		t.Run(name, func(t *testing.T) {
			if _, err := Aggregate(selection, outcomes); err == nil {
				t.Fatal("bad outcomes accepted")
			}
		})
	}
}

func TestAggregateRejectsMalformedSelection(t *testing.T) {
	profile, selection := selectFixture(t)
	outcomes := balancedOutcomes(selection, profile.CasesPerCapability/2)
	tests := map[string]func(*Selection){
		"empty":          func(s *Selection) { s.Cases = nil },
		"invalid digest": func(s *Selection) { s.CaseSetDigest = "bad" },
		"duplicate case": func(s *Selection) { s.Cases[1].QuestionID = s.Cases[0].QuestionID },
		"missing capability": func(s *Selection) {
			for index := range s.Cases {
				if s.Cases[index].Capability == CapabilityAbstention {
					s.Cases[index].Capability = CapabilityPreference
				}
			}
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := selection
			changed.Cases = append([]SelectedCase(nil), selection.Cases...)
			mutate(&changed)
			if _, err := Aggregate(changed, outcomes); err == nil {
				t.Fatal("malformed selection accepted")
			}
		})
	}
}

func TestRound6RejectsFloatingNoise(t *testing.T) {
	values := map[float64]float64{
		1.0 / 3.0:            0.333333,
		2.0 / 3.0:            0.666667,
		0.00000049:           0,
		0.00000051:           0.000001,
		math.Nextafter(1, 0): 1,
	}
	for input, want := range values {
		if got := round6(input); got != want {
			t.Fatalf("round6(%v)=%v want=%v", input, got, want)
		}
	}
}
