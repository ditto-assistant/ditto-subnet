package gen

import "testing"

// TestGenerateToolsDeterministic: the same seed yields byte-identical tool
// prompts across runs, with no LLM involved (surface variation comes from
// datagen's seeded template variants).
func TestGenerateToolsDeterministic(t *testing.T) {
	a, _ := GenerateTools(NewRNG(99), 99, 20)
	b, _ := GenerateTools(NewRNG(99), 99, 20)
	if len(a) != len(b) {
		t.Fatalf("case count differs across runs: %d vs %d", len(a), len(b))
	}
	for i := range a {
		if a[i].Prompt != b[i].Prompt {
			t.Fatalf("case %d prompt not deterministic: %q vs %q", i, a[i].Prompt, b[i].Prompt)
		}
	}
}
