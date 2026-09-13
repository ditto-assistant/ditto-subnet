package appearance

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
)

func TestChoiceIsDeterministicAndSeedVarying(t *testing.T) {
	a, b := ForSeed(41), ForSeed(41)
	if a.Inventory() != b.Inventory() || a.Accent != b.Accent || a.Font != b.Font || a.Mode != b.Mode {
		t.Fatal("appearance choice is not deterministic")
	}
	distinct := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		distinct[ForSeed(seed).Accent+"|"+ForSeed(seed).Font] = true
	}
	if len(distinct) < 30 {
		t.Fatalf("only %d distinct accent/font choices across 40 seeds", len(distinct))
	}
}

// TestChoiceIsAListedCatalogTarget: the stated preference is always one of the
// discovery-grounded targets the catalog inventory lists, so the world's
// declarative records, the served discover_capabilities result, and the graded
// canonical value agree.
func TestChoiceIsAListedCatalogTarget(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		c := ForSeed(seed)
		inv := catalog.InventoryForSeed(seed)
		if !contains(inv.AccentTargets(), c.Accent) || !contains(inv.FontTargets(), c.Font) {
			t.Fatalf("seed %d chose %q / %q outside the catalog targets", seed, c.Accent, c.Font)
		}
		if canonical, ok := inv.MatchAccent(c.Accent); !ok || canonical != c.Accent {
			t.Fatalf("seed %d accent %q does not resolve on the served inventory", seed, c.Accent)
		}
		if canonical, ok := inv.MatchFont(c.Font); !ok || canonical != c.Font {
			t.Fatalf("seed %d font %q does not resolve on the served inventory", seed, c.Font)
		}
		if contains(c.RejectedAccents(), c.Accent) || contains(c.RejectedFonts(), c.Font) || contains(c.RejectedModes(), c.Mode) {
			t.Fatalf("seed %d rejected list carries the chosen value", seed)
		}
		if !strings.Contains(c.Inventory(), c.Accent) || !strings.Contains(c.Inventory(), c.Font) {
			t.Fatalf("seed %d inventory omits a seeded value: %s", seed, c.Inventory())
		}
	}
}

func contains(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}
