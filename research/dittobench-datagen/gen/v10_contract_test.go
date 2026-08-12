package gen

import (
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV10GenerationIsExplicitAndNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("v10 scaffold changed active version to %d", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV10) {
		t.Fatal("v10 deterministic generation is not supported")
	}
	want := map[string]Profile{
		"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
		"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
		"full":   {Tools: 100, Mem: 225, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
	}
	for runSize, expected := range want {
		got, ok := ProfileForVersion(runSize, protocol.BenchVersionV10)
		if !ok || got != expected {
			t.Errorf("v10 %s profile=(%+v,%v), want %+v", runSize, got, ok, expected)
		}
	}
}

func TestV10ArtifactBindsOpenProgramsAndMetamorphicGroups(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV10)
	artifact, err := GenerateDataset(123456789, prof, protocol.BenchVersionV10)
	if err != nil {
		t.Fatal(err)
	}
	var cases []ArtifactCase
	renderers := map[universe.V10Renderer]bool{}
	groups := map[string][]ArtifactCase{}
	pairIDs := map[string]bool{}
	for _, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			pairIDs[pair.PairID] = true
		}
	}
	for _, c := range artifact.MemoryCases {
		if c.V10Provenance == nil {
			continue
		}
		cases = append(cases, c)
		p := c.V10Provenance
		renderers[p.Renderer] = true
		groups[p.MetamorphicGroup] = append(groups[p.MetamorphicGroup], c)
		if queryDepth(p.Program) < 4 {
			t.Errorf("case %s query depth=%d, want >=4", c.ID, queryDepth(p.Program))
		}
		if len(p.Ontology) < 7 || len(p.EvidencePairIDs) < 5 || len(p.SchemaSHA256) != 64 {
			t.Errorf("case %s incomplete provenance: %+v", c.ID, p)
		}
		for _, id := range p.EvidencePairIDs {
			if !pairIDs[id] {
				t.Errorf("case %s evidence %s absent from seed waves", c.ID, id)
			}
		}
	}
	if len(cases) != 12 || len(renderers) != 4 || len(groups) != 3 {
		t.Fatalf("v10 coverage cases/renderers/groups=%d/%d/%d, want 12/4/3", len(cases), len(renderers), len(groups))
	}
	for group, members := range groups {
		if len(members) != 4 {
			t.Fatalf("group %s has %d members", group, len(members))
		}
		base := ""
		changed := ""
		relations := map[string]bool{}
		for _, member := range members {
			relation := member.V10Provenance.Relation
			relations[relation] = true
			if relation == "causal_counterfactual" {
				changed = member.ExpectedAnswer
			} else if base == "" {
				base = member.ExpectedAnswer
			} else if member.ExpectedAnswer != base {
				t.Errorf("group %s invariant changed %s -> %s", group, base, member.ExpectedAnswer)
			}
		}
		if len(relations) != 4 || base == "" || changed == "" || base == changed {
			t.Errorf("group %s does not contain three invariants and one causal change", group)
		}
	}
}

func TestV10SchemaVariesBySeedButRegeneratesExactly(t *testing.T) {
	a, err := universe.GenerateV10Programs(41, 4)
	if err != nil {
		t.Fatal(err)
	}
	b, err := universe.GenerateV10Programs(41, 4)
	if err != nil {
		t.Fatal(err)
	}
	c, err := universe.GenerateV10Programs(42, 4)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("same v10 seed did not regenerate identical programs")
	}
	if a[0].Provenance.SchemaSHA256 == c[0].Provenance.SchemaSHA256 {
		t.Fatal("distinct v10 seeds reused one schema")
	}
}

func queryDepth(node universe.V10QueryNode) int {
	depth := 1
	for _, child := range node.Children {
		if candidate := 1 + queryDepth(child); candidate > depth {
			depth = candidate
		}
	}
	return depth
}
