package catalog

import (
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/appearance"
)

// Bench v13 discovery inventories (issue #1842). A seed configures the
// appearance options of its workspace — accent colors and chat fonts drawn from
// public corpora (CSS/xkcd colour names, Google Fonts families) — and the mock
// discover_capabilities result is the ONLY place their canonical spelling
// exists. A discovery-grounded case names the option through a bounded
// misspelling (AliasFor) whose unique nearest listed option is the intended
// one with a margin of at least one edit, so an honest list-then-act agent
// resolves it while a baked 8-colour/4-font registry is wrong on most seeds.
//
// One near-miss pair is planted per inventory ("Inter" and "Inter Tight"). On
// the near-miss share of cases the prompt misspells the BASE member and adds the
// partner's qualifier ("the tight variant of Intr"), so the edit-distance-only
// picker resolves to the base while the intended option is the partner: the
// margin property still holds for the alias, and reading the qualifier is what
// separates the honest agent from the typo ladder.

// Inventory is one seed's configured appearance options in canonical spelling.
type Inventory struct {
	// Accents and Fonts are the listed options (canonical spelling), sorted
	// case-insensitively as the mock lists them.
	Accents []string
	Fonts   []string
	// AccentNearMiss / FontNearMiss are the planted near-miss pairs; both
	// members are listed.
	AccentNearMiss NearMiss
	FontNearMiss   NearMiss
	// accentTargets / fontTargets are the corpus draws a discovery-grounded case
	// may target (never a legacy pool value), in seeded order.
	accentTargets []string
	fontTargets   []string
}

// NearMiss is a planted option pair: Partner is Base plus a qualifier
// ("Inter" -> "Inter Tight", "mint" -> "mint green").
type NearMiss struct {
	Base      string
	Partner   string
	Qualifier string
}

const (
	inventoryAccentDraws = 8
	inventoryFontDraws   = 6
)

// legacyAccents is the v8..v12 8-colour pool (universe.colors). It stays listed
// so the capped world_theme_discover_set family remains honestly solvable, but
// it is never a discovery-grounded target.
var legacyAccents = []string{"teal", "indigo", "amber", "emerald", "crimson", "violet", "cobalt", "coral"}

// legacyFonts are the v8..v12 baked font pools (datagen chatFonts plus the
// universe preference fonts). Never a discovery target at v13; one may be
// listed only as a planted near-miss base ("Inter" beside "Inter Tight").
var legacyFonts = []string{"Inter", "JetBrains Mono", "Georgia", "system default", "Atkinson Hyperlegible", "Source Sans 3", "IBM Plex Sans", "Aptos"}

// InventoryForSeed draws the seed's workspace appearance options. Pure in seed.
func InventoryForSeed(seed int64) Inventory {
	inv := Inventory{}
	inv.AccentNearMiss = accentNearMissPairs[v13Pick(seed, "accent-near-miss", len(accentNearMissPairs))]
	inv.FontNearMiss = fontNearMissPairs[v13Pick(seed, "font-near-miss", len(fontNearMissPairs))]

	excludedAccents := map[string]bool{}
	for _, c := range legacyAccents {
		excludedAccents[strings.ToLower(c)] = true
	}
	for _, p := range accentNearMissPairs {
		excludedAccents[strings.ToLower(p.Base)] = true
		excludedAccents[strings.ToLower(p.Partner)] = true
	}
	inv.accentTargets = drawOptions(seed, "accent-draw", colourCorpus, excludedAccents, inventoryAccentDraws)

	excludedFonts := map[string]bool{}
	for _, f := range legacyFonts {
		excludedFonts[strings.ToLower(f)] = true
	}
	for _, p := range fontNearMissPairs {
		excludedFonts[strings.ToLower(p.Base)] = true
		excludedFonts[strings.ToLower(p.Partner)] = true
	}
	inv.fontTargets = drawOptions(seed, "font-draw", fontCorpus, excludedFonts, inventoryFontDraws)

	inv.Accents = append(append(append([]string(nil), legacyAccents...), inv.accentTargets...), inv.AccentNearMiss.Base, inv.AccentNearMiss.Partner)
	inv.Fonts = append(append([]string(nil), inv.fontTargets...), inv.FontNearMiss.Base, inv.FontNearMiss.Partner)
	// The ordinary world's preferences and tool discovery share one served
	// inventory. Preserve both producers' near-misses without duplicate names.
	choice := appearance.ForSeed(seed)
	merge := func(options, extra []string) []string {
		for _, value := range extra {
			found := false
			for _, option := range options {
				found = found || strings.EqualFold(option, value)
			}
			if !found {
				options = append(options, value)
			}
		}
		return options
	}
	inv.Accents = merge(inv.Accents, choice.AccentOptions)
	inv.Fonts = merge(inv.Fonts, choice.FontOptions)
	sortFold(inv.Accents)
	sortFold(inv.Fonts)
	return inv
}

// drawOptions samples n distinct corpus entries not in excluded, in seeded order.
func drawOptions(seed int64, key string, corpus []string, excluded map[string]bool, n int) []string {
	perm := v13Perm(seed, key, len(corpus))
	out := make([]string, 0, n)
	for _, idx := range perm {
		if len(out) == n {
			break
		}
		if excluded[strings.ToLower(corpus[idx])] {
			continue
		}
		out = append(out, corpus[idx])
	}
	return out
}

func sortFold(values []string) {
	sort.SliceStable(values, func(i, j int) bool {
		return strings.ToLower(values[i]) < strings.ToLower(values[j])
	})
}

// AccentTargets are the options a plain discovery-grounded accent case may
// target: the seed's corpus draws (never a legacy colour), seeded order.
func (inv Inventory) AccentTargets() []string { return append([]string(nil), inv.accentTargets...) }

// FontTargets are the options a plain discovery-grounded font case may target.
func (inv Inventory) FontTargets() []string { return append([]string(nil), inv.fontTargets...) }

// DiscoverText renders the appearance section of the mock discover_capabilities
// result: every listed option in canonical spelling.
func (inv Inventory) DiscoverText() string {
	return fmt.Sprintf("Appearance options for this workspace — accent colors: %s; chat fonts: %s; color modes: %s.",
		strings.Join(inv.Accents, ", "), strings.Join(inv.Fonts, ", "), strings.Join(ThemeEnum(), ", "))
}

// MatchAccent resolves a submitted accent value to its listed canonical
// spelling (case-insensitive, trimmed, exact). ok=false for anything unlisted —
// the caller must NOT echo a canonical spelling in that error.
func (inv Inventory) MatchAccent(value string) (string, bool) { return matchOption(inv.Accents, value) }

// MatchFont resolves a submitted font value to its listed canonical spelling.
func (inv Inventory) MatchFont(value string) (string, bool) { return matchOption(inv.Fonts, value) }

func matchOption(options []string, value string) (string, bool) {
	want := strings.ToLower(strings.Join(strings.Fields(value), " "))
	if want == "" {
		return "", false
	}
	for _, option := range options {
		if strings.ToLower(option) == want {
			return option, true
		}
	}
	return "", false
}

// ── Alias projection ─────────────────────────────────────────────────────────

// Alias is a bounded misspelling of a listed option.
type Alias struct {
	Text string
	// Edits applied (1..MaxEdits); Distance is the OSA distance to Base; Margin
	// is min over other listed options of d(alias, other) - Distance (>= 1).
	Edits    int
	Distance int
	Margin   int
}

// MaxAliasEdits is the edit budget for an option of the given length:
// floor(len/3) over the letters, at least 1.
func MaxAliasEdits(option string) int {
	letters := 0
	for _, r := range option {
		if r != ' ' {
			letters++
		}
	}
	edits := letters / 3
	if edits < 1 {
		edits = 1
	}
	if edits > 3 {
		edits = 3
	}
	return edits
}

// AliasFor derives the misspelled surface for base among options. It applies
// 1..MaxAliasEdits(base) seeded edits (keyboard-neighbour substitution,
// adjacent transposition, omission, duplication) and accepts the result only
// when base is the UNIQUE nearest listed option with a margin of at least one
// edit, the alias equals no listed option, and the alias does not contain the
// canonical spelling. Up to aliasAttempts salted retries; ok=false tells the
// caller to regenerate with another target.
func AliasFor(base string, options []string, seed int64, salt string) (Alias, bool) {
	maxEdits := MaxAliasEdits(base)
	for attempt := 0; attempt < aliasAttempts; attempt++ {
		key := fmt.Sprintf("alias:%s:%s:%d", salt, base, attempt)
		edits := 1 + v13Pick(seed, key+":edits", maxEdits)
		text := applyEdits(base, edits, seed, key)
		if strings.EqualFold(text, base) || strings.Contains(strings.ToLower(text), strings.ToLower(base)) {
			// A duplication at the edge ("dusty rosee") would carry the canonical
			// spelling verbatim inside the alias; the prompt must never do that.
			continue
		}
		distance := OSADistance(text, base)
		if distance < 1 || distance > maxEdits {
			continue
		}
		margin := 1 << 20
		collides := false
		for _, other := range options {
			if strings.EqualFold(other, base) {
				continue
			}
			if strings.EqualFold(other, text) {
				collides = true
				break
			}
			if m := OSADistance(text, other) - distance; m < margin {
				margin = m
			}
		}
		if collides || margin < 1 {
			continue
		}
		return Alias{Text: text, Edits: edits, Distance: distance, Margin: margin}, true
	}
	return Alias{}, false
}

const aliasAttempts = 64

// keyboardNeighbours is a QWERTY adjacency map for realistic substitutions.
var keyboardNeighbours = map[byte]string{
	'a': "qsz", 'b': "vgn", 'c': "xdv", 'd': "sfe", 'e': "wrd", 'f': "dgr", 'g': "fht",
	'h': "gjy", 'i': "uok", 'j': "hku", 'k': "jli", 'l': "ko", 'm': "nj", 'n': "bm",
	'o': "ipl", 'p': "ol", 'q': "wa", 'r': "etf", 's': "adw", 't': "ryg", 'u': "yij",
	'v': "cb", 'w': "qes", 'x': "zc", 'y': "tuh", 'z': "xa",
}

// applyEdits applies n seeded single-character edits to the letters of s
// (spaces and digits are never touched). Each edit is one OSA operation.
func applyEdits(s string, n int, seed int64, key string) string {
	out := []byte(s)
	for e := 0; e < n; e++ {
		positions := letterPositions(out)
		if len(positions) == 0 {
			break
		}
		step := fmt.Sprintf("%s:%d", key, e)
		pos := positions[v13Pick(seed, step+":pos", len(positions))]
		switch v13Pick(seed, step+":kind", 4) {
		case 0: // keyboard-neighbour substitution
			lower := toLowerByte(out[pos])
			neighbours := keyboardNeighbours[lower]
			if neighbours == "" {
				neighbours = "aeiou"
			}
			repl := neighbours[v13Pick(seed, step+":sub", len(neighbours))]
			if out[pos] >= 'A' && out[pos] <= 'Z' {
				repl -= 'a' - 'A'
			}
			out[pos] = repl
		case 1: // adjacent transposition (within the same word)
			if pos+1 < len(out) && isLetter(out[pos+1]) && toLowerByte(out[pos]) != toLowerByte(out[pos+1]) {
				out[pos], out[pos+1] = out[pos+1], out[pos]
			} else if pos > 0 && isLetter(out[pos-1]) && toLowerByte(out[pos]) != toLowerByte(out[pos-1]) {
				out[pos], out[pos-1] = out[pos-1], out[pos]
			} else {
				out[pos] = fallbackSubstitute(out[pos])
			}
		case 2: // omission (never empty a word)
			if wordLength(out, pos) > 2 {
				out = append(out[:pos], out[pos+1:]...)
			} else {
				out[pos] = fallbackSubstitute(out[pos])
			}
		default: // duplication
			out = append(out[:pos+1], out[pos:]...)
		}
	}
	return string(out)
}

func fallbackSubstitute(b byte) byte {
	lower := toLowerByte(b)
	repl := byte('x')
	if lower == 'x' {
		repl = 'z'
	}
	if b >= 'A' && b <= 'Z' {
		repl -= 'a' - 'A'
	}
	return repl
}

func letterPositions(s []byte) []int {
	out := make([]int, 0, len(s))
	for i, b := range s {
		if isLetter(b) {
			out = append(out, i)
		}
	}
	return out
}

func wordLength(s []byte, pos int) int {
	start, end := pos, pos
	for start > 0 && isLetter(s[start-1]) {
		start--
	}
	for end+1 < len(s) && isLetter(s[end+1]) {
		end++
	}
	return end - start + 1
}

func isLetter(b byte) bool { return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') }

func toLowerByte(b byte) byte {
	if b >= 'A' && b <= 'Z' {
		return b + ('a' - 'A')
	}
	return b
}

// OSADistance is the optimal-string-alignment (restricted Damerau-Levenshtein)
// distance over lowercased runes: substitution, insertion, deletion, and
// adjacent transposition each cost one edit — the same unit AliasFor spends.
func OSADistance(a, b string) int {
	ra, rb := []rune(strings.ToLower(a)), []rune(strings.ToLower(b))
	if len(ra) == 0 {
		return len(rb)
	}
	if len(rb) == 0 {
		return len(ra)
	}
	rows := make([][]int, len(ra)+1)
	for i := range rows {
		rows[i] = make([]int, len(rb)+1)
		rows[i][0] = i
	}
	for j := 0; j <= len(rb); j++ {
		rows[0][j] = j
	}
	for i := 1; i <= len(ra); i++ {
		for j := 1; j <= len(rb); j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			best := rows[i-1][j] + 1
			if v := rows[i][j-1] + 1; v < best {
				best = v
			}
			if v := rows[i-1][j-1] + cost; v < best {
				best = v
			}
			if i > 1 && j > 1 && ra[i-1] == rb[j-2] && ra[i-2] == rb[j-1] {
				if v := rows[i-2][j-2] + 1; v < best {
					best = v
				}
			}
			rows[i][j] = best
		}
	}
	return rows[len(ra)][len(rb)]
}

// ── Corpora ──────────────────────────────────────────────────────────────────
//
// Frozen with the contract: enlarging either list changes v13 bytes.

// accentNearMissPairs: Partner = Base + qualifier, both real colour names.
var accentNearMissPairs = []NearMiss{
	{Base: "mint", Partner: "mint green", Qualifier: "green"},
	{Base: "rose", Partner: "rose pink", Qualifier: "pink"},
	{Base: "olive", Partner: "olive drab", Qualifier: "drab"},
	{Base: "slate", Partner: "slate grey", Qualifier: "grey"},
	{Base: "sage", Partner: "sage green", Qualifier: "green"},
	{Base: "mustard", Partner: "mustard yellow", Qualifier: "yellow"},
	{Base: "pine", Partner: "pine green", Qualifier: "green"},
	{Base: "cornflower", Partner: "cornflower blue", Qualifier: "blue"},
	{Base: "lavender", Partner: "lavender blue", Qualifier: "blue"},
	{Base: "periwinkle", Partner: "periwinkle blue", Qualifier: "blue"},
	{Base: "navy", Partner: "navy blue", Qualifier: "blue"},
	{Base: "plum", Partner: "plum purple", Qualifier: "purple"},
	{Base: "forest", Partner: "forest green", Qualifier: "green"},
	{Base: "sky", Partner: "sky blue", Qualifier: "blue"},
}

// fontNearMissPairs: Partner = Base + qualifier, both Google Fonts families.
var fontNearMissPairs = []NearMiss{
	{Base: "Inter", Partner: "Inter Tight", Qualifier: "tight"},
	{Base: "Roboto", Partner: "Roboto Slab", Qualifier: "slab"},
	{Base: "Nunito", Partner: "Nunito Sans", Qualifier: "sans"},
	{Base: "Alegreya", Partner: "Alegreya Sans", Qualifier: "sans"},
	{Base: "Merriweather", Partner: "Merriweather Sans", Qualifier: "sans"},
	{Base: "Barlow", Partner: "Barlow Condensed", Qualifier: "condensed"},
	{Base: "Fira Sans", Partner: "Fira Sans Condensed", Qualifier: "condensed"},
	{Base: "Noto Sans", Partner: "Noto Sans Display", Qualifier: "display"},
	{Base: "Open Sans", Partner: "Open Sans Condensed", Qualifier: "condensed"},
	{Base: "Encode Sans", Partner: "Encode Sans Condensed", Qualifier: "condensed"},
	{Base: "Cormorant", Partner: "Cormorant Garamond", Qualifier: "garamond"},
	{Base: "Playfair Display", Partner: "Playfair Display SC", Qualifier: "small-caps"},
	{Base: "IBM Plex Sans", Partner: "IBM Plex Sans Condensed", Qualifier: "condensed"},
	{Base: "Zilla Slab", Partner: "Zilla Slab Highlight", Qualifier: "highlight"},
}

// colourCorpus is CSS named colours (spaced, lowercase) plus common xkcd names.
var colourCorpus = []string{
	"alice blue", "antique white", "aqua", "aquamarine", "azure", "beige", "bisque", "black",
	"blanched almond", "blue", "blue violet", "brown", "burlywood", "cadet blue", "chartreuse",
	"chocolate", "cornsilk", "cyan", "dark blue", "dark cyan", "dark goldenrod", "dark gray",
	"dark green", "dark khaki", "dark magenta", "dark olive green", "dark orange", "dark orchid",
	"dark red", "dark salmon", "dark sea green", "dark slate blue", "dark slate gray",
	"dark turquoise", "dark violet", "deep pink", "deep sky blue", "dim gray", "dodger blue",
	"firebrick", "floral white", "fuchsia", "gainsboro", "ghost white", "gold", "goldenrod",
	"gray", "green", "green yellow", "honeydew", "hot pink", "indian red", "ivory", "khaki",
	"lawn green", "lemon chiffon", "light blue", "light coral", "light cyan", "light goldenrod",
	"light gray", "light green", "light pink", "light salmon", "light sea green",
	"light sky blue", "light slate gray", "light steel blue", "light yellow", "lime",
	"lime green", "linen", "magenta", "maroon", "medium aquamarine", "medium blue",
	"medium orchid", "medium purple", "medium sea green", "medium slate blue",
	"medium spring green", "medium turquoise", "medium violet red", "midnight blue",
	"mint cream", "misty rose", "moccasin", "navajo white", "old lace", "orange", "orange red",
	"orchid", "pale goldenrod", "pale green", "pale turquoise", "pale violet red",
	"papaya whip", "peach puff", "peru", "pink", "powder blue", "purple", "rebecca purple",
	"red", "rosy brown", "royal blue", "saddle brown", "salmon", "sandy brown", "sea green",
	"seashell", "sienna", "silver", "slate blue", "snow", "spring green", "steel blue", "tan",
	"thistle", "tomato", "turquoise", "wheat", "white smoke", "yellow", "yellow green",
	// xkcd survey names
	"seafoam", "puce", "mauve", "ochre", "vermilion", "cerulean", "chartreuse green",
	"burnt orange", "burnt sienna", "dusty rose", "eggplant", "terracotta", "saffron",
	"marigold", "pistachio", "apricot", "raspberry", "tangerine", "wine", "brick red",
	"charcoal", "denim", "ultramarine", "aubergine", "moss green", "sand", "taupe",
	"blush", "cinnamon", "copper", "bronze", "pewter", "ivory black", "lilac", "heather",
	"fern", "jade", "sapphire", "ruby", "topaz", "amethyst", "garnet", "onyx", "pearl",
}

// fontCorpus is a slice of popular Google Fonts families.
var fontCorpus = []string{
	"Lato", "Montserrat", "Oswald", "Raleway", "Poppins", "Ubuntu", "Rubik", "Karla",
	"Manrope", "Mulish", "Quicksand", "Work Sans", "DM Sans", "Space Grotesk", "Sora",
	"Outfit", "Urbanist", "Figtree", "Plus Jakarta Sans", "Lexend", "Red Hat Display",
	"Red Hat Text", "Public Sans", "Archivo", "Archivo Narrow", "Cabin", "Catamaran",
	"Chivo", "Exo 2", "Heebo", "Hind", "Jost", "Kanit", "Libre Franklin", "Maven Pro",
	"Muli", "Nanum Gothic", "Overpass", "Oxygen", "PT Sans", "PT Serif", "Prompt",
	"Questrial", "Signika", "Titillium Web", "Varela Round", "Yantramanav", "Assistant",
	"Asap", "Bitter", "Cardo", "Crimson Text", "Crimson Pro", "Domine", "EB Garamond",
	"Frank Ruhl Libre", "Gelasio", "Libre Baskerville", "Libre Caslon Text", "Lora",
	"Neuton", "Old Standard TT", "Prata", "Spectral", "Vollkorn", "Zilla Slab Highlight",
	"Arvo", "Josefin Slab", "Josefin Sans", "Slabo 27px", "Fira Code", "Fira Mono",
	"IBM Plex Mono", "Inconsolata", "Roboto Mono", "Source Code Pro", "Space Mono",
	"Ubuntu Mono", "Courier Prime", "DM Mono", "Overpass Mono", "Anonymous Pro",
	"Cousine", "Cutive Mono", "Nova Mono", "PT Mono", "Share Tech Mono", "Abril Fatface",
	"Bebas Neue", "Comfortaa", "Dancing Script", "Great Vibes", "Lobster", "Pacifico",
	"Righteous", "Satisfy", "Shadows Into Light", "Amatic SC", "Caveat", "Indie Flower",
	"Kalam", "Patrick Hand", "Permanent Marker", "Alfa Slab One", "Anton", "Archivo Black",
	"Bangers", "Fjalla One", "Francois One", "Passion One", "Russo One", "Teko", "Barlow Semi Condensed",
	"Saira", "Saira Condensed", "Sarabun", "Tajawal", "Cairo", "Almarai", "Noto Serif",
	"Noto Sans Mono", "Fraunces", "Newsreader", "Literata", "Piazzolla", "Petrona",
}
