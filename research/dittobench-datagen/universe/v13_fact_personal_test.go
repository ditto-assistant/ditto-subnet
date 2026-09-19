package universe

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13PersonalFactSeededTruthParity(t *testing.T) {
	for seed := int64(1); seed <= 100; seed++ {
		old, err := GenerateV13PersonalPrograms(seed, 28)
		if err != nil {
			t.Fatal(err)
		}
		for presentation := int64(1); presentation <= 6; presentation++ {
			got, err := GenerateV13PersonalFactPrograms(seed, presentation, 28)
			if err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			if len(got) != 28 {
				t.Fatal("incomplete personal coverage")
			}
			for i, c := range got {
				want := old[i].Plan.Case
				if c.Plan.Case.ExpectedAnswer != want.ExpectedAnswer || c.Plan.Case.AnswerKind != want.AnswerKind || !reflect.DeepEqual(c.Plan.Case.AnswerItems, want.AnswerItems) {
					t.Fatalf("seed %d case %d changed truth", seed, i)
				}
				if !reflect.DeepEqual(c.Provenance.Program, old[i].Provenance.Program) {
					t.Fatalf("seed %d case %d query drift: %#v != %#v", seed, i, c.Provenance.Program, old[i].Provenance.Program)
				}
				if !reflect.DeepEqual(c.Plan.Case.DistractorAnswers, want.DistractorAnswers) {
					t.Fatalf("case %d distractor drift", i)
				}
				if len(c.Plan.Case.Claims) != len(want.Claims) {
					t.Fatal("claim count drift")
				}
				for j, claim := range c.Plan.Case.Claims {
					wc := want.Claims[j]
					if claim.Expected != wc.Expected || claim.Kind != wc.Kind || claim.Unit != wc.Unit || claim.Weight != wc.Weight || claim.Critical != wc.Critical {
						t.Fatalf("case %d claim/unit drift: %#v != %#v", i, claim, wc)
					}
				}
				if len(c.Pairs) != len(old[i].Pairs) || len(c.Plan.RequiredPairIDs) != len(c.Pairs) {
					t.Fatal("evidence envelope drift")
				}
				for _, p := range c.Pairs {
					if strings.Contains(p.Prompt, "%!") || strings.Contains(p.Response, "%!") {
						t.Fatal("bad format")
					}
				}
			}
			repeat, err := GenerateV13PersonalFactPrograms(seed, presentation, 28)
			if err != nil || !reflect.DeepEqual(got, repeat) {
				t.Fatal("not reproducible")
			}
		}
	}
}

func TestV13PersonalFactMutationAndOrder(t *testing.T) {
	for seed := int64(1); seed <= 100; seed++ {
		d := newV13Draws(seed, "personal")
		for group, domain := range V13PersonalDomains {
			g := d.drawPersonalGroup(group, domain)
			base, err := buildV13PersonalFactPlan(seed, group, g, false)
			if err != nil {
				t.Fatal(err)
			}
			counter, err := buildV13PersonalFactPlan(seed, group, g, true)
			if err != nil {
				t.Fatal(err)
			}
			n := 0
			for i, f := range base.World.Facts {
				if !reflect.DeepEqual(f, counter.World.Facts[i]) {
					n++
				}
			}
			if n != 1 {
				t.Fatalf("%s mutation changed %d facts", domain, n)
			}
			bm, err := renderV13PersonalFactPlan(base, 19)
			if err != nil {
				t.Fatal(err)
			}
			cm, err := renderV13PersonalFactPlan(counter, 19)
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
				t.Fatalf("%s causal rendering drift", domain)
			}
			before, err := base.World.evaluate()
			if err != nil {
				t.Fatal(err)
			}
			for i, j := 0, len(base.World.Facts)-1; i < j; i, j = i+1, j-1 {
				base.World.Facts[i], base.World.Facts[j] = base.World.Facts[j], base.World.Facts[i]
			}
			base.World.Facts = append(base.World.Facts, base.Decoy)
			after, err := base.World.evaluate()
			if err != nil || !reflect.DeepEqual(before, after) {
				t.Fatalf("%s order or decoy changed truth: %v", domain, err)
			}
		}
	}
}

func TestV13FactSetRejectsInvalidEdits(t *testing.T) {
	fact := func(mode, value string, order int) v13Fact {
		return v13Fact{Entity: "trip", Field: "list", Value: factValue(value, protocol.ClaimKindSetMember), Mode: mode, Order: order, Record: 0}
	}
	valid := []v13Fact{fact("set_member", "coat", 0), fact("set_member", "hat", 1), fact("set_remove", "coat", 0), fact("set_add", "gloves", 1)}
	got, err := evaluateV13FactSet(valid)
	if err != nil || len(got) != 2 || got[0].Canonical != "hat" || got[1].Canonical != "gloves" {
		t.Fatal("wrong set result")
	}
	for _, change := range []func([]v13Fact){
		func(f []v13Fact) { f[2].Value = factValue("absent", protocol.ClaimKindSetMember) },
		func(f []v13Fact) { f[3].Value = factValue("hat", protocol.ClaimKindSetMember) },
		func(f []v13Fact) { f[1].Value = f[0].Value },
		func(f []v13Fact) { f[1].Order = 0 },
		func(f []v13Fact) { f[3].Order = 0 },
	} {
		f := append([]v13Fact(nil), valid...)
		change(f)
		if _, err := evaluateV13FactSet(f); err == nil {
			t.Fatal("invalid edit accepted")
		}
	}
}

func TestV13PersonalFactPresentationAndInput(t *testing.T) {
	for _, n := range []int{-1, 0, 1, 3, 5} {
		if _, err := GenerateV13PersonalFactPrograms(1, 1, n); err == nil {
			t.Fatal("bad count accepted")
		}
	}
	a, _ := GenerateV13PersonalFactPrograms(6, 1, 28)
	b, _ := GenerateV13PersonalFactPrograms(6, 2, 28)
	if reflect.DeepEqual(a, b) {
		t.Fatal("presentation entropy unused")
	}
	for i := range a {
		if a[i].Plan.Case.ExpectedAnswer != b[i].Plan.Case.ExpectedAnswer {
			t.Fatal("presentation changed truth")
		}
	}
}
