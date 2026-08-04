package persona

import (
	"math/rand"
	"strings"
	"testing"
)

// TestVarySurfacePreservesValue is the load-bearing property: the wrapped
// statement must still contain the fact value verbatim (the memory grader
// checks normalized containment), regardless of which lead-in/trailer lands.
func TestVarySurfacePreservesValue(t *testing.T) {
	for seed := int64(0); seed < 200; seed++ {
		r := rand.New(rand.NewSource(seed))
		got := varySurface(r, "I just moved to Denver.", "Denver")
		if !strings.Contains(got, "Denver") {
			t.Fatalf("seed %d: value dropped from %q", seed, got)
		}
	}
}

// TestVarySurfaceProducesDiversity checks a fixed statement renders many
// distinct surfaces across seeds (so a fixed regex cannot anchor on one frame).
func TestVarySurfaceProducesDiversity(t *testing.T) {
	seen := map[string]bool{}
	for seed := int64(0); seed < 300; seed++ {
		r := rand.New(rand.NewSource(seed))
		seen[varySurface(r, "I just moved to Denver.", "Denver")] = true
	}
	if len(seen) < 20 {
		t.Fatalf("expected many distinct surfaces, got %d", len(seen))
	}
}

// TestHaystackValuesRecoverable confirms every non-abstention self-fact value
// still appears verbatim somewhere in the rendered haystack after surface
// variation, so recall remains answerable.
func TestHaystackValuesRecoverable(t *testing.T) {
	p := BuildPlan(7, DefaultOpts())
	// Concatenate every user turn we would render.
	var all strings.Builder
	for _, f := range p.Facts {
		all.WriteString(f.UserText)
		all.WriteByte('\n')
		all.WriteString(f.AsstText) // asst-rec values live on the assistant side
		all.WriteByte('\n')
	}
	hay := all.String()
	for _, f := range p.Facts {
		if f.Entity != "self" || f.Value == "" {
			continue
		}
		if !strings.Contains(hay, f.Value) {
			t.Fatalf("self fact %s value %q not present in rendered haystack", f.ID, f.Value)
		}
	}
}
