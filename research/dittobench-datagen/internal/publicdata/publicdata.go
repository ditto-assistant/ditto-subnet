// Package publicdata exposes frozen, deterministic public vocabulary corpora
// for DittoBench v13: places, occupations, type families, colour names, and
// organisation stems.
//
// Like internal/humandata, the tables are build inputs, not runtime lookups: the
// benchmark never calls a live dataset. Each checked-in snapshot is part of
// G(seed, version) and its SHA-256 is pinned by TestFrozenCorpusIdentity, so a
// public seed remains byte reproducible even when an upstream dataset changes.
//
// A public corpus is a bigger table, not an open set — a miner can vendor every
// one of these files. What it buys is variety and near-miss ambiguity ("Inter"
// vs "Inter Tight", "teal" vs "dark teal", two cities that share a first
// letter), not secrecy. Sampling follows the humandata pattern: frequency-
// weighted draws from the head of each distribution with an explicit long-tail
// stratum so rare entries do not vanish beneath the head.
package publicdata

import (
	"bufio"
	_ "embed"
	"fmt"
	"math/rand"
	"strconv"
	"strings"
)

//go:embed data/cities.tsv
var citiesTSV string

//go:embed data/occupations.tsv
var occupationsTSV string

//go:embed data/fonts.tsv
var fontsTSV string

//go:embed data/colors.tsv
var colorsTSV string

//go:embed data/org_stems.tsv
var orgStemsTSV string

//go:embed data/purposes.tsv
var purposesTSV string

type weightedEntry struct {
	value  string
	weight int64
}

// Purpose kinds accepted by Purpose and Purposes.
const (
	PurposeProject = "project"
	PurposeTrip    = "trip"
)

var (
	cities      = parseCities(citiesTSV)
	occupations = parseColumn(occupationsTSV, "title")
	fonts       = parseFonts(fontsTSV)
	colors      = parseColors(colorsTSV)
	orgStems    = parseColumn(orgStemsTSV, "stem")
	purposes    = parsePurposes(purposesTSV)
)

// companySuffixes is a deliberately closed enum: the legal/brand suffix that
// follows an organisation stem. It is the one small list in this package and is
// documented as such in SOURCES.md.
var companySuffixes = []string{
	"Studio", "Works", "Partners", "Labs", "Collective", "Group", "& Co.", "Company", "Guild", "",
	"Holdings", "Ventures", "Systems", "Logistics", "Foods", "Media", "Digital", "Design",
	"Consulting", "Analytics", "Supply", "Atelier", "Workshop", "Press", "Brands", "Industries",
	"Interactive", "Robotics", "Textiles", "Energy", "Capital", "Cooperative",
}

// City returns a populated-place name. Four of five draws follow population
// among the 2,000 largest cities; every fifth draw samples uniformly from the
// long tail so small cities and unfamiliar spellings stay in play.
func City(r *rand.Rand, ordinal int) string {
	const head = 2000
	if ordinal%5 == 4 && len(cities) > head {
		return cities[head+r.Intn(len(cities)-head)].value
	}
	limit := head
	if limit > len(cities) {
		limit = len(cities)
	}
	return weightedPick(r, cities[:limit])
}

// Occupation returns a lower-cased occupation title drawn uniformly.
func Occupation(r *rand.Rand) string {
	return occupations[r.Intn(len(occupations))]
}

// FontFamily returns a type-family name. Three of four draws are rank-weighted
// among the 300 most popular families; every fourth draw samples uniformly from
// the long tail.
func FontFamily(r *rand.Rand, ordinal int) string {
	const head = 300
	if ordinal%4 == 3 && len(fonts) > head {
		return fonts[head+r.Intn(len(fonts)-head)].value
	}
	limit := head
	if limit > len(fonts) {
		limit = len(fonts)
	}
	return weightedPick(r, fonts[:limit])
}

// Color returns a colour name drawn uniformly from the combined xkcd survey and
// CSS named-colour table.
func Color(r *rand.Rand) string {
	return colors[r.Intn(len(colors))]
}

// OrgStem returns a Title-cased organisation or product name stem drawn
// uniformly.
func OrgStem(r *rand.Rand) string {
	return orgStems[r.Intn(len(orgStems))]
}

// CompanySuffix returns one entry of the closed suffix enum (possibly empty).
func CompanySuffix(r *rand.Rand) string {
	return companySuffixes[r.Intn(len(companySuffixes))]
}

// Purpose returns a project or trip purpose drawn uniformly from the bank for
// kind (PurposeProject or PurposeTrip).
func Purpose(r *rand.Rand, kind string) string {
	bank := purposes[kind]
	if len(bank) == 0 {
		panic(fmt.Sprintf("publicdata: unknown purpose kind %q", kind))
	}
	return bank[r.Intn(len(bank))]
}

// AllCities returns every city name in table order (population descending).
func AllCities() []string { return values(cities) }

// AllOccupations returns every occupation title in table order.
func AllOccupations() []string { return append([]string(nil), occupations...) }

// AllFonts returns every font family in table order (popularity ascending rank).
func AllFonts() []string { return values(fonts) }

// AllColors returns every colour name in table order (alphabetical).
func AllColors() []string { return append([]string(nil), colors...) }

// AllOrgStems returns every organisation stem in table order.
func AllOrgStems() []string { return append([]string(nil), orgStems...) }

// CompanySuffixes returns the closed suffix enum.
func CompanySuffixes() []string { return append([]string(nil), companySuffixes...) }

// Purposes returns the purpose bank for kind.
func Purposes(kind string) []string { return append([]string(nil), purposes[kind]...) }

// Sizes reports every pool size, so tests can assert the "no pool below 500
// except closed enums" acceptance criterion in one place.
func Sizes() map[string]int {
	return map[string]int{
		"cities":           len(cities),
		"occupations":      len(occupations),
		"fonts":            len(fonts),
		"colors":           len(colors),
		"org_stems":        len(orgStems),
		"company_suffixes": len(companySuffixes),
		"purposes.project": len(purposes[PurposeProject]),
		"purposes.trip":    len(purposes[PurposeTrip]),
	}
}

func values(entries []weightedEntry) []string {
	out := make([]string, len(entries))
	for i, entry := range entries {
		out[i] = entry.value
	}
	return out
}

func weightedPick(r *rand.Rand, entries []weightedEntry) string {
	if len(entries) == 0 {
		panic("publicdata: empty weighted corpus")
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

func scanRows(raw, header string, fn func(parts []string)) {
	scanner := bufio.NewScanner(strings.NewReader(raw))
	if !scanner.Scan() || scanner.Text() != header {
		panic(fmt.Sprintf("publicdata: invalid corpus header, want %q", header))
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		fn(strings.Split(line, "\t"))
	}
	if err := scanner.Err(); err != nil {
		panic(fmt.Sprintf("publicdata: scan corpus: %v", err))
	}
}

func parseCities(raw string) []weightedEntry {
	var out []weightedEntry
	scanRows(raw, "name\tcountry\tpopulation", func(parts []string) {
		if len(parts) != 3 || parts[0] == "" {
			panic(fmt.Sprintf("publicdata: invalid city row %q", strings.Join(parts, "\t")))
		}
		population, err := strconv.ParseInt(parts[2], 10, 64)
		if err != nil || population <= 0 {
			panic(fmt.Sprintf("publicdata: invalid population in %q", strings.Join(parts, "\t")))
		}
		out = append(out, weightedEntry{value: parts[0], weight: population})
	})
	return out
}

func parseFonts(raw string) []weightedEntry {
	var rows []struct {
		family string
		rank   int64
	}
	scanRows(raw, "family\tcategory\tpopularity", func(parts []string) {
		if len(parts) != 3 || parts[0] == "" {
			panic(fmt.Sprintf("publicdata: invalid font row %q", strings.Join(parts, "\t")))
		}
		rank, err := strconv.ParseInt(parts[2], 10, 64)
		if err != nil || rank <= 0 {
			panic(fmt.Sprintf("publicdata: invalid popularity in %q", strings.Join(parts, "\t")))
		}
		rows = append(rows, struct {
			family string
			rank   int64
		}{parts[0], rank})
	})
	// Popularity is a rank (1 = most popular); weight the head so a more
	// popular family is proportionally likelier, never zero.
	out := make([]weightedEntry, 0, len(rows))
	for _, row := range rows {
		weight := int64(len(rows)) + 1 - row.rank
		if weight < 1 {
			weight = 1
		}
		out = append(out, weightedEntry{value: row.family, weight: weight})
	}
	return out
}

func parseColors(raw string) []string {
	var out []string
	scanRows(raw, "name\tsource", func(parts []string) {
		if len(parts) != 2 || parts[0] == "" {
			panic(fmt.Sprintf("publicdata: invalid colour row %q", strings.Join(parts, "\t")))
		}
		out = append(out, parts[0])
	})
	return out
}

func parseColumn(raw, header string) []string {
	var out []string
	scanRows(raw, header, func(parts []string) {
		if len(parts) != 1 || parts[0] == "" {
			panic(fmt.Sprintf("publicdata: invalid %s row %q", header, strings.Join(parts, "\t")))
		}
		out = append(out, parts[0])
	})
	return out
}

func parsePurposes(raw string) map[string][]string {
	out := map[string][]string{}
	scanRows(raw, "kind\tpurpose", func(parts []string) {
		if len(parts) != 2 || (parts[0] != PurposeProject && parts[0] != PurposeTrip) || parts[1] == "" {
			panic(fmt.Sprintf("publicdata: invalid purpose row %q", strings.Join(parts, "\t")))
		}
		out[parts[0]] = append(out[parts[0]], parts[1])
	})
	return out
}
