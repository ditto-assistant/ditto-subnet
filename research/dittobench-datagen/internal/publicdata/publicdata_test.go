package publicdata

import (
	"crypto/sha256"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
)

// TestFrozenCorpusIdentity pins every embedded table to the SHA-256 recorded in
// data/SOURCES.md. Corpus drift is a contract change: a moved hash must ship as
// a new bench_version, never as a silent refresh.
func TestFrozenCorpusIdentity(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		want string
	}{
		{"cities", citiesTSV, "f3c93ac6accb20821d0ed13e0ebdf1fdad18e35adafe13e83fea818c2c1c6004"},
		{"occupations", occupationsTSV, "cbf91627ace1b935da67eb8be3bc6a9d22334990f86da788d4b95e381b7b4649"},
		{"fonts", fontsTSV, "8029e779b6ebd5a69175caa3e46bbe2d38aa05b59047a84fcd9c89467f334cc3"},
		{"colors", colorsTSV, "ce308f2165e03d1f51238923b4895e4a15967add6cded1579f9a8a72996edca0"},
		{"org_stems", orgStemsTSV, "d683c01a72bd537fa7d5290091a37f3643405ed37ca38e1516abf5001bcd5367"},
		{"purposes", purposesTSV, "78057f19f5e77d3423747df31635b62a857662b310880e3e3ca959ca6650d60d"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := fmt.Sprintf("%x", sha256.Sum256([]byte(tt.raw)))
			if got != tt.want {
				t.Fatalf("corpus drift: got %s want %s", got, tt.want)
			}
		})
	}
}

// TestOrgStemsExcludeNamesPlacesAndHouseholdBrands pins the second-stage
// exclusions of tools/freeze.py (refine_org_stems): no organisation stem is a
// persona given name or surname, a world city, or one of the household brands,
// places, and sensitive labels the review named. A stem that collides with a
// name or place would turn a fictional employer into a real one.
func TestOrgStemsExcludeNamesPlacesAndHouseholdBrands(t *testing.T) {
	excluded := map[string]bool{}
	for _, name := range humandata.AllGivenNames() {
		excluded[strings.ToLower(name)] = true
	}
	for _, name := range humandata.AllSurnames() {
		excluded[strings.ToLower(name)] = true
	}
	for _, city := range AllCities() {
		excluded[strings.ToLower(city)] = true
	}
	for _, reviewed := range []string{
		"Gucci", "Toshiba", "Sony", "Uber", "Rolex", "Volkswagen", "Pepsico", "Unilever",
		"Robert", "Louis", "Duke", "Brown", "Boston", "Seoul", "Singapore", "Qatar",
		"Malaysia", "Leipzig", "Uppsala", "Norwegian", "Austrian", "Deutsche",
		"Xvideos", "Trump", "Islamic", "Anthropic",
	} {
		excluded[strings.ToLower(reviewed)] = true
	}
	for _, stem := range AllOrgStems() {
		if excluded[strings.ToLower(stem)] {
			t.Fatalf("org stem %q is a person, place, or reviewed household name", stem)
		}
	}
}

// TestNoOpenPoolBelowFiveHundred is the v13 acceptance criterion: every
// vocabulary pool that stands in for a hand list carries at least 500 entries.
// Only the closed enums (company suffixes) and the authored purpose bank (the
// issue's own floor is 100) are exempt.
func TestNoOpenPoolBelowFiveHundred(t *testing.T) {
	sizes := Sizes()
	for _, pool := range []string{"cities", "occupations", "fonts", "colors", "org_stems"} {
		if sizes[pool] < 500 {
			t.Fatalf("pool %s has %d entries, want >= 500", pool, sizes[pool])
		}
	}
	if sizes["purposes.project"]+sizes["purposes.trip"] < 100 {
		t.Fatalf("purpose bank has %d entries, want >= 100", sizes["purposes.project"]+sizes["purposes.trip"])
	}
	if sizes["company_suffixes"] < 8 {
		t.Fatalf("company suffix enum has %d entries", sizes["company_suffixes"])
	}
}

func TestTablesAreCleanAndDeduplicated(t *testing.T) {
	check := func(name string, entries []string) {
		seen := map[string]bool{}
		for _, entry := range entries {
			if strings.TrimSpace(entry) != entry || entry == "" {
				t.Fatalf("%s entry %q is not trimmed", name, entry)
			}
			key := strings.ToLower(entry)
			if seen[key] {
				t.Fatalf("%s repeats %q", name, entry)
			}
			seen[key] = true
			for _, r := range entry {
				if r > 127 {
					t.Fatalf("%s entry %q is not ASCII", name, entry)
				}
			}
		}
	}
	check("cities", AllCities())
	check("occupations", AllOccupations())
	check("fonts", AllFonts())
	check("colors", AllColors())
	check("org_stems", AllOrgStems())
	check("purposes.project", Purposes(PurposeProject))
	check("purposes.trip", Purposes(PurposeTrip))
	for _, occupation := range AllOccupations() {
		if occupation != strings.ToLower(occupation) {
			t.Fatalf("occupation %q is not lower-cased", occupation)
		}
	}
	for _, stem := range AllOrgStems() {
		if strings.ContainsAny(stem, " -'.") || strings.ToUpper(stem[:1]) != stem[:1] {
			t.Fatalf("org stem %q is not a single Title-cased token", stem)
		}
	}
}

// TestNearMissPairsExist documents the property the corpora buy: confusable
// neighbours that a hand list of six or eight entries never had.
func TestNearMissPairsExist(t *testing.T) {
	fonts := map[string]bool{}
	for _, f := range AllFonts() {
		fonts[f] = true
	}
	if !fonts["Inter"] || !fonts["Inter Tight"] {
		t.Fatal("font corpus lacks the Inter / Inter Tight near-miss pair")
	}
	colors := map[string]bool{}
	for _, c := range AllColors() {
		colors[c] = true
	}
	if !colors["teal"] || !colors["dark teal"] || !colors["teal green"] {
		t.Fatal("colour corpus lacks the teal near-miss family")
	}
	for _, unpleasant := range []string{"shit", "puke", "vomit", "piss", "diarrhea"} {
		for c := range colors {
			if strings.Contains(" "+c+" ", " "+unpleasant+" ") {
				t.Fatalf("colour corpus retained %q", c)
			}
		}
	}
}

func TestSamplingIsDeterministicAndCoversTheLongTail(t *testing.T) {
	a, b := rand.New(rand.NewSource(1234)), rand.New(rand.NewSource(1234))
	for i := 0; i < 200; i++ {
		if City(a, i) != City(b, i) || FontFamily(a, i) != FontFamily(b, i) || Color(a) != Color(b) ||
			OrgStem(a) != OrgStem(b) || Occupation(a) != Occupation(b) || CompanySuffix(a) != CompanySuffix(b) ||
			Purpose(a, PurposeProject) != Purpose(b, PurposeProject) || Purpose(a, PurposeTrip) != Purpose(b, PurposeTrip) {
			t.Fatalf("sampling drift at draw %d", i)
		}
	}
	head := map[string]bool{}
	for _, city := range AllCities()[:2000] {
		head[city] = true
	}
	tail := rand.New(rand.NewSource(99))
	for i := 4; i < 200; i += 5 {
		if got := City(tail, i); head[got] {
			t.Fatalf("long-tail city draw %q came from the head", got)
		}
	}
	fontHead := map[string]bool{}
	for _, font := range AllFonts()[:300] {
		fontHead[font] = true
	}
	for i := 3; i < 200; i += 4 {
		if got := FontFamily(tail, i); fontHead[got] {
			t.Fatalf("long-tail font draw %q came from the head", got)
		}
	}
	distinct := map[string]bool{}
	for i := 0; i < 400; i++ {
		distinct[Color(tail)] = true
	}
	if len(distinct) < 250 {
		t.Fatalf("400 colour draws produced only %d distinct names", len(distinct))
	}
}
