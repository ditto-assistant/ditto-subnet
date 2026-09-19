package gen

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestToolDecisionFactRendering(t *testing.T) {
	seen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		prof, _ := ProfileForVersion("full", 13)
		rng, _ := NewRNGForVersion(seed, 13)
		tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
		f := &quantityFixture{}
		out, err := renderToolDecisionFacts(context.Background(), tools, f)
		if err != nil {
			t.Fatal(err)
		}
		count := 0
		for i, tc := range tools {
			if tc.DecisionSource == nil {
				continue
			}
			for source := tc.DecisionSource; source != nil; source = source.Previous {
				count++
				seen[source.Kind] = true
				j := -1
				for k, pair := range tc.PrerequisitePairs {
					if pair.PairID == source.PairID {
						j = k
					}
				}
				if j < 0 {
					t.Fatal("missing source identity")
				}
				if !strings.Contains(out[i].PrerequisitePairs[j].Prompt, source.Context) {
					t.Fatal("lost planning scope")
				}
				if out[i].PrerequisitePairs[j].Prompt == tc.PrerequisitePairs[j].Prompt {
					t.Fatal("old prose retained")
				}
				wire, _ := json.Marshal(tc)
				if strings.Contains(string(wire), "DecisionSource") || strings.Contains(string(wire), "effort_unsettled") || strings.Contains(string(wire), "recipient_decided") {
					t.Fatal("private source leaked")
				}
				out[i].PrerequisitePairs[j] = tc.PrerequisitePairs[j]
			}
			if !reflect.DeepEqual(out[i], tc) {
				t.Fatal("tool contract changed")
			}
		}
		if count == 0 || f.checks != count {
			t.Fatal("missing decision coverage")
		}
	}
	if len(seen) != 23 {
		t.Fatalf("covered %d decision kinds, want 23", len(seen))
	}
}

func TestToolDecisionFactAtomicFailure(t *testing.T) {
	rng, _ := NewRNGForVersion(1, 13)
	prof, _ := ProfileForVersion("full", 13)
	tools, _ := GenerateToolsForVersion(rng, 1, prof.Tools, 13)
	before, _ := json.Marshal(tools)
	out, err := renderToolDecisionFacts(context.Background(), tools, &quantityFixture{failAt: 2})
	if err == nil || out != nil {
		t.Fatal("partial tools escaped")
	}
	after, _ := json.Marshal(tools)
	if string(before) != string(after) {
		t.Fatal("source mutated")
	}
	for i := range tools {
		if tools[i].DecisionSource == nil {
			continue
		}
		tools[i].DecisionSource.PairID = "missing"
		f := &quantityFixture{}
		if _, err := renderToolDecisionFacts(context.Background(), tools, f); err == nil || f.checks != 0 {
			t.Fatal("missing evidence dispatched")
		}
		break
	}
}

func TestReplacedReadRetainsEvidenceNotOldRequest(t *testing.T) {
	prof, _ := ProfileForVersion("small", 13)
	rng, _ := NewRNGForVersion(15, 13)
	tools, _ := GenerateToolsForVersion(rng, 15, prof.Tools, 13)
	out, err := renderToolDecisionFacts(context.Background(), tools, artifactFactRenderer{})
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for i, tc := range tools {
		if tc.DecisionSource == nil || tc.DecisionSource.Previous == nil || tc.DecisionSource.Previous.Kind != "record_accountant" {
			continue
		}
		found = true
		if tc.RequestSource != nil {
			t.Fatal("replaced read retained the old accountant request")
		}
		r, ok, err := toolFactRequest(tc)
		if !ok || err != nil {
			t.Fatal("replacement read has no request authority")
		}
		for _, value := range r.Bindings {
			if value == "2024" || value == "Morgan Lee" {
				t.Fatal("old request leaked into replacement")
			}
		}
		old := tc.DecisionSource.Previous
		for _, pair := range out[i].PrerequisitePairs {
			if pair.PairID != old.PairID {
				continue
			}
			if strings.Contains(pair.Prompt, old.Values["phone"]) || !strings.Contains(pair.Response, old.Values["phone"]) {
				t.Fatal("contact escaped full-memory response boundary")
			}
		}
	}
	if !found {
		t.Fatal("retained accountant prerequisite fixture disappeared")
	}
}
