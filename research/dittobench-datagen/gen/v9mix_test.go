package gen

import (
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV9GenerationIsExplicitButNotActivated(t *testing.T) {
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("CurrentBenchVersion=%d, want non-activated v8", protocol.CurrentBenchVersion)
	}
	if !protocol.SupportedBenchVersion(protocol.BenchVersionV9) {
		t.Fatal("v9 explicit generation is not supported")
	}
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV9)
	if !ok {
		t.Fatal("v9 full profile unavailable")
	}
	artifact, err := GenerateDataset(42, prof, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	if artifact.BenchVersion != protocol.BenchVersionV9 {
		t.Fatalf("artifact version=%d, want 9", artifact.BenchVersion)
	}
	for _, c := range artifact.MemoryCases {
		if c.BenchVersion != protocol.BenchVersionV9 {
			t.Fatalf("memory case %s version=%d, want 9", c.ID, c.BenchVersion)
		}
	}
}

func TestV9ProfilesAreExplicitAndPinned(t *testing.T) {
	want := map[string]Profile{
		"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
		"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
		"full":   {Tools: 100, Mem: 225, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
	}
	for runSize, expected := range want {
		got, ok := ProfileForVersion(runSize, protocol.BenchVersionV9)
		if !ok || got != expected {
			t.Errorf("v9 %s profile=(%+v,%v), want %+v", runSize, got, ok, expected)
		}
	}
	if got, ok := ProfileForVersion("unknown", protocol.BenchVersionV9); ok || got != want["small"] {
		t.Errorf("unknown v9 run size=(%+v,%v), want explicit small fallback plus false", got, ok)
	}
	for _, future := range []int{0, 1, protocol.BenchVersionV12 + 1, 99} {
		if got, ok := ProfileForVersion("full", future); ok || got != (Profile{}) {
			t.Errorf("future/unknown version %d inherited profile (%+v,%v)", future, got, ok)
		}
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatalf("profile scaffold activated v9: current=%d", protocol.CurrentBenchVersion)
	}
}

func TestV9SameTupleProducesIdenticalBytesForEveryProfile(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV9)
		for _, seed := range []int64{0, 1, 42, 123456789} {
			a, err := GenerateDataset(seed, prof, protocol.BenchVersionV9)
			if err != nil {
				t.Fatalf("%s seed %d first generation: %v", runSize, seed, err)
			}
			b, err := GenerateDataset(seed, prof, protocol.BenchVersionV9)
			if err != nil {
				t.Fatalf("%s seed %d second generation: %v", runSize, seed, err)
			}
			ha, ba, err := a.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			hb, bb, err := b.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			if ha != hb || !reflect.DeepEqual(ba, bb) {
				t.Fatalf("%s seed %d was not byte-deterministic: %s != %s", runSize, seed, ha, hb)
			}
		}
	}
}

// This reconstructs the exact production generation sequence while retaining
// StagedCase.RequiredPairIDs, which are generator-only and intentionally absent
// from the published artifact. Every declared tool and memory dependency must
// resolve through the artifact's actual seed surfaces for the correct user.
func TestV9EveryProfileSeedsAllToolAndMemoryEvidenceReferences(t *testing.T) {
	knownSmallGaps := map[int64]bool{4: true, 6: true, 9: true}
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV9)
		for seed := int64(1); seed <= 64; seed++ {
			rng, err := NewRNGForVersion(seed, protocol.BenchVersionV9)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV9)
			suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV9)
			if err != nil {
				t.Fatalf("%s seed %d memory: %v", runSize, seed, err)
			}
			isolation, err := GenerateIsolationForVersion(seed, prof.Mem, prof.Waves, prof.IsoCases, protocol.BenchVersionV9)
			if err != nil {
				t.Fatalf("%s seed %d isolation: %v", runSize, seed, err)
			}
			suite.Cases = append(suite.Cases, isolation.Cases...)
			waves := MergeMemoryWaves(suite.Waves, isolation.SecondaryWave)
			artifact, err := BuildArtifactForVersion(seed, protocol.BenchVersionV9, tools, suite.Cases, waves)
			if err != nil {
				t.Fatalf("%s seed %d artifact: %v", runSize, seed, err)
			}
			if len(artifact.ToolCases) != prof.Tools {
				t.Fatalf("%s seed %d tool count=%d, want %d", runSize, seed, len(artifact.ToolCases), prof.Tools)
			}

			available := map[string]map[string]bool{}
			evidence := map[string]*strings.Builder{}
			addPairs := func(user string, pairs []protocol.MemoryPair) {
				if user == "" {
					user = PrimaryUser
				}
				if available[user] == nil {
					available[user] = map[string]bool{}
					evidence[user] = &strings.Builder{}
				}
				for _, pair := range pairs {
					available[user][pair.PairID] = true
					evidence[user].WriteString(pair.Prompt)
					evidence[user].WriteByte('\n')
					evidence[user].WriteString(pair.Response)
					evidence[user].WriteByte('\n')
				}
			}

			scale, _ := v8WorldProfile(prof.Mem)
			world := universe.Generate(seed, scale)
			worldPairIDs := make(map[string]bool, len(world.Pairs))
			for _, pair := range world.Pairs {
				worldPairIDs[pair.PairID] = true
			}
			worldCarriers := 0
			carrierCategory := ""
			worldToolCases := 0
			for _, tc := range artifact.ToolCases {
				if strings.HasPrefix(tc.Category, "world_") {
					worldToolCases++
				}
				carriesWorld := false
				for _, pair := range tc.PrerequisitePairs {
					carriesWorld = carriesWorld || worldPairIDs[pair.PairID]
				}
				if carriesWorld {
					worldCarriers++
					carrierCategory = tc.Category
				}
				addPairs(PrimaryUser, tc.PrerequisitePairs)
			}
			for _, wave := range artifact.MemoryWaves {
				addPairs(wave.UserID, wave.Pairs)
			}
			if worldCarriers != 1 {
				t.Fatalf("%s seed %d coherent world has %d tool carriers, want exactly 1", runSize, seed, worldCarriers)
			}

			primaryEvidence := evidence[PrimaryUser].String()
			for _, tc := range artifact.ToolCases {
				if !strings.HasPrefix(tc.Category, "world_") {
					continue
				}
				for _, spec := range tc.ExpectedTools {
					for key, value := range spec.RequiredArgs {
						switch key {
						case "pair_id":
							if !available[PrimaryUser][value] {
								t.Errorf("%s seed %d case %s requires unseeded pair_id %q", runSize, seed, tc.ID, value)
							}
						case "to", "color", "name", "steps":
							if !strings.Contains(primaryEvidence, value) {
								t.Errorf("%s seed %d case %s requires %s=%q absent from seeded evidence", runSize, seed, tc.ID, key, value)
							}
						}
					}
				}
			}

			worldMemoryCases := 0
			declaredMemoryPairs := 0
			for _, staged := range suite.Cases {
				if strings.HasPrefix(staged.Case.QuestionType, "world-") {
					worldMemoryCases++
				}
				user := staged.UserID
				if user == "" {
					user = PrimaryUser
				}
				for _, pairID := range staged.RequiredPairIDs {
					declaredMemoryPairs++
					if !available[user][pairID] {
						t.Fatalf("%s seed %d memory case %s requires unseeded pair %s for user %s", runSize, seed, staged.Case.ID, pairID, user)
					}
				}
			}
			if worldMemoryCases == 0 || declaredMemoryPairs == 0 {
				t.Fatalf("%s seed %d did not exercise declared world-memory evidence: world_cases=%d required_pairs=%d", runSize, seed, worldMemoryCases, declaredMemoryPairs)
			}

			if runSize == "small" && knownSmallGaps[seed] {
				if worldToolCases != 0 {
					t.Fatalf("small seed %d changed family mix: got %d world tool cases, want 0", seed, worldToolCases)
				}
				if strings.HasPrefix(carrierCategory, "world_") {
					t.Fatalf("small seed %d did not exercise ordinary-family world carrier: %s", seed, carrierCategory)
				}
			}
		}
	}
}

func TestV9FinalToolAndMemoryFamiliesAcrossFortySeeds(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV9)
	wantMemory := v9MemoryFamilies()
	toolHistograms := map[string]bool{}
	memoryHistograms := map[string]bool{}
	toolFamilies := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV9)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		toolHist := map[string]int{}
		for _, c := range artifact.ToolCases {
			toolHist[c.Category]++
			toolFamilies[c.Category] = true
		}
		if toolHist["set_effort"] < 1 {
			t.Errorf("seed %d omitted set_effort", seed)
		}
		if len(toolHist) != 53 {
			t.Errorf("seed %d has %d final tool families, want 53", seed, len(toolHist))
		}
		toolHistograms[v9HistogramKey(toolHist)] = true

		memoryHist := map[string]int{}
		for _, c := range artifact.MemoryCases {
			memoryHist[c.QuestionType]++
			if c.BenchVersion != protocol.BenchVersionV9 {
				t.Errorf("seed %d memory case %s version=%d", seed, c.ID, c.BenchVersion)
			}
		}
		for family := range wantMemory {
			if memoryHist[family] < 1 {
				t.Errorf("seed %d omitted memory family %q", seed, family)
			}
		}
		for family := range memoryHist {
			if !wantMemory[family] {
				t.Errorf("seed %d emitted unexpected memory family %q", seed, family)
			}
		}
		memoryHistograms[v9HistogramKey(memoryHist)] = true
	}
	if len(toolFamilies) != 53 {
		t.Fatalf("40-seed tool union=%d families, want 53", len(toolFamilies))
	}
	if len(toolHistograms) < 35 {
		t.Errorf("only %d distinct tool histograms across 40 seeds", len(toolHistograms))
	}
	if len(memoryHistograms) < 35 {
		t.Errorf("only %d distinct memory histograms across 40 seeds", len(memoryHistograms))
	}
}

// TestV9MemorySelectionBypassesLegacyFillOrder proves the memory-side finding
// from #499 is absent in the scored v9 path. The legacy weightedTypeQuota helper
// is reachable only before v8; v9's coherent-world selector gives every final
// computed, temporal, and integrity family a positive full-run floor.
func TestV9MemorySelectionBypassesLegacyFillOrder(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV9)
	minimum := map[string]int{}
	for family := range v9MemoryFamilies() {
		minimum[family] = int(^uint(0) >> 1)
	}
	for seed := int64(1); seed <= 40; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV9)
		if err != nil {
			t.Fatal(err)
		}
		counts := map[string]int{}
		for _, c := range artifact.MemoryCases {
			counts[c.QuestionType]++
		}
		for family := range minimum {
			if counts[family] < minimum[family] {
				minimum[family] = counts[family]
			}
		}
	}
	for family, count := range minimum {
		if count < 1 {
			t.Errorf("memory family %q has 40-seed minimum %d", family, count)
		}
	}
	for _, computed := range []string{
		"world-project-outstanding", "world-trip-current", "world-trip-changed-leg-current",
		"world-trip-changed-leg-previous", "world-trip-longest-current", "world-story-balance-current",
		"world-story-budget-delta", "world-story-later-net-change", "world-story-post-approval-balance",
	} {
		if minimum[computed] < 1 {
			t.Errorf("computed memory family %q was starved", computed)
		}
	}
}

func TestV9IsolationIsOracleValidAcrossThreeHundredSeeds(t *testing.T) {
	// Seed 143 is a regression vector: copying an anchor given name onto the
	// secondary-world source originally collided with another person's full
	// (name, employer, event) constraint tuple. Exercise the whole qualification
	// range so another surface collision cannot silently block a 300-seed study.
	for seed := int64(1); seed <= 300; seed++ {
		iso, err := GenerateIsolationForVersion(seed, 225, 5, 9, protocol.BenchVersionV9)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		if len(iso.Cases) != 9 || len(iso.ReviewPlans) != 9 {
			t.Fatalf("seed %d: cases=%d plans=%d, want 9 each", seed, len(iso.Cases), len(iso.ReviewPlans))
		}
		for i, staged := range iso.Cases {
			if staged.Case.ExpectedAnswer == staged.Case.ForbiddenAnswer {
				t.Errorf("seed %d case %d lost cross-user answer conflict", seed, i)
			}
			if staged.Case.BenchVersion != protocol.BenchVersionV9 {
				t.Errorf("seed %d case %d version=%d", seed, i, staged.Case.BenchVersion)
			}
		}
	}
}

func v9MemoryFamilies() map[string]bool {
	return map[string]bool{
		"conversational-chitchat": true, "conversational-declarative": true, "declarative-behavior": true,
		"world-canary": true, "world-contact-current": true, "world-contact-previous": true,
		"world-injection-resistance": true, "world-isolation-contact-current": true,
		"world-project-lead-current": true, "world-project-lead-previous": true, "world-project-outstanding": true,
		"world-story-balance-current": true, "world-story-budget-delta": true, "world-story-contact-current": true,
		"world-story-later-net-change": true, "world-story-lesson": true, "world-story-outcome-summary": true,
		"world-story-post-approval-balance": true, "world-trip-changed-leg-current": true,
		"world-trip-changed-leg-previous": true, "world-trip-current": true, "world-trip-longest-current": true,
	}
}

func v9HistogramKey(counts map[string]int) string {
	keys := make([]string, 0, len(counts))
	for key := range counts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var out strings.Builder
	for _, key := range keys {
		out.WriteString(key)
		out.WriteByte('=')
		out.WriteString(strconv.Itoa(counts[key]))
		out.WriteByte(';')
	}
	return out.String()
}
