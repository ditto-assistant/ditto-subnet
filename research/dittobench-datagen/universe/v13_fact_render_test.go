package universe

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

type factRenderFixture struct {
	plans, checks int
	reject        bool
}

func TestV13RenderRequirementsFollowEvaluatorOperators(t *testing.T) {
	w := v13FactWorld{Entity: "project", Purpose: "data room", Query: []v13FactQuery{{"read", "next_action"}, {"latest", "responsible"}}}
	w.Facts = []v13Fact{
		{Entity: w.Entity, Field: "next_action", Value: factValue("book kickoff", "action"), Mode: "static", Record: 0},
		{Entity: w.Entity, Field: "responsible", Value: factValue("Ada", "person"), Mode: "history", Record: 0, Order: 0},
		{Entity: w.Entity, Field: "responsible", Value: factValue("Bea", "person"), Mode: "history", Record: 1, Order: 1},
		{Entity: w.Entity, Field: "neutral", Value: factValue("note", "text"), Mode: "static", Record: 2},
	}
	r, err := v13FactRenderRequest(w, v13Schema{Action: "next_action", Responsible: "responsible"}, true)
	if err != nil {
		t.Fatal(err)
	}
	if r.Revision != "v13-structured-fact-render-v3" || len(r.Query) != 2 {
		t.Fatal("tuple obligation lost")
	}
	for _, q := range r.Query {
		if !strings.Contains(q.Requirement, "VALUE") || r.Bindings[q.Field] == "" {
			t.Fatal("query value obligation missing")
		}
	}
	if !strings.Contains(r.Facts[1].Relation, "Initial state") || !strings.Contains(r.Facts[2].Relation, "Final update") {
		t.Fatal("history relies on record order")
	}
	if !strings.Contains(v13AssertionRelation("independent", 0), "SOLE launch approver") || !strings.Contains(v13QueryRequirement("conflict"), "not whether the people personally agree") {
		t.Fatal("source assignment confused with speaker claims")
	}
	for _, op := range []string{"read", "latest", "latest_date", "conflict", "set_after_update"} {
		if v13QueryRequirement(op) == "" {
			t.Fatalf("missing operator %s", op)
		}
	}
	if v13QueryRequirement("unknown") != "" {
		t.Fatal("unsupported operator accepted")
	}
	r.QuestionTemplate = "What is " + r.Query[0].Field + " for " + r.Subject + " and who is the final " + r.Query[1].Field + "?"
	p, err := (&factRenderFixture{}).Plan(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := BindV13FactRenderPlan(r, p); err != nil {
		t.Fatal(err)
	}
	bad := p
	bad.Question = "Who is the " + r.Query[1].Field + " responsible for the " + r.Query[0].Field + " for " + r.Subject + "?"
	if _, err := BindV13FactRenderPlan(r, bad); err == nil {
		t.Fatal("action obligation silently removed")
	}
	bad = p
	bad.Records[0] = strings.TrimPrefix(bad.Records[0], "Initial state: ")
	if _, err := BindV13FactRenderPlan(r, bad); err == nil {
		t.Fatal("implicit initial history accepted")
	}
	bad = p
	bad.Records[1] = strings.TrimPrefix(bad.Records[1], "Final update: ")
	if _, err := BindV13FactRenderPlan(r, bad); err == nil {
		t.Fatal("implicit final history accepted")
	}
	for _, marker := range []string{"replaces", "replaced", "replacing", "supersedes", "superseded", "superseding"} {
		valid := p
		valid.Records[1] = marker + " the original state: " + strings.TrimPrefix(p.Records[1], "Final update: ")
		if _, err := BindV13FactRenderPlan(r, valid); err != nil {
			t.Fatalf("explicit replacement %s rejected: %v", marker, err)
		}
	}
}

func TestV13DatedPlanCannotInferRecordChronology(t *testing.T) {
	w := v13FactWorld{Entity: "project", Purpose: "work", Query: []v13FactQuery{{Op: "read", Field: "role"}}}
	for i := 0; i < 3; i++ {
		w.Facts = append(w.Facts, v13Fact{Entity: w.Entity, Field: fmt.Sprintf("role%d", i), Value: factValue("value", "text"), Mode: "static", Record: i})
	}
	w.Query[0].Field = "role0"
	r, err := v13FactRenderRequest(w, v13Schema{}, false)
	if err != nil {
		t.Fatal(err)
	}
	p, err := (&factRenderFixture{}).Plan(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	r.Facts[0].Mode = "dated"
	for _, word := range []string{"Later", "earlier", "then", "subsequently", "afterward"} {
		bad := p
		bad.Records[0] = word + " " + bad.Records[0]
		if _, err := BindV13FactRenderPlan(r, bad); err == nil {
			t.Fatal("date chronology inferred from record order")
		}
	}
	if _, err := BindV13FactRenderPlan(r, p); err != nil {
		t.Fatal(err)
	}
	r.Facts[0].Mode = "history"
	p.Records[0] = "Initially " + p.Records[0]
	if _, err := BindV13FactRenderPlan(r, p); err != nil {
		t.Fatal("explicit history chronology rejected")
	}
}

func TestV13FactRenderRequiresOpaqueRolesNotDescriptiveFields(t *testing.T) {
	w := v13FactWorld{Entity: "project", Purpose: "data room", Query: []v13FactQuery{{Op: "read", Field: "opaqueowner"}}}
	for i, field := range []string{"opaqueowner", "planned milestone review date", "neutral note"} {
		w.Facts = append(w.Facts, v13Fact{Entity: w.Entity, Field: field, Value: factValue("value", "text"), Mode: "static", Record: i})
	}
	r, err := v13FactRenderRequest(w, v13Schema{Owner: "opaqueowner"}, true)
	if err != nil {
		t.Fatal(err)
	}
	if r.Bindings[r.SubjectEntity] != w.Entity || r.Bindings[r.Subject] != w.Purpose {
		t.Fatal("missing explicit entity/remit binding")
	}
	for i, a := range r.Facts {
		required := false
		for _, token := range r.Required[i] {
			required = required || token == a.Field
		}
		if required != (i == 0) {
			t.Fatal("opaque and descriptive field requirements conflated")
		}
	}
}

func (f *factRenderFixture) Plan(_ context.Context, r V13FactRenderRequest) (V13FactRenderPlan, error) {
	f.plans++
	var p V13FactRenderPlan
	for i, tokens := range r.Required {
		p.Records[i] = strings.Join(tokens, " ")
	}
	p.Question = "Question about " + r.Subject
	if r.QuestionTemplate != "" {
		p.Question = r.QuestionTemplate
	}
	for _, f := range r.Facts {
		if f.Mode == "history" && f.Sequence == 0 {
			p.Records[f.Record] = "Initial state: " + p.Records[f.Record]
		}
		if f.Mode == "history" && f.Sequence == 1 {
			p.Records[f.Record] = "Final update: " + p.Records[f.Record]
		}
	}
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
			if c.Plan.Case.Question != want[i].Plan.Case.Question {
				t.Fatal("renderer changed compiled query")
			}
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
			if c.Plan.Case.Question != want[i].Plan.Case.Question {
				t.Fatal("renderer changed personal compiled query")
			}
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
