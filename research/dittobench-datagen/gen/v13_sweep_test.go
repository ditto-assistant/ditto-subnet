package gen

import (
	"fmt"
	"math/rand"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13RandomSeedSweepGeneratesEveryDataset is the arbitrary-seed half of the
// v13 generation contract. Validator seeds are block-hash derived, so the pinned
// vectors and the 1..40 sweeps say nothing about the seed a real run draws; a
// surface that trips the accidental lexical-shortcut exclusion on one wording
// must fall through to another wording (or a spare entity), never fail the run.
func TestV13RandomSeedSweepGeneratesEveryDataset(t *testing.T) {
	r := rand.New(rand.NewSource(0x5eed13))
	for _, runSize := range []string{"full", "medium"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		for shard := 0; shard < 8; shard++ {
			seeds := make([]int64, 25)
			for i := range seeds {
				seeds[i] = r.Int63()
			}
			t.Run(fmt.Sprintf("%s/%d", runSize, shard), func(t *testing.T) {
				t.Parallel()
				for _, seed := range seeds {
					if _, err := GenerateDataset(seed, prof, protocol.BenchVersionV13); err != nil {
						t.Fatalf("%s seed %d: %v", runSize, seed, err)
					}
				}
			})
		}
	}
}

// TestV13NonPublicRunSizeGenerates covers the analysis path (vstudy/gstudy):
// a memory count outside the three public profiles still builds the whole
// abstention budget, including the cross-user family, from the world scale.
func TestV13NonPublicRunSizeGenerates(t *testing.T) {
	for _, n := range []int{45, 120} {
		rng, err := NewRNGForVersion(7, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		suite, err := GenerateMemorySuiteForVersion(rng, 7, n, 5, 0.5, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("n=%d: %v", n, err)
		}
		if suite.AbstentionCases != v13AbstentionCaseCount(n) {
			t.Fatalf("n=%d: %d abstention pairs, want %d", n, suite.AbstentionCases, v13AbstentionCaseCount(n))
		}
	}
}
