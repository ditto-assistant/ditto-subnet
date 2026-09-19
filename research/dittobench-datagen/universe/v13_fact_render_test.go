package universe

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
)

type factRenderFixture struct {
	plans, checks int
	reject        bool
}

func (f *factRenderFixture) Plan(_ context.Context, r V13FactRenderRequest) (V13FactRenderPlan, error) {
	f.plans++
	var p V13FactRenderPlan
	for i, tokens := range r.Required {
		p.Records[i] = strings.Join(tokens, " ")
	}
	p.Question = "Question about " + r.Subject
	return p, nil
}
func (f *factRenderFixture) Check(_ context.Context, r V13FactRenderRequest, p V13FactRenderPlan) error {
	f.checks++
	if f.reject {
		return errors.New("semantic rejection")
	}
	for _, s := range p.Records {
		if strings.Contains(s, "{{") {
			return errors.New("not bound")
		}
	}
	// Deliberate mutation must not change the caller's accepted output.
	for k := range r.Bindings {
		r.Bindings[k] = "mutated"
	}
	return nil
}

func TestV13FactRenderChecksEveryWorldAndPreservesTruth(t *testing.T) {
	ctx := context.Background()
	for seed := int64(1); seed <= 20; seed++ {
		f := &factRenderFixture{}
		got, err := GenerateV13RenderedFactPrograms(ctx, seed, 3, 28, f)
		if err != nil {
			t.Fatal(err)
		}
		want, _ := GenerateV13FactPrograms(seed, 3, 28)
		if f.plans != 14 || f.checks != 28 {
			t.Fatalf("plans=%d checks=%d", f.plans, f.checks)
		}
		for i, c := range got {
			if c.Plan.Case.ExpectedAnswer != want[i].Plan.Case.ExpectedAnswer || !reflect.DeepEqual(c.Plan.Case.Claims, want[i].Plan.Case.Claims) {
				t.Fatal("renderer changed answer authority")
			}
			for _, p := range c.Pairs {
				if strings.Contains(p.Prompt, "mutated") {
					t.Fatal("validator mutated output")
				}
			}
		}
		f = &factRenderFixture{}
		got, err = GenerateV13RenderedPersonalFactPrograms(ctx, seed, 3, 28, f)
		if err != nil {
			t.Fatal(err)
		}
		want, _ = GenerateV13PersonalFactPrograms(seed, 3, 28)
		if f.plans != 14 || f.checks != 28 {
			t.Fatal("personal worlds not independently checked")
		}
		for i, c := range got {
			if c.Plan.Case.ExpectedAnswer != want[i].Plan.Case.ExpectedAnswer || !reflect.DeepEqual(c.Plan.Case.Claims, want[i].Plan.Case.Claims) {
				t.Fatal("personal authority drift")
			}
		}
	}
}

func TestV13FactRenderRejectsUncheckedOrInvalidPlan(t *testing.T) {
	if _, err := GenerateV13RenderedFactPrograms(context.Background(), 1, 1, 4, nil); err == nil {
		t.Fatal("nil renderer accepted")
	}
	if _, err := GenerateV13RenderedPersonalFactPrograms(context.Background(), 1, 1, 4, nil); err == nil {
		t.Fatal("nil personal renderer accepted")
	}
	f := &factRenderFixture{reject: true}
	if _, err := GenerateV13RenderedFactPrograms(context.Background(), 1, 1, 4, f); err == nil {
		t.Fatal("semantic rejection ignored")
	}
	d := newV13Draws(1, "personal")
	g := d.drawPersonalGroup(0, V13PersonalSchool)
	p, err := buildV13PersonalFactPlan(1, 0, g, false)
	if err != nil {
		t.Fatal(err)
	}
	r, err := v13FactRenderRequest(p.World, v13Schema{}, false)
	if err != nil {
		t.Fatal(err)
	}
	valid, _ := (&factRenderFixture{}).Plan(context.Background(), r)
	for _, mutate := range []func(*V13FactRenderPlan){
		func(p *V13FactRenderPlan) { p.Records[0] = "omitted facts" },
		func(p *V13FactRenderPlan) { p.Records[0] += " {{unknown999}}" },
		func(p *V13FactRenderPlan) { p.Records[0] += " {{malformed}}" },
		func(p *V13FactRenderPlan) { p.Question += " {{value0}}" },
		func(p *V13FactRenderPlan) { p.Question = "missing subject" },
		func(p *V13FactRenderPlan) { p.Records[0] += " " + strings.Repeat(r.Required[0][0]+" ", 17) },
		func(p *V13FactRenderPlan) { p.Records[0] += " " + r.Required[1][len(r.Required[1])-1] },
	} {
		candidate := valid
		mutate(&candidate)
		if _, err := BindV13FactRenderPlan(r, candidate); err == nil {
			t.Fatal("invalid plan accepted")
		}
	}
}

func TestV13FactRenderCounterfactualRebindsOneRecord(t *testing.T) {
	for _, domain := range V13PersonalDomains {
		d := newV13Draws(93, "personal")
		g := d.drawPersonalGroup(0, domain)
		base, _ := buildV13PersonalFactPlan(93, 0, g, false)
		counter, _ := buildV13PersonalFactPlan(93, 0, g, true)
		a, _ := v13FactRenderRequest(base.World, v13Schema{}, false)
		b, _ := v13FactRenderRequest(counter.World, v13Schema{}, false)
		plan, _ := (&factRenderFixture{}).Plan(context.Background(), a)
		left, err := BindV13FactRenderPlan(a, plan)
		if err != nil {
			t.Fatal(err)
		}
		right, err := BindV13FactRenderPlan(b, plan)
		if err != nil {
			t.Fatal(err)
		}
		n := 0
		for i, s := range left.Records {
			if s != right.Records[i] {
				n++
			}
		}
		if n != 1 || left.Question != right.Question {
			t.Fatalf("%s counterfactual binding drift", domain)
		}
	}
}
