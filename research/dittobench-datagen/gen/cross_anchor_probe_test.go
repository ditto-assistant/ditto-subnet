package gen

import (
	"crypto/sha256"
	"fmt"
	"os"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestV13CrossAnchorRecoveryRegression(t *testing.T) {
	for _, seed := range []int64{2001896, 2001897, 2001898} {
		_, err := v13CrossUserFacts(seed, 250, universe.V13Allocation{CrossPeople: []int{0, 2, 4}}, 13)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		profile, _ := ProfileForVersion("full", 13)
		if _, err := GenerateDataset(seed, profile, 13); err != nil {
			t.Fatalf("full seed %d: %v", seed, err)
		}
	}
}

// The prelaunch enterprise slice deliberately changes the former qualification
// artifact. Pin its corrected bytes; old receipts do not qualify this candidate.
func TestV13CrossAnchorRecoveryPinsCurrentCandidate(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	a, err := GenerateDataset(1294236556, profile, 13)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := a.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprintf("%x", sha256.Sum256(raw)); got != "0acb227a590f07c9257797657732fbe364d94dad0d4514afcfede050d644b965" {
		t.Fatalf("qualification artifact changed: %s", got)
	}
}

func TestV13CrossAnchorTenThousandSeedSweep(t *testing.T) {
	if os.Getenv("DITTO_V13_ANCHOR_SWEEP") != "1" {
		t.Skip("operator sweep")
	}
	for seed := int64(2000000); seed < 2010000; seed++ {
		if _, err := v13CrossUserFacts(seed, 250, universe.V13Allocation{CrossPeople: []int{0, 2, 4}}, 13); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
	}
}
