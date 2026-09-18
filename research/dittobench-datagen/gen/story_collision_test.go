package gen_test

import (
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"testing"
)

func TestV13InvoiceStoryCollisionSeedGenerates(t *testing.T) {
	p, _ := gen.ProfileForVersion("full", 13)
	for _, seed := range []int64{2000260, 2000261, 2000262} {
		if _, err := gen.GenerateDataset(seed, p, 13); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
	}
}
