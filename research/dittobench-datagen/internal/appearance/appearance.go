// Package appearance derives the v13 seed's product-appearance preferences
// (accent colour, interface font, colour mode) and the option inventory the
// mock discover_capabilities tool serves for them.
//
// It exists so two producers agree without sharing state: universe seeds the
// preference the user states in their world, and toolexec serves the option
// list a harness must resolve a misspelled request against. Both are pure
// functions of the seed, so the served inventory always names the seeded value
// and the seeded near-misses.
//
// Choices come from the frozen public corpora (internal/publicdata) rather than
// the pre-v13 hand lists of eight colours and six fonts. Decoys prefer real
// near-misses from the same corpus ("Inter" against "Inter Tight", "teal"
// against "dark teal"); every decoy is verified not to be graded as contained
// in the true value, so a correct answer can never trip its own distractor.
package appearance

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
)

// Choice is one seed's appearance preferences with the option sets that
// surround each chosen value. Every *Options slice contains the chosen value
// plus its decoys in served (seeded, shuffled) order.
type Choice struct {
	Accent        string
	AccentOptions []string
	Font          string
	FontOptions   []string
	Mode          string
	ModeOptions   []string
}

const (
	accentDecoys = 6
	fontDecoys   = 5
)

var modes = []string{"dark", "light", "system"}

// ForSeed returns the deterministic appearance choice for one seed. It owns an
// independent hash stream, so adding it cannot perturb any other generator RNG.
func ForSeed(seed int64) Choice {
	r := rand.New(rand.NewSource(streamSeed(seed, "choice")))
	accent := publicdata.Color(r)
	font := publicdata.FontFamily(r, 0)
	mode := modes[r.Intn(len(modes))]
	choice := Choice{
		Accent: accent, AccentOptions: options(r, accent, accentDecoys, publicdata.AllColors(), func(r *rand.Rand) string { return publicdata.Color(r) }),
		Font: font, FontOptions: options(r, font, fontDecoys, publicdata.AllFonts(), func(r *rand.Rand) string { return publicdata.FontFamily(r, r.Intn(4)) }),
		Mode: mode, ModeOptions: append([]string(nil), modes...),
	}
	return choice
}

// RejectedAccents returns the accent options other than the chosen accent.
func (c Choice) RejectedAccents() []string { return without(c.AccentOptions, c.Accent) }

// RejectedFonts returns the font options other than the chosen font.
func (c Choice) RejectedFonts() []string { return without(c.FontOptions, c.Font) }

// RejectedModes returns the colour modes other than the chosen mode.
func (c Choice) RejectedModes() []string { return without(c.ModeOptions, c.Mode) }

// Inventory renders the discover_capabilities result for this seed: every
// option in served order, the true values undistinguished from their decoys.
func (c Choice) Inventory() string {
	return fmt.Sprintf("Appearance options: accent colors %s; fonts %s; %s modes.",
		strings.Join(c.AccentOptions, ", "), strings.Join(c.FontOptions, ", "), joinAnd(c.ModeOptions))
}

// options assembles the chosen value with n decoys. Near-misses — corpus
// entries sharing a whole word with the chosen value — fill the decoy slots
// first; uniform corpus draws fill the rest. No decoy may be graded as
// contained in the chosen value (grade.Hit), otherwise a correct answer would
// surface its own distractor. The result is shuffled so the true value has no
// fixed position.
func options(r *rand.Rand, chosen string, n int, corpus []string, draw func(*rand.Rand) string) []string {
	out := []string{chosen}
	seen := map[string]bool{strings.ToLower(chosen): true}
	admit := func(candidate string) bool {
		key := strings.ToLower(candidate)
		if seen[key] || grade.Hit(candidate, chosen) {
			return false
		}
		seen[key] = true
		out = append(out, candidate)
		return true
	}
	nearMisses := nearMissesOf(chosen, corpus)
	r.Shuffle(len(nearMisses), func(i, j int) { nearMisses[i], nearMisses[j] = nearMisses[j], nearMisses[i] })
	// Keep at most half the decoy slots for near-misses so the inventory is not
	// a single family a harness could pattern-match, then fill uniformly.
	for _, candidate := range nearMisses {
		if len(out) > n/2 {
			break
		}
		admit(candidate)
	}
	for attempts := 0; len(out) <= n && attempts < 4096; attempts++ {
		admit(draw(r))
	}
	if len(out) <= n {
		panic(fmt.Sprintf("appearance: could not assemble %d decoys around %q", n, chosen))
	}
	r.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out
}

// nearMissesOf lists corpus entries sharing at least one whole word with value
// (excluding value itself), in corpus order so the caller's shuffle is the only
// randomness.
func nearMissesOf(value string, corpus []string) []string {
	words := map[string]bool{}
	for _, w := range strings.Fields(strings.ToLower(value)) {
		if len(w) >= 3 {
			words[w] = true
		}
	}
	var out []string
	for _, candidate := range corpus {
		if strings.EqualFold(candidate, value) {
			continue
		}
		for _, w := range strings.Fields(strings.ToLower(candidate)) {
			if words[w] {
				out = append(out, candidate)
				break
			}
		}
	}
	sort.Strings(out)
	return out
}

func without(values []string, selected string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value != selected {
			out = append(out, value)
		}
	}
	return out
}

func joinAnd(values []string) string {
	switch len(values) {
	case 0:
		return ""
	case 1:
		return values[0]
	case 2:
		return values[0] + " and " + values[1]
	default:
		return strings.Join(values[:len(values)-1], ", ") + ", and " + values[len(values)-1]
	}
}

func streamSeed(seed int64, salt string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-appearance:%d:%s", seed, salt)
	return int64(h.Sum64() & ((1 << 63) - 1))
}
