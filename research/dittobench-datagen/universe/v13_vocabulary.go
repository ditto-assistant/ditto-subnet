package universe

import (
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
)

// Bench v13 vocabulary (#1825). Every helper here replaces one hand list the
// v8..v12 world drew from (12 relations, 12 roles, 12 cities, 12 contexts, 18x12
// coined company stems, 24 project-name families, 10x8 trip aliases, 8 project
// and 8 trip purposes) with the frozen public corpora in internal/publicdata or
// a compositional bank whose product is at least 500 surfaces. They are reached
// only through GenerateForVersion at bench_version >= 13, so the frozen world
// bytes never move.

// relationKinds x relationQualifiers is the compositional relationship bank
// (35 x 16 = 560 surfaces). Roughly a third of relations carry no qualifier so
// the surface is not uniformly "<kind> from <place>".
var relationKinds = []string{
	"cousin", "neighbor", "former roommate", "accountant", "bookkeeper", "old manager", "family friend",
	"client contact", "design collaborator", "event producer", "sister-in-law", "brother-in-law",
	"godparent", "former colleague", "mentor", "mentee", "landlord", "tenant", "co-founder", "business partner",
	"physiotherapist", "piano teacher", "old classmate", "teammate", "bandmate", "carpool partner", "book-club friend",
	"climbing partner", "former intern", "contractor", "photographer", "translator", "editor", "financial adviser", "neighbor's daughter",
}

var relationQualifiers = []string{
	"", "", "", "", "", "from university", "from the running club", "from the choir", "from my first job",
	"from the co-op", "from the studio", "from the allotment", "from the language class", "from the sailing club",
	"from the old neighborhood", "from the volunteer crew",
}

func corpusRelation(r *rand.Rand) string {
	kind := relationKinds[r.Intn(len(relationKinds))]
	qualifier := relationQualifiers[r.Intn(len(relationQualifiers))]
	if qualifier == "" {
		return kind
	}
	return kind + " " + qualifier
}

// contextEventNouns names the event an organisation stem hosts; seasons frame
// the purpose-shaped alternative. Both forms are products of a corpus and a
// bank, so the reachable surface is thousands of distinct contexts.
var contextEventNouns = []string{
	"launch", "conference", "workshop", "showcase", "retreat", "summit", "open day", "residency",
	"benefit", "premiere", "pilot", "symposium", "expo", "hackathon", "roadshow", "fundraiser",
}

var contextSeasons = []string{"winter", "spring", "summer", "autumn", "2024", "2025", "midsummer", "year-end"}

func corpusContext(r *rand.Rand) string {
	if r.Intn(2) == 0 {
		return publicdata.OrgStem(r) + " " + contextEventNouns[r.Intn(len(contextEventNouns))]
	}
	return contextSeasons[r.Intn(len(contextSeasons))] + " " + publicdata.Purpose(r, publicdata.PurposeProject)
}

// corpusCompany joins a Wikidata organisation stem to one of the closed legal
// suffixes: "Talgo Holdings", "Orion Studio", "Piper".
func corpusCompany(r *rand.Rand) string {
	return strings.TrimSpace(publicdata.OrgStem(r) + " " + publicdata.CompanySuffix(r))
}

// projectNameSuffixPairs are the confusable second words a project's formal
// name and its shorthand alias take. Paired with a corpus stem they keep the
// "Harborline / Harborlight" property (projectNamesAreRelated) with a
// stems x pairs product far above the old 24 hand families.
var projectNameSuffixPairs = [][2]string{
	{"Line", "Light"}, {"View", "Side"}, {"Star", "Point"}, {"Field", "Gate"}, {"Haven", "House"},
	{"Stone", "Shore"}, {"Bridge", "Brook"}, {"Wood", "Wick"}, {"Grove", "Lane"}, {"Row", "Road"},
	{"Hall", "Hill"}, {"Ridge", "Reach"}, {"Park", "Path"}, {"Court", "Cross"}, {"Mill", "Mile"},
	{"Bay", "Bank"},
}

func uniqueCorpusProjectIdentity(r *rand.Rand, seenNames, seenAliases map[string]bool) (string, string) {
	prefixes := []string{"Project", "Program", "Initiative", "Campaign"}
	nouns := []string{"brief", "ledger", "workstream", "rollout", "plan", "review", "track", "file"}
	for {
		stem := publicdata.OrgStem(r)
		pair := projectNameSuffixPairs[r.Intn(len(projectNameSuffixPairs))]
		name := prefixes[r.Intn(len(prefixes))] + " " + stem + " " + pair[0]
		alias := strings.ToLower(stem + " " + pair[1] + " " + nouns[r.Intn(len(nouns))])
		if !seenNames[name] && !seenAliases[alias] {
			seenNames[name] = true
			seenAliases[alias] = true
			return name, alias
		}
	}
}

// tripRouteNouns pairs with a single-word corpus colour ("cobalt loop",
// "sienna crossing"), giving hundreds of colour words x 16 nouns.
var tripRouteNouns = []string{
	"loop", "route", "circuit", "run", "trail", "path", "line", "crossing",
	"ramble", "traverse", "passage", "arc", "sweep", "ring", "drift", "track",
}

// tripAliasForbidden are purpose-shaped words a trip alias must never contain
// (the alias must not answer or contradict the purpose question).
var tripAliasForbidden = map[string]bool{"trip": true, "tour": true, "museum": true, "food": true, "family": true, "festival": true, "archive": true, "research": true}

func corpusTripAlias(r *rand.Rand) string {
	for {
		color := publicdata.Color(r)
		if strings.ContainsAny(color, " -'") || tripAliasForbidden[color] {
			continue
		}
		return strings.ToLower(color + " " + tripRouteNouns[r.Intn(len(tripRouteNouns))])
	}
}
