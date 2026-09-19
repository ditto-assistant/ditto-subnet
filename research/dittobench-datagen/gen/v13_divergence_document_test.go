package gen

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestDivergenceFactRendering(t *testing.T) {
	seen := map[string]bool{}
	for seed := int64(1); seed <= 12; seed++ {
		cases, pairs, sources := buildParserDivergenceSources(seed, 12, protocol.BenchVersionV13)
		before, _ := json.Marshal(cases)
		f := &quantityFixture{}
		got, err := renderDivergenceFacts(context.Background(), cases, pairs, sources, f)
		if err != nil {
			t.Fatal(err)
		}
		if f.checks != len(pairs) {
			t.Fatal("unchecked evidence")
		}
		for i, s := range sources {
			seen[s.Kind] = true
			r, err := divergenceFactDocument(s)
			if err != nil {
				t.Fatal(err)
			}
			if r.Bindings["{{actual0}}"] != s.Actual || r.Bindings["{{other0}}"] != s.Other {
				t.Fatal("source values changed")
			}
			if got[i].Prompt == pairs[i].Prompt {
				t.Fatal("public prose retained")
			}
			got[i].Prompt = pairs[i].Prompt
			if !reflect.DeepEqual(got[i], pairs[i]) {
				t.Fatal("evidence identity changed")
			}
			// Counterfactuals swap truth without adding a draw or parsing prose.
			s.Actual, s.Other = s.Other, s.Actual
			cf, err := divergenceFactDocument(s)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(cf.Records, r.Records) || cf.Bindings["{{actual0}}"] != r.Bindings["{{other0}}"] {
				t.Fatal("counterfactual changed relation")
			}
		}
		after, _ := json.Marshal(cases)
		if string(before) != string(after) {
			t.Fatal("questions or grades mutated")
		}
	}
	if len(seen) != 4 {
		t.Fatal("missing divergence kinds")
	}
}

func TestDivergenceFactAtomicFailure(t *testing.T) {
	cases, pairs, sources := buildParserDivergenceSources(1, 4, protocol.BenchVersionV13)
	snapshot, _ := json.Marshal(pairs)
	if got, err := renderDivergenceFacts(context.Background(), cases, pairs, sources, &quantityFixture{failAt: 2}); err == nil || got != nil {
		t.Fatal("partial evidence escaped")
	}
	after, _ := json.Marshal(pairs)
	if string(snapshot) != string(after) {
		t.Fatal("input mutated")
	}
	sources[3].Kind = "unknown"
	f := &quantityFixture{}
	if _, err := renderDivergenceFacts(context.Background(), cases, pairs, sources, f); err == nil || f.checks != 0 {
		t.Fatal("invalid source dispatched")
	}
}
