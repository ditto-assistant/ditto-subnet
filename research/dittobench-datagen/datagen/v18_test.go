package datagen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v18ToolMix counts the v18 tool families of one generated full half.
func v18ToolMix(t *testing.T, seed int64) map[string]int {
	t.Helper()
	ds, err := GenerateForVersion(seed, V18FullToolHalf, protocol.BenchVersionV18)
	if err != nil {
		t.Fatalf("seed %d: %v", seed, err)
	}
	cases := ds.ToolCases
	if len(cases) != V18FullToolHalf {
		t.Fatalf("seed %d: %d tool cases", seed, len(cases))
	}
	mix := map[string]int{}
	for _, tc := range cases {
		switch {
		case IsV13Restraint(tc.Category):
			mix["restraint"]++
		case v13DiscoveryFamily(tc.Category):
			mix["discovery"]++
		case v13IsUnexpectedFamily(tc.Category):
			mix["unexpected"]++
		case IsDecoyCorrect(tc.Category):
			mix["decoy"]++
		case IsV16ConnectorCase(tc.Category):
			mix[tc.Category]++
		case strings.HasPrefix(tc.Category, "world_"):
			mix["world"]++
		}
	}
	return mix
}

// The 50-case v18 half carries the full-run discovery and unexpected quotas,
// the v18 decoy quota and every v16 connector case on every swept seed, and
// the full-run restraint quota up to the structural floor (the world carrier,
// the state-dependent routes and the mutation follow-ups are never
// converted). Below v18 the quotas are the v16 ones (TestV16KnownVector and
// TestV17KnownVector pin the bytes).
func TestV18ToolQuotasHoldAcrossSeeds(t *testing.T) {
	seeds := 60
	if testing.Short() {
		seeds = 8
	}
	want := V16ConnectorQuotaFor(V18FullToolHalf)
	restraintTotal, restraintFull := 0, 0
	for seed := int64(1); seed <= int64(seeds); seed++ {
		mix := v18ToolMix(t, seed)
		if mix["discovery"] != 6 || mix["unexpected"] != 4 || mix["decoy"] != V18DecoyCases {
			t.Fatalf("seed %d: discovery %d unexpected %d decoy %d, want 6, 4, %d", seed, mix["discovery"], mix["unexpected"], mix["decoy"], V18DecoyCases)
		}
		if mix[V16CategoryLinkedRead] != want.LinkedRead || mix[V16CategoryLinkedAction] != want.LinkedAction ||
			mix[V16CategoryUnlinkedProbe] != want.Unlinked || mix[V16CategoryNamedProduct] != want.NamedProduct {
			t.Fatalf("seed %d: connector mix %v, want %+v", seed, mix, want)
		}
		if mix["restraint"] < 10 || mix["restraint"] > V13FullRestraintCaseCount {
			t.Fatalf("seed %d: %d restraint cases, want 10..%d", seed, mix["restraint"], V13FullRestraintCaseCount)
		}
		if mix["world"] < 1 {
			t.Fatalf("seed %d lost every world case", seed)
		}
		restraintTotal += mix["restraint"]
		if mix["restraint"] == V13FullRestraintCaseCount {
			restraintFull++
		}
	}
	// 2026-11-07 sweep of seeds 1..100: mean 15.08, full quota on 48, minimum 10.
	if mean := float64(restraintTotal) / float64(seeds); !testing.Short() && mean < 14.5 {
		t.Fatalf("mean restraint %.2f over %d seeds, want >= 14.5 (%d at the full quota)", mean, seeds, restraintFull)
	}
}

func TestV18QuotaFunctionsAreGated(t *testing.T) {
	if q := v13QuotaForVersion(50, protocol.BenchVersionV17); q != v13QuotaFor(50) {
		t.Fatalf("v17 quota moved: %+v", q)
	}
	if q := v13QuotaForVersion(50, protocol.BenchVersionV18); q.Discovery != 6 || q.Unexpected != 4 || q.Decoy != V18DecoyCases {
		t.Fatalf("v18 quota %+v", q)
	}
	if q := v13QuotaForVersion(24, protocol.BenchVersionV18); q != v13QuotaFor(24) {
		t.Fatalf("v18 medium quota moved: %+v", q)
	}
	if v13RestraintCaseCountForVersion(50, protocol.BenchVersionV17) != V13MediumRestraintCaseCount ||
		v13RestraintCaseCountForVersion(50, protocol.BenchVersionV18) != V13FullRestraintCaseCount ||
		v13RestraintCaseCountForVersion(24, protocol.BenchVersionV18) != v13RestraintCaseCount(24) {
		t.Fatal("restraint budget gating")
	}
	if toolCategoryWeightV18("entity_lookup_chain") != 8 || toolCategoryWeightV18("link_chain_result_usage") != 2 ||
		toolCategoryWeightV18("web_result_usage") != toolCategoryWeightV13("web_result_usage") {
		t.Fatal("v18 weights")
	}
}
