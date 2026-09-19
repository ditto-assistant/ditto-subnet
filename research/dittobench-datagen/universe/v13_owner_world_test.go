package universe

import (
	"reflect"
	"strings"
	"testing"
)

func TestOwnerWorldChronologyAndIsolation(t *testing.T) {
	w := v13OwnerWorld{Entity: "target", Purpose: "shipping", Role: "owner", Correction: "revision",
		Events: []v13OwnerEvent{{"target", "Ada", 0}, {"other", "Decoy", 99}, {"target", "Bea", 1}}}
	if got, err := w.answer(); err != nil || got != "Bea" {
		t.Fatalf("got %q, %v", got, err)
	}
	w.Events[0], w.Events[2] = w.Events[2], w.Events[0]
	if got, err := w.answer(); err != nil || got != "Bea" {
		t.Fatalf("record order changed truth: %q %v", got, err)
	}
	w.Events = append(w.Events, v13OwnerEvent{"target", "Conflicting", 1})
	if _, err := w.answer(); err == nil {
		t.Fatal("accepted ambiguous chronology")
	}
	if _, _, err := renderV13OwnerWorld(w, 1); err == nil {
		t.Fatal("rendered invalid world")
	}
}

func TestOwnerWorldRejectsMissingAndUnsupportedEvidence(t *testing.T) {
	w := v13OwnerWorld{Entity: "target", Purpose: "shipping", Role: "owner", Correction: "revision"}
	if _, err := w.answer(); err == nil {
		t.Fatal("accepted missing evidence")
	}
	w.Events = []v13OwnerEvent{{"target", "Ada", 0}, {"target", "Bea", 1}, {"target", "Cy", 2}}
	if got, err := w.answer(); err != nil || got != "Cy" {
		t.Fatalf("bad state evaluator: %q %v", got, err)
	}
	if _, _, err := renderV13OwnerWorld(w, 1); err == nil {
		t.Fatal("silently omitted third assignment")
	}
}

func TestOwnerSliceMetamorphicAndRenderingEntropy(t *testing.T) {
	for seed := int64(1); seed <= 200; seed++ {
		legacyBefore, err := GenerateV13Programs(seed, 4)
		if err != nil {
			t.Fatal(err)
		}
		var expected string
		presentations := map[string]bool{}
		for renderSeed := int64(1); renderSeed <= 12; renderSeed++ {
			cases, err := GenerateV13OwnerSlice(seed, renderSeed)
			if err != nil {
				t.Fatalf("seed %d render %d: %v", seed, renderSeed, err)
			}
			if len(cases) != 4 {
				t.Fatal("incomplete slice")
			}
			if renderSeed == 1 {
				expected = cases[0].Plan.Case.ExpectedAnswer
			}
			for i, c := range cases {
				answer := c.Plan.Case.ExpectedAnswer
				if answer != legacyBefore[i].Plan.Case.ExpectedAnswer {
					t.Fatal("presentation refactor changed seeded world truth")
				}
				if (i < 3 && answer != expected) || (i == 3 && answer == expected) {
					t.Fatal("metamorphic answer mismatch")
				}
				if len(c.Pairs) != 6 || len(c.Plan.RequiredPairIDs) != 6 {
					t.Fatal("missing evidence")
				}
				if strings.Contains(c.Plan.Case.Question, answer) {
					t.Fatal("question leaks answer")
				}
				if len(c.Plan.Case.Claims) != 1 || c.Plan.Case.Claims[0].Expected != answer {
					t.Fatal("grader differs from world")
				}
				found := false
				for _, p := range c.Pairs {
					found = found || strings.Contains(p.Prompt, answer) || strings.Contains(p.Response, answer)
				}
				if !found {
					t.Fatal("answer not supported by evidence")
				}
			}
			presentations[cases[0].Plan.Case.Question] = true
			repeat, err := GenerateV13OwnerSlice(seed, renderSeed)
			if err != nil || !reflect.DeepEqual(cases, repeat) {
				t.Fatal("not reproducible")
			}
		}
		if len(presentations) < 2 {
			t.Fatal("render entropy unused")
		}
		legacyAfter, err := GenerateV13Programs(seed, 4)
		if err != nil || !reflect.DeepEqual(legacyBefore, legacyAfter) {
			t.Fatal("changed default generator")
		}
	}
}

func TestOwnerRenderingCounterfactualChangesOnlyUpdate(t *testing.T) {
	w := v13OwnerWorld{Entity: "target", Purpose: "shipping", Role: "owner", Correction: "revision",
		Events: []v13OwnerEvent{{"target", "Ada", 0}, {"target", "Bea", 1}}}
	for seed := int64(0); seed < 1000; seed++ {
		w.Events[1].Person = "Bea"
		base, q, err := renderV13OwnerWorld(w, seed)
		if err != nil {
			t.Fatal(err)
		}
		w.Events[1].Person = "Cy"
		counter, cq, err := renderV13OwnerWorld(w, seed)
		if err != nil {
			t.Fatal(err)
		}
		if q != cq || base[0] != counter[0] || base[1] == counter[1] {
			t.Fatal("mutation changed presentation or unrelated facts")
		}
		if strings.ReplaceAll(base[1], "Bea", "Cy") != counter[1] {
			t.Fatal("mutation changed more than selected fact")
		}
	}
}
