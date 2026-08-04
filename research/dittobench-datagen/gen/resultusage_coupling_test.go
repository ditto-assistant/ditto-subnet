package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// TestResultUsagePromptCoupledToServedNeedle pins the prompt/needle coupling:
// GenerateTools must derive result-usage prompts from the SAME master seed the
// mock endpoint serves from (BuildFixture), so the entity a prompt asks about is
// exactly the one the tool returns. Regenerating tool cases from a fresh sub-seed
// would make the prompt ask about a different fabricated entity than the served
// needle, penalizing a harness that reconciles the two.
func TestResultUsagePromptCoupledToServedNeedle(t *testing.T) {
	const seed = int64(123456789)
	cases, _ := GenerateTools(NewRNG(seed), seed, 60)
	checked := 0
	for _, c := range cases {
		if !datagen.IsResultUsage(c.Category) {
			continue
		}
		subject := toolexec.NeedleFor(seed, c.ID).Subject
		if subject == "" {
			t.Fatalf("case %s: served needle has an empty subject", c.ID)
		}
		if !strings.Contains(strings.ToLower(c.Prompt), strings.ToLower(subject)) {
			t.Errorf("case %s: prompt %q does not mention the served needle subject %q", c.ID, c.Prompt, subject)
		}
		checked++
	}
	if checked == 0 {
		t.Fatal("no result-usage cases generated to check coupling")
	}
}
