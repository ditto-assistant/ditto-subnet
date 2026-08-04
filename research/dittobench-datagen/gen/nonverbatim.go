package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 6 non-verbatim / computed answers (plan 4.3): break the
// cleartext-substring property. The load-bearing fact is stated in one unit and the
// question asks it in another, so the answer token appears in NO seeded pair — a
// literal grep fails, but a reader who converts succeeds. The answer stays
// deterministically gradeable via the accept-set (protocol.MemoryCase.AcceptAny):
// every equivalent surface form of the converted value is accepted, and CRUCIALLY
// the original stored form is NOT in the accept-set, so a parser that echoes the
// stored "90 minutes" without converting scores zero.
//
// Deterministic per (seed, draw position); v6-gated so v5's bytes are untouched.

const QTNonVerbatim = "nonverbatim-computed"

type nonVerbatimSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func nonVerbatimEnabled(benchVersion int) bool { return benchVersion >= protocol.BenchVersionV6 }

// nonVerbatimCasesFor is the seed-independent case quota. Each case seeds one fact
// (stated in the un-asked unit) and asks the converted value.
func nonVerbatimCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 5
	case n >= 70:
		return 3
	case n >= 40:
		return 2
	default:
		return 0
	}
}

func nonVerbatimCasesForVersion(n, nWaves, benchVersion int) int {
	if benchVersion >= protocol.BenchVersionV8 {
		switch {
		case nWaves < 2:
			return 0
		case n >= 100:
			// V8 spends the rest of this former scalar-conversion budget on
			// shared-world joins and messy business reconciliation. Fifteen keeps
			// every conversion domain represented without crowding the fixed v7
			// runtime envelope.
			return 15
		case n >= 40:
			return 2
		default:
			return 0
		}
	}
	return nonVerbatimCasesFor(n, nWaves)
}

// convSpec is a stated fact whose answer requires a unit conversion. stored is how
// the user states it (the un-asked unit); ask asks for the converted unit; accept is
// the set of correct converted surface forms; the stored form is deliberately absent
// from accept so an un-converted echo fails.
type convSpec struct {
	stored []string // how the user states the fact (fmt: %s = the stored quantity phrase)
	ask    []string // question surfaces
	// entries pair a concrete stored quantity phrase with its converted accept-set.
	entries []convEntry
	// v8Contexts disambiguate repeated instances of the same conversion domain.
	// The stored fact and question share a natural subject anchor, so a memory
	// graph can contain several trips, commutes, orders, shelves, or jobs without
	// making the question under-specified. Earlier contracts ignore this field.
	v8Contexts []convContext
}

type convEntry struct {
	storedQty     string   // "90-minute", "a 90 minute"
	accept        []string // converted forms; NONE a substring of storedQty
	componentDays []int    // v8 multi-leg trip durations; empty for scalar conversions
}

type convContext struct {
	stored string
	ask    string
	// entries overrides the domain's scalar entries when the context requires a
	// genuinely composed answer. V8 trip contexts state separate country legs;
	// their accepted answer is the sum, never a total copied from the memory.
	entries []convEntry
}

var convSpecs = []convSpec{
	{ // minutes -> hours
		stored: []string{
			"#lead# my %s train is how I get to work.#trail#",
			"#lead# I take the %s train each way.#trail#",
		},
		ask: []string{
			"How many hours is my commute each way?",
			"In hours, how long is my train commute?",
			"My commute — how long is that in hours?",
		},
		entries: []convEntry{
			{storedQty: "30-minute", accept: []string{"0.5 hours", "0.5 hour", "half an hour", "a half hour", "0.5"}},
			{storedQty: "45-minute", accept: []string{"0.75 hours", "three quarters of an hour", "three-quarters of an hour", "0.75"}},
			{storedQty: "90-minute", accept: []string{"1.5 hours", "1.5 hour", "an hour and a half", "one and a half hours", "1.5"}},
			{storedQty: "150-minute", accept: []string{"2.5 hours", "two and a half hours", "two and a half", "2.5"}},
		},
		v8Contexts: []convContext{
			{stored: "#lead# my weekday train from Beacon to Manhattan takes %s each way.#trail#", ask: "How many hours is my weekday train from Beacon to Manhattan each way?"},
			{stored: "#lead# the morning train I take from Stamford to Grand Central is a %s ride.#trail#", ask: "In hours, how long is my morning ride from Stamford to Grand Central?"},
			{stored: "#lead# my commute from Princeton Junction to Penn Station uses a %s train.#trail#", ask: "How many hours is my train commute from Princeton Junction to Penn Station?"},
			{stored: "#lead# the train from Providence to Boston for my office days takes %s.#trail#", ask: "In hours, how long is my Providence-to-Boston office commute?"},
			{stored: "#lead# my hospital-shift commute from New Haven is a %s train journey.#trail#", ask: "How many hours is my New Haven train commute for hospital shifts?"},
			{stored: "#lead# the train I use from Durham to Raleigh for classes takes %s each way.#trail#", ask: "In hours, how long is my Durham-to-Raleigh commute for classes?"},
		},
	},
	{ // dozens -> units
		stored: []string{
			"#lead# I ordered %s of the good candles.#trail#",
			"#lead# I picked up %s of those candles.#trail#",
		},
		ask: []string{
			"How many candles did I order, as a number?",
			"In total units, how many candles did I get?",
			"What's the total count of candles I ordered?",
		},
		entries: []convEntry{
			{storedQty: "two dozen", accept: []string{"24 candles", "24 units", "24"}},
			{storedQty: "three dozen", accept: []string{"36 candles", "36 units", "36"}},
			{storedQty: "half a dozen", accept: []string{"6 candles", "6 units", "six candles", "6"}},
			{storedQty: "a dozen and a half", accept: []string{"18 candles", "18 units", "18"}},
		},
		v8Contexts: []convContext{
			{stored: "#lead# I ordered %s amber tapers for my sister's autumn wedding.#trail#", ask: "How many amber tapers did I order for my sister's autumn wedding?"},
			{stored: "#lead# I picked up %s beeswax candles for the neighborhood dinner.#trail#", ask: "As a number, how many beeswax candles did I get for the neighborhood dinner?"},
			{stored: "#lead# I bought %s white pillar candles for the theater production.#trail#", ask: "What is the total count of white pillar candles for the theater production?"},
			{stored: "#lead# I ordered %s blue votives for the lakeside memorial.#trail#", ask: "How many blue votives did I order for the lakeside memorial?"},
			{stored: "#lead# I got %s cedar candles for the winter market stall.#trail#", ask: "In total units, how many cedar candles are for my winter market stall?"},
			{stored: "#lead# I picked up %s citronella candles for the family reunion patio.#trail#", ask: "What is the total number of citronella candles for the family reunion patio?"},
		},
	},
	{ // weeks -> days
		stored: []string{
			"#lead# my trip is %s long.#trail#",
			"#lead# I'll be away for %s.#trail#",
		},
		ask: []string{
			"How many days is my trip?",
			"In days, how long is my trip?",
			"My trip length — how many days is that?",
		},
		entries: []convEntry{
			{storedQty: "two weeks", accept: []string{"14 days", "fourteen days", "14"}},
			{storedQty: "three weeks", accept: []string{"21 days", "twenty-one days", "21"}},
			{storedQty: "a week and a half", accept: []string{"10 days", "ten days", "10"}},
			{storedQty: "four weeks", accept: []string{"28 days", "twenty-eight days", "28"}},
		},
		v8Contexts: []convContext{
			{stored: "#lead# for last spring's food trip, I spent %s.#trail#", ask: "How many days was my food trip through France and Spain last spring?", entries: tripLegEntries("France", "Spain")},
			{stored: "#lead# for my autumn hiking trip, I planned %s.#trail#", ask: "In days, how long is my autumn hiking trip across Switzerland and Italy altogether?", entries: tripLegEntries("Switzerland", "Italy")},
			{stored: "#lead# last year's museum trip included %s.#trail#", ask: "How many days did my museum trip through the Netherlands and Belgium last overall?", entries: tripLegEntries("the Netherlands", "Belgium")},
			{stored: "#lead# for my cherry-blossom trip, I planned %s.#trail#", ask: "How many days is my cherry-blossom trip through Japan and South Korea?", entries: tripLegEntries("Japan", "South Korea")},
			{stored: "#lead# for the wildlife research trip, I allocated %s.#trail#", ask: "In days, how long is my wildlife research trip through Kenya and Tanzania in total?", entries: tripLegEntries("Kenya", "Tanzania")},
			{stored: "#lead# for the music festivals, I set aside %s.#trail#", ask: "How many days is my music-festival trip through Portugal and Spain?", entries: tripLegEntries("Portugal", "Spain")},
		},
	},
}

func tripLegEntries(first, second string) []convEntry {
	return []convEntry{
		{storedQty: fmt.Sprintf("one week in %s and one week in %s", first, second), accept: []string{"14 days", "fourteen days", "14"}, componentDays: []int{7, 7}},
		{storedQty: fmt.Sprintf("two weeks in %s and one week in %s", first, second), accept: []string{"21 days", "twenty-one days", "21"}, componentDays: []int{14, 7}},
		{storedQty: fmt.Sprintf("two weeks in %s and two weeks in %s", first, second), accept: []string{"28 days", "twenty-eight days", "28"}, componentDays: []int{14, 14}},
	}
}

// convSpecsV7Extra are ADDITIONAL non-verbatim domains available only under v7
// (appended to convSpecs by convSpecsForVersion). They must stay out of the base
// convSpecs slice: buildNonVerbatim draws r.Perm(len(specs)), so growing the
// base pool would perturb the v6 RNG stream and move v6's frozen bytes.
var convSpecsV7Extra = []convSpec{
	{ // feet -> inches
		stored: []string{
			"#lead# the bookshelf I'm building is %s tall.#trail#",
			"#lead# my new bookshelf stands %s.#trail#",
		},
		ask: []string{
			"How tall is my bookshelf in inches?",
			"In inches, how tall is the bookshelf?",
			"The bookshelf height — what's that in inches?",
		},
		entries: []convEntry{
			{storedQty: "4 feet", accept: []string{"48 inches", "forty-eight inches", "48"}},
			{storedQty: "5 feet", accept: []string{"60 inches", "sixty inches", "60"}},
			{storedQty: "six feet", accept: []string{"72 inches", "seventy-two inches", "72"}},
			{storedQty: "three and a half feet", accept: []string{"42 inches", "forty-two inches", "42"}},
		},
		v8Contexts: []convContext{
			{stored: "#lead# the walnut bookshelf for my study will be %s tall.#trail#", ask: "How tall will the walnut bookshelf in my study be, in inches?"},
			{stored: "#lead# the oak bookshelf beside the living-room fireplace stands %s.#trail#", ask: "In inches, how tall is the oak bookshelf by my living-room fireplace?"},
			{stored: "#lead# the pine bookshelf for my daughter's room is %s tall.#trail#", ask: "What is the height in inches of the pine bookshelf for my daughter's room?"},
			{stored: "#lead# the narrow bookshelf in my kitchen is %s tall.#trail#", ask: "How many inches tall is the narrow bookshelf in my kitchen?"},
			{stored: "#lead# the birch bookshelf for the studio wall stands %s.#trail#", ask: "In inches, how tall is the birch bookshelf for the studio wall?"},
			{stored: "#lead# the painted bookshelf in the guest room will be %s tall.#trail#", ask: "What is the painted guest-room bookshelf's height in inches?"},
		},
	},
	{ // years -> months
		stored: []string{
			"#lead# I've been at my job for %s now.#trail#",
			"#lead# my tenure at the company is %s.#trail#",
		},
		ask: []string{
			"How many months have I been at my job?",
			"In months, how long have I been at the company?",
			"My job tenure — how many months is that?",
		},
		entries: []convEntry{
			{storedQty: "two years", accept: []string{"24 months", "twenty-four months", "24"}},
			{storedQty: "a year and a half", accept: []string{"18 months", "eighteen months", "18"}},
			{storedQty: "three years", accept: []string{"36 months", "thirty-six months", "36"}},
			{storedQty: "half a year", accept: []string{"6 months", "six months", "6"}},
		},
		v8Contexts: []convContext{
			{stored: "#lead# I've worked as a product designer at Northstar Labs for %s.#trail#", ask: "How many months have I been a product designer at Northstar Labs?"},
			{stored: "#lead# my research job at the maritime museum has lasted %s.#trail#", ask: "In months, how long have I held my research job at the maritime museum?"},
			{stored: "#lead# I've been on the payments team at Cedar Bank for %s.#trail#", ask: "How many months have I worked on Cedar Bank's payments team?"},
			{stored: "#lead# my teaching role at Eastview College is %s old now.#trail#", ask: "In months, how long have I taught at Eastview College?"},
			{stored: "#lead# I've worked in the conservation lab at the city archive for %s.#trail#", ask: "How many months have I worked in the city archive's conservation lab?"},
			{stored: "#lead# my operations role at Harbor Transit has lasted %s.#trail#", ask: "In months, how long have I held my operations role at Harbor Transit?"},
		},
	},
}

// convSpecsForVersion returns the base non-verbatim pool for pre-v7 contracts
// (so their RNG stream and bytes are unchanged) and the base + v7 extras for v7.
func convSpecsForVersion(benchVersion int) []convSpec {
	if benchVersion >= protocol.BenchVersionV7 {
		out := make([]convSpec, 0, len(convSpecs)+len(convSpecsV7Extra))
		out = append(out, convSpecs...)
		out = append(out, convSpecsV7Extra...)
		return out
	}
	return convSpecs
}

// buildNonVerbatim generates the v6 non-verbatim / computed-answer suite.
func buildNonVerbatim(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves, benchVersion int) nonVerbatimSuite {
	var out nonVerbatimSuite
	nCases := nonVerbatimCasesForVersion(n, nWaves, benchVersion)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	leadG := persona.Grammar{
		"lead":  {"", "By the way,", "Quick note:", "Oh,", "For context,"},
		"trail": {"", " Anyway.", " Just noting it.", " Thought I'd mention."},
	}
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-nv-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-nv-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	specs := convSpecsForVersion(benchVersion)
	specPerm := r.Perm(len(specs))
	for c := 0; c < nCases; c++ {
		spec := specs[specPerm[c%len(specPerm)]]
		entry := spec.entries[r.Intn(len(spec.entries))]
		// State the fact in the un-asked unit (grammar-varied lead/trail around the
		// stored template's %s = the stored quantity phrase).
		tmpl := spec.stored[r.Intn(len(spec.stored))]
		question := ""
		if benchVersion >= protocol.BenchVersionV8 && len(spec.v8Contexts) > 0 {
			// nCases can exceed the number of conversion domains. Give each
			// repeated domain a distinct, seed-rotated subject instead of placing
			// several facts behind the same generic question ("my trip", "my
			// commute", and so on). There are six contexts because full v8 draws
			// each of the five domains six times.
			occurrence := c / len(specPerm)
			rotation := int(uint64(seed) % uint64(len(spec.v8Contexts)))
			ctx := spec.v8Contexts[(occurrence+rotation)%len(spec.v8Contexts)]
			tmpl = ctx.stored
			question = ctx.ask
			if len(ctx.entries) > 0 {
				entry = ctx.entries[r.Intn(len(ctx.entries))]
			}
		}
		lead := persona.Expand(r, leadG, "lead")
		trail := persona.Expand(r, leadG, "trail")
		body := fmt.Sprintf(tmpl, entry.storedQty)
		body = replaceTag(body, "#lead#", lead)
		body = replaceTag(body, "#trail#", trail)
		seedPair(c, body, "Good to know.")
		if question == "" {
			// Keep the legacy RNG order byte-for-byte: v6/v7 select their question
			// only after rendering and seeding the stored fact.
			question = spec.ask[r.Intn(len(spec.ask))]
		}

		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:             protocol.OpaqueCaseID(seed, "memnv", ordinal),
			QuestionType:   QTNonVerbatim,
			Question:       question,
			ExpectedAnswer: entry.accept[0],
			AnswerKind:     protocol.AnswerValue,
			AcceptAny:      entry.accept,
		}, RunAfterWave: nWaves - 1})
		ordinal++
	}
	return out
}

// replaceTag substitutes a single literal tag, trimming a doubled leading space
// when the tag rendered empty at the start of the sentence.
func replaceTag(s, tag, val string) string {
	out := ""
	for {
		i := indexOf(s, tag)
		if i < 0 {
			out += s
			break
		}
		out += s[:i] + val
		s = s[i+len(tag):]
	}
	// tidy: leading space, doubled spaces
	out = tidySpaces(out)
	return out
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func tidySpaces(s string) string {
	// collapse runs of spaces and trim a leading space
	var b []byte
	prevSpace := true // trim leading
	for i := 0; i < len(s); i++ {
		if s[i] == ' ' {
			if prevSpace {
				continue
			}
			prevSpace = true
			b = append(b, ' ')
		} else {
			prevSpace = false
			b = append(b, s[i])
		}
	}
	// trim trailing space
	for len(b) > 0 && b[len(b)-1] == ' ' {
		b = b[:len(b)-1]
	}
	return string(b)
}
