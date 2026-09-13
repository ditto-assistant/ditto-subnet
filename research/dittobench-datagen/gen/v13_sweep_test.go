package gen

import (
	"fmt"
	"math/rand"
	"strings"
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

// TestV13NonPublicRunSizeGenerates: the v13 slot table is published for the
// three public profiles only, so a memory count outside them fails closed
// instead of inventing an envelope (the analysis path passes a public profile
// and derives its isolation quota from the world scale, v13IsoCasesForMem);
// every public profile builds its whole abstention budget, cross-user family
// included.
func TestV13NonPublicRunSizeGenerates(t *testing.T) {
	for _, n := range []int{45, 120} {
		rng, err := NewRNGForVersion(7, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := GenerateMemorySuiteForVersion(rng, 7, n, 5, 0.5, protocol.BenchVersionV13); err == nil || !strings.Contains(err.Error(), "no slot table") {
			t.Fatalf("n=%d: want the slot-table fail-closed error, got %v", n, err)
		}
	}
	for _, runSize := range []string{"medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		rng, err := NewRNGForVersion(7, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		suite, err := GenerateMemorySuiteForVersion(rng, 7, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("%s: %v", runSize, err)
		}
		if suite.AbstentionCases != v13AbstentionCaseCount(prof.Mem) {
			t.Fatalf("%s: %d abstention pairs, want %d", runSize, suite.AbstentionCases, v13AbstentionCaseCount(prof.Mem))
		}
	}
}
