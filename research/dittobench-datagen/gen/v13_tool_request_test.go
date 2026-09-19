package gen

import (
	"context"
	"reflect"
	"testing"
)

func TestToolFactRequestInformationBoundary(t *testing.T) {
	seen := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		rng, _ := NewRNGForVersion(seed, 13)
		prof, _ := ProfileForVersion("full", 13)
		tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
		out, err := renderToolRequests(context.Background(), tools, &quantityFixture{})
		if err != nil {
			t.Fatal(err)
		}
		for i, tc := range tools {
			r, ok, err := toolFactRequest(tc)
			if err != nil {
				t.Fatal(err)
			}
			if !ok {
				continue
			}
			seen[r.Records[0].Assertions[0].Kind] = true
			if out[i].Prompt == tc.Prompt {
				t.Fatal("old request retained")
			}
			out[i].Prompt = tc.Prompt
			if !reflect.DeepEqual(out[i], tc) {
				t.Fatal("grading or evidence changed")
			}
			tc.Prompt, tc.EffectAnswer, tc.ExpectedBehavior = "poison", "poison", "poison"
			tc.ExpectedTools, tc.PrerequisitePairs = nil, nil
			if tc.DecisionSource != nil {
				s := *tc.DecisionSource
				s.Values = map[string]string{}
				for k, v := range tc.DecisionSource.Values {
					if k != "project" {
						v = "poison"
					}
					s.Values[k] = v
				}
				// Opposite stored decisions must have identical request inputs.
				opposites := map[string]string{"effort_default": "effort_unsettled", "calendar_dated": "calendar_undated", "recipient_decided": "recipient_undecided", "route_one_off": "route_existing_workflow", "route_new_workflow": "route_existing_workflow", "route_calendar_absent": "route_calendar_exists", "route_email_planned": "route_email_requested"}
				if opposite, found := opposites[s.Kind]; found {
					s.Kind = opposite
				}
				tc.DecisionSource = &s
			}
			cf, _, err := toolFactRequest(tc)
			if err != nil || !reflect.DeepEqual(r, cf) {
				t.Fatal("request depends on hidden answer or decision state")
			}
		}
	}
	if len(seen) < 17 {
		t.Fatalf("request coverage %d, want at least 17", len(seen))
	}
}

func TestToolFactRequestAtomicFailure(t *testing.T) {
	rng, _ := NewRNGForVersion(1, 13)
	prof, _ := ProfileForVersion("full", 13)
	tools, _ := GenerateToolsForVersion(rng, 1, prof.Tools, 13)
	if out, err := renderToolRequests(context.Background(), tools, &quantityFixture{failAt: 2}); err == nil || out != nil {
		t.Fatal("partial requests escaped")
	}
}
