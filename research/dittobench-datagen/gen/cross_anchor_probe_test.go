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

func TestV13CrossAnchorRecoveryPreservesQualificationSeed(t *testing.T) {
	profile, _ := ProfileForVersion("full", 13)
	a, err := GenerateDataset(1294236556, profile, 13)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := a.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprintf("%x", sha256.Sum256(raw)); got != "aa87ff7416d0b2cd2fa9d894b2741d315e830ae81707002c34d73783b1a3e6a3" {
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
