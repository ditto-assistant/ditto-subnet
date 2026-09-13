// Package appearance exposes the seed's stated appearance preferences for the
// v13 world (bench_version >= 13). The served option lists themselves are the
// catalog's (catalog.InventoryForSeed): the same inventory the scorer runtime
// lists through discover_capabilities and validates set_accent_color /
// set_chat_font against, so the world's stated preference, the served option
// list, and the graded canonical value can never disagree. This package only
// picks which listed target the seed's user prefers and renders the rejected
// alternatives; every draw owns an independent hash stream, so adding it
// cannot perturb any other generator RNG.
package appearance

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
)

// Choice is one seed's appearance preference: the true accent, font, and colour
// mode, and the served option lists that surround each chosen value.
type Choice struct {
	Accent        string
	AccentOptions []string
	Font          string
	FontOptions   []string
	Mode          string
	ModeOptions   []string
	inventory     catalog.Inventory
}

var modes = []string{"dark", "light", "system"}

// ForSeed returns the deterministic appearance choice for one seed: a
// discovery-grounded accent and font target of the seed's catalog inventory
// (never a legacy pool value) and a colour mode.
func ForSeed(seed int64) Choice {
	r := rand.New(rand.NewSource(streamSeed(seed, "choice")))
	inv := catalog.InventoryForSeed(seed)
	accents, fonts := inv.AccentTargets(), inv.FontTargets()
	choice := Choice{
		Accent: accents[r.Intn(len(accents))], AccentOptions: append([]string(nil), inv.Accents...),
		Font: fonts[r.Intn(len(fonts))], FontOptions: append([]string(nil), inv.Fonts...),
		Mode: modes[r.Intn(len(modes))], ModeOptions: append([]string(nil), modes...),
		inventory: inv,
	}
	return choice
}

// RejectedAccents returns the accent options other than the chosen accent.
func (c Choice) RejectedAccents() []string { return without(c.AccentOptions, c.Accent) }

// RejectedFonts returns the font options other than the chosen font.
func (c Choice) RejectedFonts() []string { return without(c.FontOptions, c.Font) }

// RejectedModes returns the colour modes other than the chosen mode.
func (c Choice) RejectedModes() []string { return without(c.ModeOptions, c.Mode) }

// Inventory renders the appearance sentence of the discover_capabilities
// result for this seed: every listed option in served order, the true values
// undistinguished from their decoys (catalog.Inventory.DiscoverText).
func (c Choice) Inventory() string { return c.inventory.DiscoverText() }

func without(values []string, selected string) []string {
	out := make([]string, 0, len(values))
	for _, v := range values {
		if !strings.EqualFold(v, selected) {
			out = append(out, v)
		}
	}
	return out
}

func streamSeed(seed int64, salt string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-appearance|%d|%s", seed, salt)
	return int64(h.Sum64() & ((1 << 63) - 1))
}
