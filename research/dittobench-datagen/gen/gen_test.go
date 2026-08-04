package gen

import (
	"testing"
)

func TestProfiles(t *testing.T) {
	for _, size := range []string{"small", "medium", "full"} {
		p, ok := ProfileFor(size)
		if !ok {
			t.Fatalf("profile %q should exist", size)
		}
		if p.Tools <= 0 || p.Mem <= 0 {
			t.Fatalf("profile %q has non-positive counts: %+v", size, p)
		}
	}
	// small must be the cheapest
	s, _ := ProfileFor("small")
	f, _ := ProfileFor("full")
	if s.Tools >= f.Tools || s.Mem >= f.Mem {
		t.Fatalf("small should be cheaper than full")
	}
	// unknown size falls back to small
	if p, ok := ProfileFor("xl"); ok || p != Profiles["small"] {
		t.Fatalf("unknown size should fall back to small (ok=false)")
	}
}

func TestFreshSeedUnique(t *testing.T) {
	seen := map[int64]bool{}
	for i := 0; i < 1000; i++ {
		s := FreshSeed()
		if s < 0 {
			t.Fatalf("seed should be non-negative, got %d", s)
		}
		if seen[s] {
			t.Fatalf("duplicate seed %d in 1000 draws", s)
		}
		seen[s] = true
	}
}

func TestGenerateToolsNilLLM(t *testing.T) {
	r := NewRNG(7)
	cases, _ := GenerateTools(r, 7, 10)
	if len(cases) != 10 {
		t.Fatalf("expected 10 cases, got %d", len(cases))
	}
	for _, c := range cases {
		if c.ID == "" || c.Prompt == "" || c.Category == "" {
			t.Fatalf("malformed case: %+v", c)
		}
	}
}
