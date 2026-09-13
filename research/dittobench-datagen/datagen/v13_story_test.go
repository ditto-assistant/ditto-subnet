package datagen

import (
	"math/rand"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestV13ToolWorldSeedsStoryV2Unnoised: the shared world attached to the tool
// prerequisite boundary at v13 carries story v2 memories (opaque session ids,
// no "story-" prefix) and the writing-noise pass leaves them byte-identical to
// the world render, exactly as it leaves the v8 script memories alone.
func TestV13ToolWorldSeedsStoryV2Unnoised(t *testing.T) {
	for seed := int64(1); seed <= 3; seed++ {
		// The seed rotation for v13 lands with the plumbing PR; the tool RNG only
		// needs a deterministic source here.
		rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV12)
		if err != nil {
			t.Fatal(err)
		}
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, 100, protocol.BenchVersionV13)
		world := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		prompts := make(map[string]string, len(world.Pairs))
		for _, pair := range world.Pairs {
			prompts[pair.PairID] = pair.Prompt
		}
		storyPairs := map[string]bool{}
		for _, story := range world.Stories {
			storyPairs[story.PairID] = true
		}
		seen := 0
		for _, tc := range cases {
			for _, pair := range tc.PrerequisitePairs {
				if !storyPairs[pair.PairID] {
					continue
				}
				seen++
				if strings.HasPrefix(pair.SessionID, "story-") {
					t.Fatalf("seed %d story pair %s carries a slot-leaking session id %q", seed, pair.PairID, pair.SessionID)
				}
				if pair.Prompt != prompts[pair.PairID] {
					t.Fatalf("seed %d story pair %s was re-noised at the tool boundary", seed, pair.PairID)
				}
			}
		}
		if seen != len(storyPairs) {
			t.Fatalf("seed %d tool boundary seeded %d story pairs, world has %d", seed, seen, len(storyPairs))
		}
	}
}
