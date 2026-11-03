package gen

import (
	"regexp"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Inspect the whole graph the scorer seeds, not the hidden prerequisite list
// of one case in isolation. Both public and noisy private-base surfaces must
// leave exactly one retrievable record for each scoped request.
// Public seeds run over all 40 qualification seeds; the salted private-base
// surface over v13SaltedSeeds.
func checkV13RestraintContextsSurviveSharedGraphAndRandomization(t *testing.T, public []DatasetArtifact) {
	refPattern := regexp.MustCompile(`\bc[0-9a-f]{16}\b`)
	asks, acts := 0, 0
	for _, artifacts := range [][]DatasetArtifact{public, v13SaltedSeeds(t)} {
		for _, a := range artifacts {
			seed, salt := a.Seed, a.SurfaceSalt
			graph := map[string]protocol.MemoryPair{}
			for _, wave := range a.MemoryWaves {
				if wave.UserID == "" || wave.UserID == PrimaryUser {
					for _, p := range wave.Pairs {
						graph[p.PairID] = p
					}
				}
			}
			for _, c := range a.ToolCases {
				for _, p := range c.PrerequisitePairs {
					graph[p.PairID] = p
				}
			}
			seen := map[string]bool{}
			for _, c := range a.ToolCases {
				if !datagen.IsV13Restraint(c.Category) || len(c.PrerequisitePairs) == 0 {
					continue
				}
				refs := refPattern.FindAllString(c.Prompt, -1)
				if len(refs) != 1 || seen[refs[0]] {
					t.Fatalf("seed %d salt %d: missing or shared visible context in %s", seed, salt, c.ID)
				}
				ref := refs[0]
				seen[ref] = true
				matches := 0
				for _, p := range graph {
					if strings.Contains(p.Prompt, ref) || strings.Contains(p.Response, ref) {
						matches++
						if p.PairID != c.PrerequisitePairs[0].PairID || !strings.Contains(p.Prompt, ref) || !strings.Contains(p.Response, ref) {
							t.Fatalf("seed %d: context selects wrong or incompletely scoped record", seed)
						}
					}
				}
				if matches != 1 {
					t.Fatalf("seed %d: request resolves %d records, want one", seed, matches)
				}
				if c.Restraint != nil {
					asks++
				} else {
					acts++
				}
			}
		}
	}
	if asks == 0 || acts == 0 {
		t.Fatal("test must cover both clarify and act contexts")
	}
}
