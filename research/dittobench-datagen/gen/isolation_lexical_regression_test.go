package gen

import (
	"fmt"
	"os"
	"testing"
)

func TestV13IsolationLexicalRegression(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	for _, seed := range []int64{2004083, 2004084, 2004085} {
		if _, err := GenerateDataset(seed, profile, 13); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
	}
}

// The real N14 run generates entire datasets, not just cross-user anchors.
// Keep this opt-in sweep separate from the ordinary small regression set.
func TestV13FullRouterTrainingSeedSweep(t *testing.T) {
	if os.Getenv("DITTO_V13_FULL_ROUTER_SWEEP") != "1" {
		t.Skip("operator full-dataset sweep")
	}
	profile, _ := ProfileForVersion("full", 13)
	for shard := 0; shard < 10; shard++ {
		t.Run(fmt.Sprint(shard), func(t *testing.T) {
			t.Parallel()
			first := int64(2000000 + shard*1000)
			for seed := first; seed < first+1000; seed++ {
				if _, err := GenerateDataset(seed, profile, 13); err != nil {
					t.Fatalf("seed %d: %v", seed, err)
				}
			}
		})
	}
}
