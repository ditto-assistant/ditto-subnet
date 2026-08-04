package datagen

import (
	"math/rand"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// A result-usage case's prompt must reference the SAME needle subject the mock
// server will serve; otherwise the question and the served answer are incoherent.
func TestResultUsagePromptsAreCoherent(t *testing.T) {
	const seed = 12345
	r := rand.New(rand.NewSource(seed))
	// A large n so every category (incl. the two result-usage ones) appears.
	cases := GenerateCases(r, seed, len(categories)*3)

	seen := 0
	for _, c := range cases {
		if !IsResultUsage(c.Category) {
			continue
		}
		seen++
		subject := toolexec.NeedleFor(seed, c.ID).Subject
		if subject == "" {
			t.Fatalf("case %s: empty needle subject", c.ID)
		}
		if !strings.Contains(c.Prompt, subject) {
			t.Fatalf("case %s prompt %q must contain needle subject %q", c.ID, c.Prompt, subject)
		}
		if len(c.ExpectedTools) == 0 {
			t.Fatalf("case %s: result-usage case must expect a tool", c.ID)
		}
		// Every expected tool must be one the mock endpoint actually serves.
		for _, et := range c.ExpectedTools {
			if !toolexec.Serves(et.Name) {
				t.Fatalf("case %s: result-usage expects unserved tool %q", c.ID, et.Name)
			}
		}
	}
	if seen == 0 {
		t.Fatal("no result-usage cases generated")
	}
}

// Determinism: result-usage prompt (needle subject) is fixed by the seed.
func TestResultUsageDeterministic(t *testing.T) {
	const seed = 99
	a := GenerateCases(rand.New(rand.NewSource(seed)), seed, len(categories)*2)
	b := GenerateCases(rand.New(rand.NewSource(seed)), seed, len(categories)*2)
	for i := range a {
		if a[i].Prompt != b[i].Prompt || a[i].ID != b[i].ID {
			t.Fatalf("case %d not deterministic: %q vs %q", i, a[i].Prompt, b[i].Prompt)
		}
	}
}
