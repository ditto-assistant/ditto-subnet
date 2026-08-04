package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 6 multi-query recall (plan 4.6): production retrieval fans out over
// several focused sub-queries and intersects the results. This case is answerable
// ONLY by combining more than one query: the user has saved several items, each
// tagged with two independent attributes, and the question asks for the single item
// matching BOTH. A single-shot recall that grabs one attribute's matches returns a
// distractor; only a fan-out (query attribute A, query attribute B, intersect)
// resolves it. The decoys each match exactly ONE attribute, so a one-query harness
// is actively caught rather than merely underperforming.
//
// Deterministic per (seed, draw position); v6-gated so v5's bytes are untouched.

const QTMultiQuery = "multi-query-recall"

type multiQuerySuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func multiQueryEnabled(benchVersion int) bool { return benchVersion >= protocol.BenchVersionV6 }

// multiQueryCasesFor is the seed-independent case quota. Each case seeds four items
// across sessions and asks one intersection question, and benefits from spanning
// sessions, so single-wave (small) runs carry none.
func multiQueryCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 5
	case n >= 70:
		return 2
	case n >= 40:
		return 1
	default:
		return 0
	}
}

// mqDomain is a saved-collection domain with two INDEPENDENT categorical attributes.
// The question filters on one value of each; exactly one saved item matches both.
type mqDomain struct {
	item   string   // "cafe", "playlist", ...
	attrA  string   // human label for attribute A ("neighborhood")
	attrB  string   // human label for attribute B ("vibe")
	valsA  []string // possible A values
	valsB  []string // possible B values
	seedA  string   // grammar for stating item's A ("%[1]s is in %[2]s")  fmt: (name, aVal)
	seedB  string   // grammar for stating item's B                          fmt: (name, bVal)
	askFmt string   // fmt: (aVal, bVal, item)
}

var mqDomains = []mqDomain{
	{item: "cafe", attrA: "neighborhood", attrB: "specialty",
		valsA: []string{"Riverside", "Old Town", "the Docklands", "Highgate", "Millbrook"},
		valsB: []string{"pour-over", "matcha", "cold brew", "pastries", "single-origin"},
		seedA: "My cafe %[1]s is over in %[2]s.", seedB: "%[1]s is the one known for %[2]s.",
		askFmt: "Which of my saved cafes is in %[1]s AND known for %[2]s?"},
	{item: "playlist", attrA: "mood", attrB: "length",
		valsA: []string{"upbeat", "mellow", "focus", "moody", "nostalgic"},
		valsB: []string{"under an hour", "about two hours", "a quick 20 minutes", "half a day", "three hours"},
		seedA: "My playlist %[1]s is a %[2]s set.", seedB: "%[1]s runs %[2]s.",
		askFmt: "Which of my playlists is %[1]s AND runs %[2]s?"},
	{item: "restaurant", attrA: "cuisine", attrB: "area",
		valsA: []string{"Thai", "Ethiopian", "Georgian", "Peruvian", "Basque"},
		valsB: []string{"downtown", "by the harbor", "uptown", "near the park", "on the east side"},
		seedA: "The restaurant %[1]s does %[2]s food.", seedB: "%[1]s is %[2]s.",
		askFmt: "Which of my saved restaurants is %[1]s AND %[2]s?"},
	{item: "trail", attrA: "difficulty", attrB: "scenery",
		valsA: []string{"easy", "moderate", "steep", "flat", "technical"},
		valsB: []string{"a waterfall", "ridgeline views", "an old-growth forest", "a lake", "wildflower meadows"},
		seedA: "The trail %[1]s is %[2]s.", seedB: "%[1]s has %[2]s.",
		askFmt: "Which of my saved trails is %[1]s AND has %[2]s?"},
}

// buildMultiQuery generates the v6 multi-query (fan-out intersection) suite.
func buildMultiQuery(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) multiQuerySuite {
	var out multiQuerySuite
	nCases := multiQueryCasesFor(n, nWaves)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-mq-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-mq-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	for c := 0; c < nCases; c++ {
		dom := mqDomains[r.Intn(len(mqDomains))]
		// Pick the target (A,B) filter values.
		aPerm := r.Perm(len(dom.valsA))
		bPerm := r.Perm(len(dom.valsB))
		targetA, otherA := dom.valsA[aPerm[0]], dom.valsA[aPerm[1]]
		targetB, otherB := dom.valsB[bPerm[0]], dom.valsB[bPerm[1]]

		// Four coined item names. Item roles:
		//   match  -> (targetA, targetB)  = the answer
		//   onlyA  -> (targetA, otherB)   = matches A only (single-query decoy)
		//   onlyB  -> (otherA, targetB)   = matches B only (single-query decoy)
		//   neither-> (otherA, otherB)
		names := make([]string, 4)
		for i := range names {
			names[i] = persona.CoinShaped(seed, fmt.Sprintf("mq|name|%d|%d", c, i))
		}
		match, onlyA, onlyB, neither := names[0], names[1], names[2], names[3]
		items := []struct {
			name, a, b string
		}{
			{match, targetA, targetB},
			{onlyA, targetA, otherB},
			{onlyB, otherA, targetB},
			{neither, otherA, otherB},
		}
		// Seed each item's two attributes in DIFFERENT sessions so no single window
		// holds both filters for any item — the intersection must be retrieved.
		order := r.Perm(len(items))
		for oi, idx := range order {
			it := items[idx]
			seedPair(2*c+(oi%2), fmt.Sprintf(dom.seedA, it.name, it.a), "Good to know.")
			seedPair(2*c+((oi+1)%2), fmt.Sprintf(dom.seedB, it.name, it.b), "Noted.")
		}

		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:                protocol.OpaqueCaseID(seed, "memmq", ordinal),
			QuestionType:      QTMultiQuery,
			Question:          fmt.Sprintf(dom.askFmt, targetA, targetB),
			ExpectedAnswer:    match,
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: []string{onlyA, onlyB}, // each matches ONE filter
		}, RunAfterWave: nWaves - 1})
		ordinal++
	}
	return out
}
