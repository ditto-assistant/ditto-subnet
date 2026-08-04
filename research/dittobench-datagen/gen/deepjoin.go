package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 7 three-hop relational joins. The v5 multi-hop cases
// (gen/multihop.go) join TWO memories (mine -> sister -> Dana -> puppy); a
// harness that special-cased one join level passes them. A deep join needs a
// THREE-pair traversal seeded across three different sessions:
//
//	session A: "my mentor is Ingrid."
//	session B: "Ingrid's partner is Rafael."
//	session C: "Rafael just started at <coined>."
//	question:  "Where does my mentor's partner work?" -> <coined>
//
// Two shallow strategies are actively trapped rather than merely uncredited:
//
//   - the ONE-JOIN trap: a pair attaching the SAME leaf attribute directly to
//     the first-hop person ("Ingrid works at <decoy1>") is seeded, so a harness
//     that resolves "my mentor" and grabs the nearest employer surfaces a
//     scored distractor;
//   - the WRONG-CHAIN trap: a full decoy chain (a different relative, their
//     partner, and that partner's employer <decoy2>) is seeded, so a harness
//     that keys on the leaf attribute alone ("who works where?") surfaces the
//     other chain's value.
//
// Asked as a metamorphic twin pair (shared TwinGroup) like the v5 joins.
// Deterministic per (seed, draw position); v7-gated so v6's bytes are
// untouched.
const QTDeepJoin = "multi-hop-deep"

type deepJoinSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func deepJoinEnabled(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV7
}

// deepJoinChainsFor is the seed-independent chain quota. Each chain emits a
// twin PAIR (two cases) plus seven haystack pairs.
func deepJoinChainsFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 12
	case n >= 40:
		return 1
	default:
		return 0
	}
}

// djFirstHops are the first-hop relations; djSecondHops the second-hop link.
// Kept disjoint from each other so "my father-in-law's partner" can never
// parse as a single relation, disjoint from the v5 multi-hop relation pools
// (relationPairs) so one run never states the same singular relation twice
// with different people, and wide enough that the family is not a fixed cue
// list.
var djFirstHops = []string{
	"father-in-law", "mother-in-law", "stepbrother", "stepsister",
	"goddaughter", "grandfather", "grandmother", "great-aunt",
	"eldest niece", "pen pal", "former manager", "climbing partner",
	"book-club host", "next-door neighbor", "college advisor", "sailing instructor",
	"choir director", "old bandmate", "travel buddy", "chess rival",
	"physical therapist", "dog walker", "landlady", "ski instructor",
	"apiary mentor", "quilting-circle lead", "kayak guide", "allotment neighbor",
}

var djSecondHops = []string{
	"partner", "spouse", "business partner", "flatmate",
}

// djNames is the deep-join intermediary name pool. DISJOINT from the v5
// multi-hop relativeNames: if the two modules could draw the same person in
// one run, a single name might carry two conflicting leaf facts (two
// employers, two towns) and the join would be genuinely ambiguous.
var djNames = []string{
	"Beatrix", "Caspian", "Delphine", "Evander", "Fenella", "Gideon",
	"Henrike", "Isandro", "Jocasta", "Kenji", "Liudmila", "Matteo",
	"Nerissa", "Obadiah", "Perpetua", "Quillon", "Rosalind", "Severin",
	"Tindra", "Ulysses", "Verena", "Wendeline", "Xiomara", "Yevgenia",
	"Anselm", "Brigida", "Corentin", "Dagny", "Emrys", "Fiadh",
	"Guillermo", "Halldora", "Ismene", "Joaquin", "Katarina", "Lorcan",
	"Mireille", "Nikolai", "Ottoline", "Pascal", "Quintina", "Rurik",
	"Sunniva", "Torquil", "Ursula", "Valentin", "Wilhelmina", "Xavier",
	"Ysolde", "Zenobia", "Aurelio", "Bronwen",
}

// djLeaf is a terminal attribute: how the leaf fact is stated (fmt: person,
// value), how the one-join decoy is stated (fmt: person, decoy value), and the
// twin ask surfaces (fmt: first-hop relation, second-hop relation).
type djLeaf struct {
	fact  persona.Grammar // root: %[1]s person, %[2]s value
	decoy string          // fmt: (person, decoyValue) — first-hop person's OWN leaf value
	ask   persona.Grammar // root: %[1]s firstHop, %[2]s secondHop
}

var djLeaves = []djLeaf{
	{ // works at
		fact: persona.Grammar{
			"root":  {"#lead# %[1]s #verb# %[2]s.#trail#", "#lead# %[1]s #verb# a company called %[2]s.#trail#"},
			"lead":  {"Oh,", "By the way,", "Heard that", "So,", ""},
			"verb":  {"just started at", "now works at", "took a role at", "just joined"},
			"trail": {" Exciting.", " Big step.", "", " Good for them."},
		},
		decoy: "%[1]s has been at %[2]s for years, by the way.",
		ask: persona.Grammar{
			"root": {
				"#lead# where does my %[1]s's %[2]s work?",
				"#lead# what company does my %[1]s's %[2]s work for?",
				"#lead# remind me who my %[1]s's %[2]s works for.",
			},
			"lead": {"", "Quick one:", "Hey,", "So,"},
		},
	},
	{ // moved to a town
		fact: persona.Grammar{
			"root":  {"#lead# %[1]s #verb# a town called %[2]s.#trail#", "#lead# %[1]s #verb# %[2]s, a little town.#trail#"},
			"lead":  {"Oh,", "By the way,", "So,", "Heard that", ""},
			"verb":  {"just moved to", "relocated to", "settled in", "recently moved to"},
			"trail": {" Big change.", " Nice area.", "", " Suits them."},
		},
		decoy: "%[1]s still lives over in %[2]s, same as always.",
		ask: persona.Grammar{
			"root": {
				"#lead# what town did my %[1]s's %[2]s move to?",
				"#lead# where does my %[1]s's %[2]s live now?",
				"#lead# which town did my %[1]s's %[2]s end up in?",
			},
			"lead": {"", "Quick one:", "Hey,"},
		},
	},
}

// buildDeepJoin generates the v7 three-hop join suite. Deterministic per
// (seed, draw position); all picks draw from the shared suite rng at a fixed
// point in GenerateMemorySuite.
func buildDeepJoin(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) deepJoinSuite {
	var out deepJoinSuite
	nChains := deepJoinChainsFor(n, nWaves)
	if nChains == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-dj-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-dj-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	leads := []string{"", "Oh, ", "By the way, ", "For context, ", "So, "}
	trails := []string{"", " Lovely person.", " We go way back.", " Good egg."}
	relIntro := func(relation, person string) string {
		body := "my " + relation + " is " + person
		if r.Intn(2) == 0 {
			body = person + " is my " + relation
		}
		return leads[r.Intn(len(leads))] + body + "." + trails[r.Intn(len(trails))]
	}
	linkIntro := func(a, link, b string) string {
		body := a + "'s " + link + " is " + b
		if r.Intn(2) == 0 {
			body = b + " is " + a + "'s " + link
		}
		return leads[r.Intn(len(leads))] + body + "." + trails[r.Intn(len(trails))]
	}

	// One permutation across ALL chains for both the relations and the people:
	// two chains must never state the same singular relation ("my grandfather is
	// X" twice) or attach a second leaf fact to one person, either of which
	// would make a join ambiguous.
	hop1Perm := r.Perm(len(djFirstHops))
	namePerm := r.Perm(len(djNames))
	for c := 0; c < nChains; c++ {
		hop1, hop1Decoy := djFirstHops[hop1Perm[2*c]], djFirstHops[hop1Perm[2*c+1]]
		hop2 := djSecondHops[r.Intn(len(djSecondHops))]
		leaf := djLeaves[r.Intn(len(djLeaves))]

		// Four distinct people: target chain A->B, decoy chain C->D.
		personA := djNames[namePerm[4*c]]
		personB := djNames[namePerm[4*c+1]]
		personC := djNames[namePerm[4*c+2]]
		personD := djNames[namePerm[4*c+3]]

		targetVal := persona.CoinShaped(seed, fmt.Sprintf("dj|val|%d", c))
		oneJoinDecoy := persona.CoinShaped(seed, fmt.Sprintf("dj|one|%d", c))
		wrongChainDecoy := persona.CoinShaped(seed, fmt.Sprintf("dj|dec|%d", c))

		// Target chain: three pairs, three sessions. The one-join trap attaches
		// the leaf attribute directly to person A in a fourth session.
		seedPair(7*c, relIntro(hop1, personA), "Good to know.")
		seedPair(7*c+1, linkIntro(personA, hop2, personB), "Noted.")
		seedPair(7*c+2, fmt.Sprintf(persona.Expand(r, leaf.fact, "root"), personB, targetVal), "Nice.")
		seedPair(7*c+3, fmt.Sprintf(leaf.decoy, personA, oneJoinDecoy), "Got it.")
		// Decoy chain: wrong first-hop relative, same second-hop link and leaf.
		seedPair(7*c+4, relIntro(hop1Decoy, personC), "Okay.")
		seedPair(7*c+5, linkIntro(personC, hop2, personD), "Noted.")
		seedPair(7*c+6, fmt.Sprintf(persona.Expand(r, leaf.fact, "root"), personD, wrongChainDecoy), "Thanks.")

		twin := fmt.Sprintf("djtwin-%d-%d", seed, c)
		q1 := fmt.Sprintf(persona.Expand(r, leaf.ask, "root"), hop1, hop2)
		var q2 string
		for i := 0; i < 6; i++ {
			q2 = fmt.Sprintf(persona.Expand(r, leaf.ask, "root"), hop1, hop2)
			if q2 != q1 {
				break
			}
		}
		for _, q := range []string{q1, q2} {
			out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
				ID:                protocol.OpaqueCaseID(seed, "memdj", ordinal),
				QuestionType:      QTDeepJoin,
				Question:          q,
				ExpectedAnswer:    targetVal,
				AnswerKind:        protocol.AnswerValue,
				DistractorAnswers: []string{oneJoinDecoy, wrongChainDecoy},
				TwinGroup:         twin,
			}, RunAfterWave: nWaves - 1})
			ordinal++
		}
	}
	return out
}
