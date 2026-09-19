package universe

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13FactProgramsSeededTruthParity(t *testing.T) {
	for seed := int64(1); seed <= 100; seed++ {
		legacy, err := GenerateV13Programs(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		for presentation := int64(1); presentation <= 6; presentation++ {
			got, err := GenerateV13FactPrograms(seed, presentation, 28)
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != 28 {
				t.Fatal("missing family")
			}
			for i, c := range got {
				want := legacy[i].Plan.Case
				if c.Plan.Case.ExpectedAnswer != want.ExpectedAnswer || c.Plan.Case.AnswerKind != want.AnswerKind || !reflect.DeepEqual(c.Plan.Case.AnswerItems, want.AnswerItems) {
					t.Fatalf("seed %d presentation %d case %d changed truth: %q != %q", seed, presentation, i, c.Plan.Case.ExpectedAnswer, want.ExpectedAnswer)
				}
				if !reflect.DeepEqual(c.Provenance.Program, legacy[i].Provenance.Program) {
					t.Fatalf("case %d changed query program", i)
				}
				if !reflect.DeepEqual(c.Plan.Case.DistractorAnswers, want.DistractorAnswers) {
					t.Fatalf("case %d changed distractor semantics", i)
				}
				if len(c.Pairs) != 6 || len(c.Plan.RequiredPairIDs) != 6 {
					t.Fatal("missing evidence")
				}
				if len(c.Plan.Case.Claims) != len(want.Claims) {
					t.Fatal("changed claims")
				}
				for j, claim := range c.Plan.Case.Claims {
					if claim.Expected != want.Claims[j].Expected || claim.Kind != want.Claims[j].Kind || claim.Weight != want.Claims[j].Weight || claim.Critical != want.Claims[j].Critical {
						t.Fatal("changed claim semantics")
					}
				}
				for _, p := range c.Pairs {
					if strings.Contains(p.Prompt, "%!") || strings.Contains(p.Response, "%!") {
						t.Fatal("formatting artifact")
					}
				}
				if strings.Contains(c.Plan.Case.Question, "%!") {
					t.Fatal("question format artifact")
				}
			}
			again, err := GenerateV13FactPrograms(seed, presentation, 28)
			if err != nil || !reflect.DeepEqual(got, again) {
				t.Fatal("non-deterministic output")
			}
		}
	}
}

func TestV13FactWorldCausalMutationAndOrder(t *testing.T) {
	for seed := int64(1); seed <= 100; seed++ {
		d := newV13Draws(seed, "business")
		s := generateV13Schema(seed)
		for group := 0; group < 7; group++ {
			g := d.drawGroup(group)
			base, err := buildV13FactWorld(d, s, group, g, false)
			if err != nil {
				t.Fatal(err)
			}
			counter, err := buildV13FactWorld(d, s, group, g, true)
			if err != nil {
				t.Fatal(err)
			}
			n := 0
			for i, f := range base.Facts {
				if !reflect.DeepEqual(f, counter.Facts[i]) {
					n++
				}
			}
			if n != 1 {
				t.Fatal("mutation must change exactly one fact")
			}
			bm, err := renderV13FactMember(base, s, 77, "2030-01-01")
			if err != nil {
				t.Fatal(err)
			}
			cm, err := renderV13FactMember(counter, s, 77, "2030-01-01")
			if err != nil {
				t.Fatal(err)
			}
			n = 0
			for i, row := range bm.Records {
				if row != cm.Records[i] {
					n++
				}
			}
			if n != 1 || bm.Question != cm.Question || bm.Expected == cm.Expected {
				t.Fatal("causal rendering invariant failed")
			}
			before, err := base.evaluate()
			if err != nil {
				t.Fatal(err)
			}
			for i, j := 0, len(base.Facts)-1; i < j; i, j = i+1, j-1 {
				base.Facts[i], base.Facts[j] = base.Facts[j], base.Facts[i]
			}
			// Newer, conflicting facts about another entity cannot change truth.
			other := base.Facts[0]
			other.Entity = "unrelated"
			other.Order = 999
			other.Date = other.Date.AddDate(10, 0, 0)
			base.Facts = append(base.Facts, other)
			after, err := base.evaluate()
			if err != nil || !reflect.DeepEqual(before, after) {
				t.Fatalf("ordering or unrelated facts changed answer: %v", err)
			}
		}
	}
}

func TestV13FactWorldRejectsAmbiguity(t *testing.T) {
	f := v13Fact{Entity: "target", Field: "role", Value: factValue("Ada", protocol.ClaimKindPerson), Mode: "history", Record: 0, Order: 0}
	w := v13FactWorld{Entity: "target", Purpose: "shipping", Facts: []v13Fact{f, f}, Query: []v13FactQuery{{"latest", "role"}}}
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted tied history")
	}
	w.Facts = w.Facts[:1]
	if _, err := renderV13FactMember(w, v13Schema{}, 1, "2030-01-01"); err == nil {
		t.Fatal("render invented a missing final event")
	}
	w.Facts[0].Mode = "dated"
	w.Query[0].Op = "latest_date"
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted missing date")
	}
	w.Facts[0].Date = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	w.Facts = append(w.Facts, w.Facts[0])
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted tied dates")
	}
	w.Query[0].Field = "missing"
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted absent fact")
	}
}

func TestV13FactProgramsInputAndPresentation(t *testing.T) {
	for _, n := range []int{-1, 0, 1, 3, 5} {
		if _, err := GenerateV13FactPrograms(1, 1, n); err == nil {
			t.Fatal("invalid count accepted")
		}
	}
	a, _ := GenerateV13FactPrograms(4, 1, 28)
	b, _ := GenerateV13FactPrograms(4, 2, 28)
	if reflect.DeepEqual(a, b) {
		t.Fatal("presentation entropy unused")
	}
	for i := range a {
		if a[i].Plan.Case.ExpectedAnswer != b[i].Plan.Case.ExpectedAnswer {
			t.Fatal("render entropy changed truth")
		}
	}
}

func TestV13FactWorldRejectsUnboundAliasAndTypeDrift(t *testing.T) {
	w := v13FactWorld{Entity: "target", Purpose: "shipping", Query: []v13FactQuery{{"latest", "owner"}}, Facts: []v13Fact{
		{Entity: "target", Field: "owner", Value: factValue("Ada", protocol.ClaimKindPerson), Mode: "history", Order: 0, Record: 0},
		{Entity: "target", Field: "owner", Value: factValue("Bea", protocol.ClaimKindPerson), Mode: "history", Order: 1, Record: 1},
	}}
	w.Facts[1].Value.Surface = "Cy"
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted prose/answer drift")
	}
	w.Facts[1].Value.Surface = "Bea"
	w.Facts[1].Value.Kind = protocol.ClaimKindStatus
	if _, err := w.evaluate(); err == nil {
		t.Fatal("accepted field type drift")
	}
}
