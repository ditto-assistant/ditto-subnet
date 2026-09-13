package appearance

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
)

func TestChoiceIsDeterministicAndSeedVarying(t *testing.T) {
	a, b := ForSeed(41), ForSeed(41)
	if a.Inventory() != b.Inventory() || a.Accent != b.Accent || a.Font != b.Font || a.Mode != b.Mode {
		t.Fatal("appearance choice is not deterministic")
	}
	distinct := map[string]bool{}
	for seed := int64(1); seed <= 40; seed++ {
		distinct[ForSeed(seed).Inventory()] = true
	}
	if len(distinct) < 38 {
		t.Fatalf("40 seeds produced only %d distinct inventories", len(distinct))
	}
}

// TestOptionsComeFromTheCorpusNotAHandList is the #1825 acceptance test: every
// accent and font option — the true value and every near-miss decoy — is an
// entry of the frozen public corpus, and the served set is not the pre-v13
// eight-colour / six-font hand list.
func TestOptionsComeFromTheCorpusNotAHandList(t *testing.T) {
	colors := map[string]bool{}
	for _, c := range publicdata.AllColors() {
		colors[c] = true
	}
	fonts := map[string]bool{}
	for _, f := range publicdata.AllFonts() {
		fonts[f] = true
	}
	legacyColors := map[string]bool{"teal": true, "indigo": true, "amber": true, "emerald": true, "crimson": true, "violet": true, "cobalt": true, "coral": true}
	legacyFonts := map[string]bool{"Atkinson Hyperlegible": true, "Inter": true, "Source Sans 3": true, "IBM Plex Sans": true, "Georgia": true, "Aptos": true}
	legacyOnlyColors, legacyOnlyFonts := 0, 0
	nearMissSeeds := 0
	for seed := int64(1); seed <= 40; seed++ {
		c := ForSeed(seed)
		if len(c.AccentOptions) != accentDecoys+1 || len(c.FontOptions) != fontDecoys+1 {
			t.Fatalf("seed %d option counts: accents=%d fonts=%d", seed, len(c.AccentOptions), len(c.FontOptions))
		}
		allLegacy := true
		for _, option := range c.AccentOptions {
			if !colors[option] {
				t.Fatalf("seed %d accent option %q is not in the colour corpus", seed, option)
			}
			allLegacy = allLegacy && legacyColors[option]
			if option != c.Accent && grade.Hit(option, c.Accent) {
				t.Fatalf("seed %d accent decoy %q is contained in the true accent %q", seed, option, c.Accent)
			}
		}
		if allLegacy {
			legacyOnlyColors++
		}
		allLegacy = true
		for _, option := range c.FontOptions {
			if !fonts[option] {
				t.Fatalf("seed %d font option %q is not in the font corpus", seed, option)
			}
			allLegacy = allLegacy && legacyFonts[option]
			if option != c.Font && grade.Hit(option, c.Font) {
				t.Fatalf("seed %d font decoy %q is contained in the true font %q", seed, option, c.Font)
			}
		}
		if allLegacy {
			legacyOnlyFonts++
		}
		if sharesWord(c.Accent, c.RejectedAccents()) || sharesWord(c.Font, c.RejectedFonts()) {
			nearMissSeeds++
		}
		if !strings.Contains(c.Inventory(), c.Accent) || !strings.Contains(c.Inventory(), c.Font) || !strings.Contains(c.Inventory(), c.Mode) {
			t.Fatalf("seed %d inventory omits a seeded value: %s", seed, c.Inventory())
		}
	}
	if legacyOnlyColors > 0 || legacyOnlyFonts > 0 {
		t.Fatalf("%d/%d seeds served only the pre-v13 hand list (colours/fonts)", legacyOnlyColors, legacyOnlyFonts)
	}
	if nearMissSeeds < 10 {
		t.Fatalf("only %d/40 seeds carry a corpus near-miss decoy", nearMissSeeds)
	}
}

func sharesWord(value string, others []string) bool {
	words := map[string]bool{}
	for _, w := range strings.Fields(strings.ToLower(value)) {
		words[w] = true
	}
	for _, other := range others {
		for _, w := range strings.Fields(strings.ToLower(other)) {
			if words[w] {
				return true
			}
		}
	}
	return false
}
