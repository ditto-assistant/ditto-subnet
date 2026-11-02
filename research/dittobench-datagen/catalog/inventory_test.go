package catalog

import (
	"strings"
	"testing"
)

func TestOSADistance(t *testing.T) {
	for _, tc := range []struct {
		a, b string
		want int
	}{
		{"inter", "inter", 0}, {"inter", "intr", 1}, {"inter", "itner", 1}, {"Inter", "inter", 0},
		{"inter", "inter tight", 6}, {"mint", "mint green", 6}, {"teal", "tael", 1}, {"abc", "", 3},
	} {
		if got := OSADistance(tc.a, tc.b); got != tc.want {
			t.Errorf("OSADistance(%q,%q)=%d want %d", tc.a, tc.b, got, tc.want)
		}
	}
}

// TestInventoryLists: both near-miss members listed, legacy accents listed (the
// capped world family stays solvable), legacy fonts never listed, corpus draws
// never a legacy value, and the served text carries every canonical spelling.
func TestInventoryLists(t *testing.T) {
	legacyFont := map[string]bool{}
	for _, f := range legacyFonts {
		legacyFont[strings.ToLower(f)] = true
	}
	legacyAccent := map[string]bool{}
	for _, c := range legacyAccents {
		legacyAccent[c] = true
	}
	distinctAccentTargets := map[string]bool{}
	distinctFontTargets := map[string]bool{}
	for seed := int64(1); seed <= 100; seed++ {
		inv := InventoryForSeed(seed)
		text := inv.DiscoverText()
		for _, name := range append(append([]string{}, inv.Accents...), inv.Fonts...) {
			if !strings.Contains(text, name) {
				t.Fatalf("seed %d served text lacks %q", seed, name)
			}
		}
		for _, pair := range []NearMiss{inv.AccentNearMiss, inv.FontNearMiss} {
			if _, ok := matchOption(append(inv.Accents, inv.Fonts...), pair.Base); !ok {
				t.Fatalf("seed %d near-miss base %q unlisted", seed, pair.Base)
			}
			if _, ok := matchOption(append(inv.Accents, inv.Fonts...), pair.Partner); !ok {
				t.Fatalf("seed %d near-miss partner %q unlisted", seed, pair.Partner)
			}
			if !strings.HasPrefix(strings.ToLower(pair.Partner), strings.ToLower(pair.Base)+" ") {
				t.Fatalf("near-miss partner %q is not base %q plus a qualifier", pair.Partner, pair.Base)
			}
		}
		for _, c := range legacyAccents {
			if _, ok := inv.MatchAccent(c); !ok {
				t.Fatalf("seed %d dropped legacy accent %q from the list", seed, c)
			}
		}
		// Ordinary world preferences may independently draw a familiar font
		// from the public corpus. They must be listed, but never become a
		// discovery-family target merely because they were in an old pool.
		for _, target := range inv.FontTargets() {
			if legacyFont[strings.ToLower(target)] {
				t.Fatalf("seed %d font target %q is a legacy font", seed, target)
			}
		}
		if len(inv.AccentTargets()) != inventoryAccentDraws || len(inv.FontTargets()) != inventoryFontDraws {
			t.Fatalf("seed %d target counts %d/%d", seed, len(inv.AccentTargets()), len(inv.FontTargets()))
		}
		for _, target := range inv.AccentTargets() {
			if legacyAccent[target] {
				t.Fatalf("seed %d accent target %q is a legacy colour", seed, target)
			}
			distinctAccentTargets[target] = true
		}
		for _, target := range inv.FontTargets() {
			distinctFontTargets[target] = true
		}
		if got, ok := inv.MatchAccent(strings.ToUpper(inv.Accents[0])); !ok || got != inv.Accents[0] {
			t.Fatalf("case-insensitive match failed: %q %v", got, ok)
		}
		if _, ok := inv.MatchAccent("not-a-colour"); ok {
			t.Fatal("unlisted value matched")
		}
	}
	if len(distinctAccentTargets) < 60 || len(distinctFontTargets) < 60 {
		t.Fatalf("inventories draw from too small a surface: %d accents, %d fonts across 100 seeds", len(distinctAccentTargets), len(distinctFontTargets))
	}
}

// TestAliasForMarginProperty: over 300 seeds, every target of every inventory
// gets an alias within floor(len/3) edits whose UNIQUE nearest listed option is
// the target with margin >= 1, and the alias equals no listed option.
func TestAliasForMarginProperty(t *testing.T) {
	failures := 0
	for seed := int64(1); seed <= 300; seed++ {
		inv := InventoryForSeed(seed)
		check := func(base string, options []string, salt string) {
			alias, ok := AliasFor(base, options, seed, salt)
			if !ok {
				failures++
				t.Errorf("seed %d: no admissible alias for %q", seed, base)
				return
			}
			if alias.Edits < 1 || alias.Edits > MaxAliasEdits(base) || alias.Distance > MaxAliasEdits(base) {
				t.Errorf("seed %d %q alias %q edits=%d distance=%d exceeds floor(len/3)=%d", seed, base, alias.Text, alias.Edits, alias.Distance, MaxAliasEdits(base))
			}
			if strings.EqualFold(alias.Text, base) {
				t.Errorf("seed %d alias equals the canonical %q", seed, base)
			}
			d := OSADistance(alias.Text, base)
			for _, other := range options {
				if strings.EqualFold(other, base) {
					continue
				}
				if od := OSADistance(alias.Text, other); od-d < 1 {
					t.Errorf("seed %d alias %q for %q is within margin of %q (%d vs %d)", seed, alias.Text, base, other, od, d)
				}
			}
		}
		for _, target := range inv.AccentTargets() {
			check(target, inv.Accents, "test")
		}
		check(inv.AccentNearMiss.Base, inv.Accents, "test")
		for _, target := range inv.FontTargets() {
			check(target, inv.Fonts, "test")
		}
		check(inv.FontNearMiss.Base, inv.Fonts, "test")
	}
	if failures > 0 {
		t.Fatalf("%d targets had no admissible alias", failures)
	}
}
