package datagen

import (
	"math/rand"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8ToolWritingNoiseCoversEverySemanticDomain(t *testing.T) {
	const seed int64 = 424242
	rotated, err := protocol.RotateSeedForVersion(seed, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(rotated)), seed, 100, protocol.BenchVersionV8)
	coverage := applyV8WritingNoise(seed+1, cases)
	for _, domain := range []string{"personal", "business", "research", "settings", "general"} {
		if got := coverage["prompt:"+domain]; got < 2 {
			t.Errorf("V8 noisy tool prompts for %s=%d, want at least 2; all=%v", domain, got, coverage)
		}
	}
	for _, domain := range []string{"personal", "business", "general"} {
		if got := coverage["pair:"+domain]; got < 2 {
			t.Errorf("V8 noisy prerequisite pairs for %s=%d, want at least 2; all=%v", domain, got, coverage)
		}
	}
}
