package gen

import (
	"context"
	"reflect"
	"testing"
)

func TestMutationFactRequests(t *testing.T) {
	seen := map[string]bool{}
	for seed := int64(1); seed <= 20; seed++ {
		rng, _ := NewRNGForVersion(seed, 13)
		prof, _ := ProfileForVersion("full", 13)
		tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
		out, err := renderMutationRequests(context.Background(), tools, &quantityFixture{})
		if err != nil {
			t.Fatal(err)
		}
		for i, tc := range tools {
			if tc.MutationSource == nil {
				continue
			}
			s := *tc.MutationSource
			seen[s.Kind] = true
			if s.Reviewer != "" {
				seen["reviewer"] = true
			}
			r, err := mutationFactRequest(s)
			if err != nil {
				t.Fatal(err)
			}
			if r.Records[0].Role != "request" {
				t.Fatal("not a request")
			}
			if out[i].Prompt == tc.Prompt {
				t.Fatal("old request retained")
			}
			out[i].Prompt = tc.Prompt
			if !reflect.DeepEqual(out[i], tc) {
				t.Fatal("mutation contract changed")
			}
			// Answers and opaque internal targets are not request inputs.
			s.Email, s.PreviousEmail, s.TargetPairID = "poison", "poison", "poison"
			cf, err := mutationFactRequest(s)
			if err != nil || !reflect.DeepEqual(r, cf) {
				t.Fatal("hidden target or answer leaked into request")
			}
		}
		if out, err := renderMutationRequests(context.Background(), tools, &quantityFixture{failAt: 2}); err == nil || out != nil {
			t.Fatal("partial mutation requests escaped")
		}
	}
	if len(seen) != 3 {
		t.Fatalf("missing mutation state coverage: %v", seen)
	}
}
