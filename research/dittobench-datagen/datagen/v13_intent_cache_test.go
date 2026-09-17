package datagen

import (
	"math/rand"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV13EffortIntentMatchesGradedValueAcrossOneRun(t *testing.T) {
	seen := map[string]int{}
	for seed := int64(1); seed <= 100; seed++ {
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(seed)), seed, 100, protocol.BenchVersionV13)
		for _, c := range cases {
			if c.Category != "set_effort" {
				continue
			}
			prompt := strings.ToLower(c.Prompt)
			want := ""
			for _, phrase := range []string{"deeply and carefully", "think everything through thoroughly", "as deep as you can", "think hard and carefully"} {
				if strings.Contains(prompt, phrase) {
					want = "high"
				}
			}
			for _, phrase := range []string{"reasoning balanced", "middle-of-the-road", "moderate level", "balanced on how much"} {
				if strings.Contains(prompt, phrase) {
					if want != "" {
						t.Fatal("ambiguous effort intent")
					}
					want = "medium"
				}
			}
			if want == "" {
				t.Fatalf("unrecognized public effort intent: %q", c.Prompt)
			}
			seen[want]++
			if got := c.ExpectedTools[0].RequiredArgs["effort"]; got != want {
				t.Fatalf("seed %d: prompt %q implies %s, grader requires %s", seed, c.Prompt, want, got)
			}
		}
	}
	if seen["high"] == 0 || seen["medium"] == 0 {
		t.Fatal("both intent banks must be exercised")
	}
}
