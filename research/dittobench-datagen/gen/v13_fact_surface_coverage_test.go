package gen

import (
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Source coverage is not semantic qualification. Missing categories fail this
// structural guard rather than counting as private due to world entropy alone.
func TestV13FactToolRequestCoverageInventory(t *testing.T) {
	missing := map[string]int{}
	covered := 0
	for seed := int64(1); seed <= 10; seed++ {
		prof, _ := ProfileForVersion("full", 13)
		rng, _ := NewRNGForVersion(seed, 13)
		tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
		for _, tc := range tools {
			_, ok, err := toolFactRequest(tc)
			if err != nil {
				t.Fatal(err)
			}
			if ok || tc.MutationSource != nil {
				covered++
				continue
			}
			if strings.Contains(tc.Category, "restraint") {
				t.Fatal("restraint request source missing")
			}
			missing[tc.Category]++
		}
	}
	keys := make([]string, 0, len(missing))
	for k := range missing {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	t.Logf("typed requests=%d; remaining categories=%d", covered, len(keys))
	for _, k := range keys {
		t.Logf("remaining %s: %d", k, missing[k])
	}
	if len(keys) != 0 {
		t.Fatal("tool request source coverage regressed")
	}
}

// Request coverage alone is insufficient: every prerequisite delivered to a
// tool case must also have a typed record owner, including tool-only evidence.
func TestV13FactToolPrerequisiteSourceCoverage(t *testing.T) {
	for _, size := range []string{"small", "medium", "full"} {
		for seed := int64(1); seed <= 40; seed++ {
			prof, _ := ProfileForVersion(size, 13)
			scale, _ := v8WorldProfile(prof.Mem)
			world := universe.GenerateForVersion(seed, scale, 13)
			owned := map[string]bool{}
			for _, pair := range world.Pairs {
				owned[pair.PairID] = true
			}
			rng, _ := NewRNGForVersion(seed, 13)
			tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
			for _, tc := range tools {
				for _, pair := range tc.PrerequisitePairs {
					if owned[pair.PairID] {
						continue
					}
					found := false
					for source := tc.DecisionSource; source != nil; source = source.Previous {
						if source.PairID == pair.PairID {
							found = true
						}
					}
					if found {
						continue
					}
					t.Fatalf("unowned prerequisite: profile=%s seed=%d category=%s pair=%s source=%+v", size, seed, tc.Category, pair.PairID, tc.DecisionSource)
				}
			}
		}
	}
}

func TestV13FactToolSourcesAllProfiles(t *testing.T) {
	for _, size := range []string{"small", "medium", "full"} {
		for seed := int64(1); seed <= 40; seed++ {
			prof, _ := ProfileForVersion(size, 13)
			rng, _ := NewRNGForVersion(seed, 13)
			tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, 13)
			for _, tc := range tools {
				if tc.MutationSource != nil {
					if _, err := mutationFactRequest(*tc.MutationSource); err != nil {
						t.Fatal(err)
					}
					continue
				}
				if _, ok, err := toolFactRequest(tc); !ok || err != nil {
					t.Fatalf("uncovered %s/%s: %v", size, tc.Category, err)
				}
			}
		}
	}
}
