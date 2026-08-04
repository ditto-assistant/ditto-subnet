// Package humandata exposes frozen, deterministic human-name data for V8.
//
// The benchmark never calls a live name service. The checked-in snapshots and
// their provenance are part of G(seed, version), so a public seed remains byte
// reproducible even when an upstream dataset changes.
package humandata

import (
	"bufio"
	_ "embed"
	"fmt"
	"math/rand"
	"strconv"
	"strings"
)

//go:embed data/given_names.tsv
var givenNamesTSV string

//go:embed data/surnames.tsv
var surnamesTSV string

//go:embed data/nicknames.tsv
var nicknamesTSV string

type weightedName struct {
	value  string
	weight int64
}

var (
	givenNames = parseWeighted(givenNamesTSV)
	surnames   = parseWeighted(surnamesTSV)
	nicknames  = parseNicknames(nicknamesTSV)
)

// GivenName returns a deterministic mixture of common and long-tail U.S.
// given names. Four of five draws follow observed frequency; every fifth draw
// samples uniformly from ranks 1,001-10,000 so uncommon and international
// names do not disappear beneath the head of the distribution.
func GivenName(r *rand.Rand, ordinal int) string {
	if ordinal%5 == 4 && len(givenNames) > 1000 {
		return givenNames[1000+r.Intn(len(givenNames)-1000)].value
	}
	limit := 1000
	if limit > len(givenNames) {
		limit = len(givenNames)
	}
	return weightedPick(r, givenNames[:limit])
}

// Surname returns a deterministic U.S. Census surname sample. Most draws use
// observed frequency, with a bounded long-tail stratum for broader coverage.
func Surname(r *rand.Rand, ordinal int) string {
	if ordinal%7 == 6 && len(surnames) > 1000 {
		return surnames[1000+r.Intn(len(surnames)-1000)].value
	}
	limit := 2000
	if limit > len(surnames) {
		limit = len(surnames)
	}
	return weightedPick(r, surnames[:limit])
}

// PreferredName returns a real, frozen diminutive when one is known. Names
// without a documented mapping use the given name unchanged; arbitrary
// truncation and token-shaped "nicknames" are never invented.
func PreferredName(given string, r *rand.Rand) string {
	options := nicknames[strings.ToLower(given)]
	if len(options) == 0 {
		return given
	}
	return options[r.Intn(len(options))]
}

// DistinctPreferredName returns a believable name that somebody might
// actually use for the person in conversation, while guaranteeing that it is
// not merely the given name repeated in a "nickname" field. Frozen documented
// diminutives win. When a corpus mapping is unavailable, a reviewed familiar
// contraction wins; every other name uses an ordinary social nickname (the
// "everyone at school called me Scout" shape).
//
// This helper is V8-only through its universe caller. PreferredName remains
// unchanged for every frozen pre-V8 generation path.
func DistinctPreferredName(given string, r *rand.Rand, ordinal int) string {
	return DistinctPreferredNameExcluding(given, r, ordinal, nil)
}

// DistinctPreferredNameExcluding preserves the same human-name policy while
// avoiding preferred names already assigned elsewhere in one generated world.
// A documented diminutive or familiar contraction still wins when available;
// the reviewed social-name pool provides the fallback needed to keep vague
// interpersonal references unambiguous at full scale.
func DistinctPreferredNameExcluding(given string, r *rand.Rand, ordinal int, excluded map[string]bool) string {
	var distinct []string
	for _, option := range nicknames[strings.ToLower(given)] {
		if !strings.EqualFold(option, given) && !excluded[strings.ToLower(option)] {
			distinct = append(distinct, option)
		}
	}
	if len(distinct) > 0 {
		return distinct[r.Intn(len(distinct))]
	}
	if short := informalShortForms[strings.ToLower(given)]; short != "" && !excluded[strings.ToLower(short)] {
		return short
	}
	start := (ordinal + r.Intn(len(socialNicknames))) % len(socialNicknames)
	for offset := range socialNicknames {
		candidate := socialNicknames[(start+offset)%len(socialNicknames)]
		if !strings.EqualFold(candidate, given) && !excluded[strings.ToLower(candidate)] {
			return candidate
		}
	}
	panic("humandata: social nickname corpus contains no available distinct value")
}

// These reviewed spoken contractions cover names where the frozen source lacks
// a mapping but the everyday shortening is unambiguous. Everything else uses a
// social nickname; the generator never guesses by chopping an arbitrary suffix.
var informalShortForms = map[string]string{
	"alexandria": "Alex",
	"arturo":     "Art",
	"johanna":    "Jo",
	"juliana":    "Jules",
	"niko":       "Nik",
	"ravi":       "Rav",
	"savanna":    "Sav",
}

var socialNicknames = []string{
	"Ace", "Bear", "Bee", "Birdie", "Blue", "Breezy", "Buddy", "Chip",
	"Coach", "Dash", "Doc", "Flash", "Goldie", "Happy", "Indy", "Jazz",
	"Junebug", "Kit", "Lucky", "Mack", "Midge", "Mo", "Nell", "Pip",
	"Red", "Scout", "Skip", "Sparky", "Stretch", "Sunny", "Teddy", "Wren",
}

// AllGivenNames returns the frozen 10,000-name vocabulary for V8-only persona
// attributes that need a broad, non-enumerable human-name surface.
func AllGivenNames() []string {
	out := make([]string, len(givenNames))
	for i, entry := range givenNames {
		out[i] = entry.value
	}
	return out
}

func weightedPick(r *rand.Rand, entries []weightedName) string {
	if len(entries) == 0 {
		panic("humandata: empty weighted corpus")
	}
	var total int64
	for _, entry := range entries {
		total += entry.weight
	}
	draw := r.Int63n(total)
	for _, entry := range entries {
		if draw < entry.weight {
			return entry.value
		}
		draw -= entry.weight
	}
	return entries[len(entries)-1].value
}

func parseWeighted(raw string) []weightedName {
	scanner := bufio.NewScanner(strings.NewReader(raw))
	if !scanner.Scan() || scanner.Text() != "name\tcount" {
		panic("humandata: invalid weighted-corpus header")
	}
	var out []weightedName
	for scanner.Scan() {
		parts := strings.Split(scanner.Text(), "\t")
		if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" {
			panic(fmt.Sprintf("humandata: invalid weighted row %q", scanner.Text()))
		}
		weight, err := strconv.ParseInt(parts[1], 10, 64)
		if err != nil || weight <= 0 {
			panic(fmt.Sprintf("humandata: invalid weight in %q", scanner.Text()))
		}
		out = append(out, weightedName{value: parts[0], weight: weight})
	}
	if err := scanner.Err(); err != nil {
		panic(fmt.Sprintf("humandata: scan weighted corpus: %v", err))
	}
	return out
}

func parseNicknames(raw string) map[string][]string {
	scanner := bufio.NewScanner(strings.NewReader(raw))
	if !scanner.Scan() || scanner.Text() != "name\tnickname" {
		panic("humandata: invalid nickname-corpus header")
	}
	out := map[string][]string{}
	for scanner.Scan() {
		parts := strings.Split(scanner.Text(), "\t")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			panic(fmt.Sprintf("humandata: invalid nickname row %q", scanner.Text()))
		}
		key := strings.ToLower(parts[0])
		out[key] = append(out[key], parts[1])
	}
	if err := scanner.Err(); err != nil {
		panic(fmt.Sprintf("humandata: scan nickname corpus: %v", err))
	}
	return out
}
