package gen

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

type quantityFixture struct {
	artifactFactRenderer
	checks int
	failAt int
}

func (f *quantityFixture) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	f.checks++
	if f.checks == f.failAt {
		return errors.New("semantic failure")
	}
	return nil
}

func TestQuantityFactSourceAuthority(t *testing.T) {
	conventions := map[string]bool{}
	units := map[string]bool{}
	for seed := int64(1); seed <= 12; seed++ {
		cases := BuildFamilyCompilerV13(seed, 16)
		for _, fc := range cases {
			r, err := quantityFactDocument(fc)
			if err != nil {
				t.Fatal(err)
			}
			conventions[fc.Convention] = true
			units[fc.Unit.Name] = true
			if r.Bindings["{{opening0}}"] != familyV2Amount(fc.Unit, fc.Opening) || r.Bindings["{{magnitude0}}"] != familyV2Amount(fc.Unit, fc.Magnitude) || r.Bindings["{{settled0}}"] != familyV2Amount(fc.Unit, fc.Settled) {
				t.Fatal("source operands changed")
			}
			before, _ := json.Marshal(r)
			fc.Correct = -123
			fc.Linked = -456
			fc.Pairs = nil
			fc.Staged = StagedCase{}
			after, err := quantityFactDocument(fc)
			if err != nil {
				t.Fatal(err)
			}
			raw, _ := json.Marshal(after)
			if string(before) != string(raw) {
				t.Fatal("compiler reads computed answers or rendered prose")
			}
			old := after
			if fc.Convention == ConventionAdds {
				fc.Convention = ConventionReduces
			} else {
				fc.Convention = ConventionAdds
			}
			cf, err := quantityFactDocument(fc)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(old.Bindings, cf.Bindings) || reflect.DeepEqual(old.Records[0].Assertions[1], cf.Records[0].Assertions[1]) {
				t.Fatal("counterfactual did not isolate convention")
			}
		}
	}
	if len(conventions) != 3 || len(units) != 9 {
		t.Fatalf("incomplete convention/unit coverage: %d/%d", len(conventions), len(units))
	}
}

func TestQuantityFactRenderingPreservesCompiler(t *testing.T) {
	cases := BuildFamilyCompilerV13(1, 16)
	f := &quantityFixture{}
	got, err := renderQuantityFacts(context.Background(), cases, f)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(cases) || f.checks != len(cases) {
		t.Fatal("unchecked cases")
	}
	for i := range got {
		if got[i].Pairs[0].Prompt == cases[i].Pairs[0].Prompt {
			t.Fatal("record not rendered")
		}
		got[i].Pairs[0].Prompt = cases[i].Pairs[0].Prompt
		if got[i].Staged.Case.Question == cases[i].Staged.Case.Question {
			t.Fatal("question not rendered")
		}
		got[i].Staged.Case.Question = cases[i].Staged.Case.Question
		if !reflect.DeepEqual(got[i], cases[i]) {
			t.Fatal("answers, provenance or evidence identity changed")
		}
	}
	snapshot, _ := json.Marshal(cases)
	if out, err := renderQuantityFacts(context.Background(), cases, &quantityFixture{failAt: 2}); err == nil || out != nil {
		t.Fatal("partially checked cases escaped")
	}
	after, _ := json.Marshal(cases)
	if string(snapshot) != string(after) {
		t.Fatal("source cases mutated")
	}
	cases[0].Convention = "unknown"
	bad := &quantityFixture{}
	if _, err := renderQuantityFacts(context.Background(), cases, bad); err == nil || bad.checks != 0 {
		t.Fatal("invalid source dispatched")
	}
}
