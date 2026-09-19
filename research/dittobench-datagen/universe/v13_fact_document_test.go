package universe

import (
	"context"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func documentFixture() V13FactDocumentRequest {
	r := V13FactDocumentRequest{Revision: V13FactDocumentRevision, Domain: "story", Bindings: map[string]string{"{{entity0}}": "thread"}}
	for i := 0; i < 5; i++ {
		token := fmt.Sprintf("{{value%d}}", i)
		r.Bindings[token] = fmt.Sprintf("value%d", i)
		r.Records = append(r.Records, V13FactDocumentRecord{MinBytes: 1, MaxBytes: 200, Assertions: []V13DocumentAssertion{{Kind: "state", Relation: "independent record", Arguments: map[string]string{"entity": "{{entity0}}", "value": token}}}})
	}
	return r
}

func TestV13FactDocumentRoleAuthority(t *testing.T) {
	r := documentFixture()
	before, _ := V13FactRenderDigest(r)
	r.Records[0].Role = "request"
	if err := ValidateV13FactDocumentRequest(r); err != nil {
		t.Fatal(err)
	}
	after, _ := V13FactRenderDigest(r)
	if before == after {
		t.Fatal("role not bound to replay identity")
	}
	r.Records[0].Role = "answer"
	if ValidateV13FactDocumentRequest(r) == nil {
		t.Fatal("unknown role accepted")
	}
}

// Structural fixture only; never semantic qualification.
type documentRendererFixture struct {
	factRenderFixture
	plans, checks int
	reject        bool
}

func (f *documentRendererFixture) PlanDocument(_ context.Context, r V13FactDocumentRequest) (V13FactDocumentPlan, error) {
	f.plans++
	p := V13FactDocumentPlan{}
	for _, rec := range r.Records {
		tokens := []string{}
		for _, a := range rec.Assertions {
			for _, token := range a.Arguments {
				tokens = append(tokens, token)
			}
		}
		sort.Strings(tokens)
		p.Records = append(p.Records, strings.Join(tokens, " "))
	}
	// Mutating callback state must not change the authoritative request.
	r.Bindings["{{entity0}}"] = "corrupted"
	return p, nil
}
func (f *documentRendererFixture) CheckDocument(_ context.Context, r V13FactDocumentRequest, p V13FactDocumentPlan) error {
	f.checks++
	if f.reject {
		return fmt.Errorf("semantic rejection")
	}
	r.Bindings["{{entity0}}"] = "corrupted"
	p.Records[0] = "corrupted"
	return nil
}

func TestV13FactDocumentBoundariesAndChecks(t *testing.T) {
	r := documentFixture()
	f := &documentRendererFixture{}
	p, err := RenderV13FactDocument(context.Background(), r, f)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Records) != 5 || p.Records[0] != "thread value0" || r.Bindings["{{entity0}}"] != "thread" || f.plans != 1 || f.checks != 1 {
		t.Fatal("record authority or validation boundary changed")
	}
	if _, err := RenderV13FactDocument(context.Background(), r, &documentRendererFixture{reject: true}); err == nil {
		t.Fatal("semantic failure ignored")
	}
	if _, err := RenderV13FactDocument(context.Background(), r, nil); err == nil {
		t.Fatal("unchecked document accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := RenderV13FactDocument(ctx, r, f); err == nil || f.plans != 1 {
		t.Fatal("cancelled render dispatched")
	}
}

func TestV13FactDocumentRejectsInvalidBindings(t *testing.T) {
	for _, mode := range []string{"missing", "cross-record", "malformed", "count", "short", "long", "unknown", "bad-revision", "empty-assertion"} {
		t.Run(mode, func(t *testing.T) {
			r := documentFixture()
			p, _ := (&documentRendererFixture{}).PlanDocument(context.Background(), cloneV13DocumentRequest(r))
			switch mode {
			case "missing":
				p.Records[0] = "{{entity0}}"
			case "cross-record":
				p.Records[0] += " {{value1}}"
			case "malformed":
				p.Records[0] += " {{oops}}"
			case "count":
				p.Records = p.Records[:4]
			case "short":
				r.Records[0].MinBytes = 150
			case "long":
				r.Records[0].MaxBytes = 2
			case "unknown":
				delete(r.Bindings, "{{value0}}")
			case "bad-revision":
				r.Revision = "unknown"
			case "empty-assertion":
				r.Records[0].Assertions = nil
			}
			if _, err := BindV13FactDocument(r, p); err == nil {
				t.Fatal("invalid document accepted")
			}
		})
	}
}

func TestV13FactDocumentSourcePreflight(t *testing.T) {
	for _, mode := range []string{"revision", "no-records", "bounds", "missing", "nested-token", "unused", "empty-token", "blank-role", "blank-relation"} {
		t.Run(mode, func(t *testing.T) {
			r := documentFixture()
			switch mode {
			case "revision":
				r.Revision = "unknown"
			case "no-records":
				r.Records = nil
			case "bounds":
				r.Records[0].MaxBytes = 0
			case "missing":
				delete(r.Bindings, "{{value0}}")
			case "nested-token":
				r.Bindings["{{value0}}"] = "{{entity0}}"
			case "unused":
				r.Bindings["{{extra0}}"] = "unrelated"
			case "empty-token":
				r.Records[0].Assertions[0].Arguments["value"] = ""
			case "blank-role":
				r.Records[0].Assertions[0].Arguments[" "] = "{{value0}}"
			case "blank-relation":
				r.Records[0].Assertions[0].Relation = " "
			}
			f := &documentRendererFixture{}
			if _, err := RenderV13FactDocument(context.Background(), r, f); err == nil || f.plans != 0 || f.checks != 0 {
				t.Fatal("invalid source reached renderer")
			}
		})
	}
}

func TestV13FactDocumentInteriorEvidence(t *testing.T) {
	r := documentFixture()
	p, _ := (&documentRendererFixture{}).PlanDocument(context.Background(), cloneV13DocumentRequest(r))
	for i := range r.Records {
		r.Records[i].InteriorFacts = true
		r.Records[i].MaxBytes = 500
	}
	if _, err := BindV13FactDocument(r, p); err == nil {
		t.Fatal("edge evidence accepted")
	}
	for i := range p.Records {
		p.Records[i] = strings.Repeat("x ", 50) + p.Records[i] + strings.Repeat(" x", 50)
	}
	if _, err := BindV13FactDocument(r, p); err != nil {
		t.Fatal(err)
	}
	p.Records[0] = "{{value0}} " + p.Records[0]
	if _, err := BindV13FactDocument(r, p); err == nil {
		t.Fatal("early duplicate reveals evidence")
	}
}

func TestV13FactDocumentReplayAndCounterfactual(t *testing.T) {
	r := documentFixture()
	rec := &V13RecordingFactRenderer{Renderer: &documentRendererFixture{}}
	want, err := RenderV13FactDocument(context.Background(), r, rec)
	if err != nil {
		t.Fatal(err)
	}
	events := rec.Events()
	replay := NewV13ReplayFactRenderer(events)
	events[0].DocumentPlan.Records[0] = "corrupt"
	got, err := RenderV13FactDocument(context.Background(), r, replay)
	if err != nil || replay.Complete() != nil || !reflect.DeepEqual(got, want) {
		t.Fatal("replay authority lost")
	}
	for _, mode := range []string{"missing", "extra", "order", "request", "plan", "check", "wrong-type", "counterfactual"} {
		t.Run(mode, func(t *testing.T) {
			e := rec.Events()
			q := cloneV13DocumentRequest(r)
			switch mode {
			case "missing":
				e = e[:1]
			case "extra":
				e = append(e, e[0])
			case "order":
				e[0], e[1] = e[1], e[0]
			case "request":
				e[0].RequestSHA256 = "wrong"
			case "plan":
				e[0].DocumentPlan.Records[0] = "wrong"
			case "check":
				e[1].PlanSHA256 = "wrong"
			case "wrong-type":
				e[0].Plan = &V13FactRenderPlan{}
			case "counterfactual":
				q.Bindings["{{value0}}"] = "different"
			}
			rp := NewV13ReplayFactRenderer(e)
			_, err := RenderV13FactDocument(context.Background(), q, rp)
			if err == nil && rp.Complete() == nil {
				t.Fatal("altered transcript accepted")
			}
		})
	}
	p, _ := (&documentRendererFixture{}).PlanDocument(context.Background(), cloneV13DocumentRequest(r))
	r.Bindings["{{value2}}"] = "changed"
	cf, err := BindV13FactDocument(r, p)
	if err != nil {
		t.Fatal(err)
	}
	for i := range cf.Records {
		if (cf.Records[i] != want.Records[i]) != (i == 2) {
			t.Fatal("counterfactual changed unrelated record")
		}
	}
}
