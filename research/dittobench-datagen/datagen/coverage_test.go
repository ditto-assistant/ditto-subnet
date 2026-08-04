package datagen

import (
	"math/rand"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestFullCatalogCoverage checks the property: every tool in the catalog is
// the correct answer (an expected tool) for at least one generated case. A large
// n makes the stratified draw hit every category.
func TestFullCatalogCoverage(t *testing.T) {
	// Generate at the latest contract (v5) so the coverage property holds over the
	// full catalog including the Code Mode tools (run_code / search_tools), which
	// are only reachable under v5's category set. The catalog is advertised to the
	// harness for every version, so every tool must be a correct answer somewhere.
	rotated, err := protocol.RotateSeedForVersion(2026, protocol.BenchVersionV5)
	if err != nil {
		t.Fatal(err)
	}
	r := rand.New(rand.NewSource(rotated))
	toolCases, _ := GenerateCasesWithFillersForVersion(r, 2026, 200, protocol.BenchVersionV5)
	reachable := map[string]bool{}
	for _, c := range toolCases {
		for _, ts := range c.ExpectedTools {
			reachable[ts.Name] = true
		}
	}
	var missing []string
	for _, tool := range catalog.Catalog() {
		if !reachable[tool.Name] {
			missing = append(missing, tool.Name)
		}
	}
	if len(missing) > 0 {
		t.Fatalf("catalog tools never a correct answer: %v", missing)
	}
}

func TestV8DestructiveToolsCannotMutateScoredWorldEvidence(t *testing.T) {
	seen := map[string]bool{}
	profiles := []struct {
		n, scale, worldCases int
	}{{6, 1, 0}, {48, 2, 57}, {100, 3, 190}}
	for _, profile := range profiles {
		for seed := int64(1); seed <= 100; seed++ {
			world := universe.Generate(seed, profile.scale)
			plans, err := world.QuestionPlans(profile.worldCases)
			if err != nil {
				t.Fatal(err)
			}
			protected := map[string]bool{}
			for _, plan := range plans {
				for _, pairID := range plan.SortedEvidenceIDs() {
					protected[pairID] = true
				}
			}

			rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV8)
			if err != nil {
				t.Fatal(err)
			}
			toolCases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, profile.n, protocol.BenchVersionV8)
			mutated := map[string]string{}
			for _, tc := range toolCases {
				if tc.Category != "world_memory_delete" && tc.Category != "world_memory_update" {
					continue
				}
				seen[tc.Category] = true
				for _, spec := range tc.ExpectedTools {
					pairID := spec.RequiredArgs["pair_id"]
					if pairID == "" {
						continue
					}
					if protected[pairID] {
						t.Fatalf("n=%d seed %d case %s mutates scored evidence pair %s", profile.n, seed, tc.ID, pairID)
					}
					if prior := mutated[pairID]; prior != "" {
						t.Fatalf("n=%d seed %d cases %s and %s mutate the same pair %s", profile.n, seed, prior, tc.ID, pairID)
					}
					mutated[pairID] = tc.ID
				}
			}
		}
	}
	for _, category := range []string{"world_memory_delete", "world_memory_update"} {
		if !seen[category] {
			t.Fatalf("did not exercise %s", category)
		}
	}
}

func TestV8CatalogUsesCurrentWorkflowSurface(t *testing.T) {
	reachable := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV8)
		if err != nil {
			t.Fatal(err)
		}
		r := rand.New(rand.NewSource(rotated))
		toolCases, _ := GenerateCasesWithFillersForVersion(r, seed, 100, protocol.BenchVersionV8)
		for _, c := range toolCases {
			for _, ts := range c.ExpectedTools {
				reachable[ts.Name] = true
			}
		}
	}
	for _, tool := range catalog.CatalogForVersion(protocol.BenchVersionV8) {
		if !reachable[tool.Name] {
			t.Errorf("v8 catalog tool %q is never a correct answer", tool.Name)
		}
	}
	for _, retired := range []string{"execute_agent_workflow", "get_agent_job_status", "create_automation", "list_automations", "create_recipe", "apply_recipe"} {
		if reachable[retired] {
			t.Errorf("v8 still generates retired tool %q", retired)
		}
		for _, tool := range catalog.CatalogForVersion(protocol.BenchVersionV8) {
			if tool.Name == retired {
				t.Errorf("v8 still advertises retired tool %q", retired)
			}
		}
	}
}
