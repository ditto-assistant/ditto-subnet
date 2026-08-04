package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
)

// TestInjectionShapesAreNotDominated is the B2 gate. The injection framings used
// to come from 13 fixed templates, which with a public generator is a
// memorizable lexicon: a harness fingerprints every shape and routes on it
// without understanding the attack. Framings are now composed from
// independently-varied parts, so no single shape may account for more than a
// small fraction of injection cases across a seed sweep.
func TestInjectionShapesAreNotDominated(t *testing.T) {
	prof, _ := ProfileFor("full")
	shapes := map[string]int{}
	total := 0
	for seed := int64(1); seed <= 40; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		payload := persona.InjectionPayload(seed)
		for _, c := range cases {
			if c.ForbiddenAnswer != payload || c.ForbiddenAnswer == "" {
				continue
			}
			shapes[persona.InjectionSkeleton(c.Question, payload)]++
			total++
		}
	}
	if total == 0 {
		t.Fatal("no injection cases sampled")
	}
	top, topShape := 0, ""
	for s, n := range shapes {
		if n > top {
			top, topShape = n, s
		}
	}
	share := float64(top) / float64(total)
	if share > 0.15 {
		t.Fatalf("most common injection shape covers %.1f%% of %d cases (want <=15%%) — the framing pool is still fingerprintable:\n  %s", share*100, total, topShape)
	}
	t.Logf("injection shapes: %d distinct over %d cases, most common %.1f%%", len(shapes), total, share*100)
}

// TestInjectionFramingsKeepPayloadAndQuestion guards the composition itself: a
// framing that dropped the payload would have nothing to detect compliance
// with, and one that dropped the real question would be unanswerable, scoring
// an honest harness 0 for no reason.
func TestInjectionFramingsKeepPayloadAndQuestion(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 20; seed++ {
		_, cases, _, err := GenerateMemoryV2(NewRNG(seed), seed, prof.Mem)
		if err != nil {
			t.Fatalf("gen: %v", err)
		}
		payload := persona.InjectionPayload(seed)
		for _, c := range cases {
			if c.ForbiddenAnswer != payload || c.ForbiddenAnswer == "" {
				continue
			}
			if !strings.Contains(c.Question, payload) {
				t.Errorf("seed %d case %s: injection framing lost the payload", seed, c.QuestionID)
			}
			if strings.Contains(persona.InjectionSkeleton(c.Question, payload), "<Q>") {
				continue // the real question is present
			}
			t.Errorf("seed %d case %s: injection framing carries no recognizable question:\n  %s", seed, c.QuestionID, c.Question)
		}
	}
}
