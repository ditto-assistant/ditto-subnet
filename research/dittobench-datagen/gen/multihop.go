package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Multi-hop relational retrieval (bench_version 5; v5 plan section 4.9 — the KG
// moat). Every other memory case is answerable from a SINGLE stored pair; a
// grounded champion with strong single-shot retrieval aces them. A multi-hop case
// is answerable only by JOINING two or more memories that no single pair contains:
//
//	session A (early):  "My sister is Dana."
//	session B (later):  "Dana adopted a puppy and named it <coined>."
//	question:           "What did my sister name her puppy?"  -> <coined>
//
// The leaf token exists in the store but is reachable only by traversing the
// relation link (mine -> sister -> Dana -> puppy). A grep parser and single-shot
// vector recall both miss it: the query "my sister's puppy" lexically matches the
// RELATION pair (no leaf) and the LEAF pair only via the intermediary entity that
// the query never names. Resolving it needs the linked knowledge graph subject
// promotion builds (backend kg.go), which is the production moat the aligned
// champion stack ships and the single strongest discriminator between a shallow
// retriever and a real one.
//
// Anti-shotgun: each case seeds a WRONG-RELATIVE decoy on the same leaf attribute
// ("my cousin adopted a puppy named <other>"), so a one-hop match on the leaf
// entity ("puppy") surfaces the cousin's value and zeros. The join must resolve
// the correct relative.
const (
	// QTMultiHop is a relational-join question answerable only by traversing a link
	// no single stored pair contains.
	QTMultiHop = "multi-hop-relational"
)

type multiHopSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func multiHopEnabled(benchVersion int) bool { return benchVersion >= protocol.BenchVersionV5 }

// multiHopCasesFor is the seed-independent multi-hop CHAIN quota. Each chain emits
// a metamorphic twin PAIR (two cases), so the case count is 2x this. The chain
// needs its relation intro and leaf fact seeded before the question and benefits
// from spanning sessions/waves, so single-wave (small) runs carry none.
func multiHopCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 3
	case n >= 70:
		return 2
	case n >= 40:
		return 2
	case n >= 20:
		return 1
	default:
		return 0
	}
}

// relativeNames are uncommon given names for the intermediary entity. Uncommon so
// a collision with a rendered decoy person is unlikely; the join is by exact name
// match between the relation pair and the leaf pair. A large pool so the
// intermediary is not enumerable across seeds.
var relativeNames = []string{
	"Dana", "Priya", "Marcus", "Ingrid", "Rafael", "Yuki", "Nadia", "Emeka",
	"Lena", "Tomas", "Anouk", "Darius", "Freya", "Kwame", "Sofia", "Bjorn",
	"Iris", "Malik", "Rosa", "Viktor", "Amara", "Theo", "Petra", "Idris",
	"Simone", "Oskar", "Leila", "Hassan", "Greta", "Diego", "Noor", "Cillian",
	"Yasmin", "Arjun", "Elodie", "Kofi", "Saoirse", "Milo", "Zara", "Lars",
}

// relationPair is a (target, decoy) relation on the same leaf attribute. The
// question asks about the TARGET relative; naming the DECOY relative's leaf value
// is the shallow one-hop error. A wide pool of relation kinds so the family cannot
// be reduced to a fixed cue list.
type relationPair struct {
	target string
	decoy  string
}

var relationPairs = []relationPair{
	{target: "sister", decoy: "cousin"},
	{target: "brother", decoy: "uncle"},
	{target: "best friend", decoy: "neighbor"},
	{target: "aunt", decoy: "coworker"},
	{target: "mentor", decoy: "landlord"},
	{target: "roommate", decoy: "dentist"},
	{target: "cousin", decoy: "barber"},
	{target: "old college friend", decoy: "manager"},
	{target: "gym buddy", decoy: "accountant"},
	{target: "godmother", decoy: "physio"},
	{target: "nephew", decoy: "plumber"},
	{target: "sister-in-law", decoy: "trainer"},
	{target: "childhood friend", decoy: "vet"},
	{target: "brother-in-law", decoy: "tutor"},
}

// leafFact is a joinable attribute whose value is a COINED token (a name for a
// thing, so a coined value reads naturally). It is grammar-generated so the
// phrasing is drawn from a large space (hundreds of surfaces per noun) rather than
// one memorizable template — the family cannot be enumerated into a static cue
// list. intro states the relation; fact attaches the coined value to the
// intermediary; ask queries the target relative's leaf, present in the store only
// via the link.
type leafFact struct {
	noun string          // "puppy", "sailboat", ...
	fact persona.Grammar // root: %s person, %s value
	ask  persona.Grammar // root: %s target relation
}

// leafNouns are the joinable things whose NAME is coined. A wide set so the
// attribute space cannot be enumerated into a "if the question mentions X, do a
// 2-hop join" shortcut.
var leafNouns = []string{
	"puppy", "kitten", "sailboat", "houseplant", "road bike", "acoustic guitar",
	"food truck", "podcast", "cat", "campervan", "startup", "fantasy-league team",
	"rowboat", "terrarium", "vintage scooter", "book club", "sourdough starter",
	"parrot", "e-bike", "greenhouse",
}

// randomLeaf picks a leaf-attribute KIND and a noun, then builds grammar-driven
// fact/ask surfaces. Varying the KIND (name-a-thing / moved-to-place / works-at /
// lives-on-street) as well as the noun and phrasing means the question STRUCTURE
// itself varies, so there is no single "detect 'name their X' -> do a 2-hop join"
// shortcut to overfit. The value is always a COINED token (exact grading, natural
// as a name / town / employer / street).
func randomLeaf(r *rand.Rand) leafFact {
	kinds := []func() leafFact{
		func() leafFact { // name a thing
			noun := leafNouns[r.Intn(len(leafNouns))]
			return leafFact{noun: noun,
				fact: persona.Grammar{
					"root":  {"#lead# %s #verb# their " + noun + " %s.#trail#", "#lead# %s #verb# the " + noun + " %s.#trail#"},
					"lead":  {"Fun update:", "Oh,", "By the way,", "Heard that", "So,", "Turns out", ""},
					"verb":  {"named", "decided to call", "went with", "is calling", "finally named", "settled on calling"},
					"trail": {" Cute, right?", " I liked it.", "", " Very them."}},
				ask: persona.Grammar{
					"root": {"#lead# what did my %s name their " + noun + "?", "#lead# what's my %s's " + noun + " called?", "#lead# remind me what my %s named their " + noun + "."},
					"lead": {"", "Quick one:", "Hey,", "So,"}}}
		},
		func() leafFact { // moved to a town
			return leafFact{noun: "town",
				fact: persona.Grammar{
					"root":  {"#lead# %s #verb# a town called %s.#trail#", "#lead# %s #verb# %s, a little town.#trail#"},
					"lead":  {"Oh,", "By the way,", "Heard that", "So,", ""},
					"verb":  {"just moved to", "relocated to", "settled in", "recently moved to"},
					"trail": {" Big change.", " Suits them.", "", " Nice area."}},
				ask: persona.Grammar{
					"root": {"#lead# what town did my %s move to?", "#lead# where does my %s live now?", "#lead# which town did my %s relocate to?"},
					"lead": {"", "Quick one:", "Hey,"}}}
		},
		func() leafFact { // works at
			return leafFact{noun: "employer",
				fact: persona.Grammar{
					"root":  {"#lead# %s #verb# %s.#trail#", "#lead# %s #verb# a company called %s.#trail#"},
					"lead":  {"Oh,", "By the way,", "Heard", "So,", ""},
					"verb":  {"started a job at", "now works at", "took a role at", "just joined"},
					"trail": {" Exciting.", " Good for them.", "", " Big step."}},
				ask: persona.Grammar{
					"root": {"#lead# where does my %s work?", "#lead# what company did my %s join?", "#lead# who does my %s work for now?"},
					"lead": {"", "Quick one:", "Hey,"}}}
		},
		func() leafFact { // lives on street
			return leafFact{noun: "street",
				fact: persona.Grammar{
					"root":  {"#lead# %s #verb# %s Street.#trail#", "#lead# %s's new place is on %s Street.#trail#"},
					"lead":  {"Oh,", "By the way,", "So,", ""},
					"verb":  {"moved to", "just bought a place on", "now lives on"},
					"trail": {" Lovely block.", "", " Quiet street."}},
				ask: persona.Grammar{
					"root": {"#lead# what street does my %s live on?", "#lead# which street is my %s's place on?"},
					"lead": {"", "Hey,"}}}
		},
	}
	return kinds[r.Intn(len(kinds))]()
}

// buildMultiHop generates the multi-hop relational cases. Deterministic per (seed,
// draw position): all picks draw from the shared suite rng at a fixed point in
// GenerateMemorySuite, gated so a pre-v5 contract's draw sequence is untouched.
func buildMultiHop(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) multiHopSuite {
	var out multiHopSuite
	nCases := multiHopCasesFor(n, nWaves)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	// The relation intro and the leaf fact are seeded in DIFFERENT sessions, so the
	// join is genuinely cross-session (the answer is in no single session window).
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-mh-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-mh-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	// introOf varies the relation-introduction surface (lead, body order, trail) so
	// the relation pair is not a fixed memorizable template.
	leads := []string{"", "Oh, ", "By the way, ", "For context, ", "Quick note: ", "So, "}
	trails := []string{"", " Lovely person.", " We're close.", " Known them ages.", " Good egg."}
	introOf := func(relation, person string) string {
		body := "my " + relation + " is " + person
		if r.Intn(2) == 0 {
			body = person + " is my " + relation
		}
		return leads[r.Intn(len(leads))] + body + "." + trails[r.Intn(len(trails))]
	}

	for c := 0; c < nCases; c++ {
		rel := relationPairs[r.Intn(len(relationPairs))]
		leaf := randomLeaf(r)
		// Distinct intermediary names for target and decoy.
		tPerm := r.Perm(len(relativeNames))
		targetPerson := relativeNames[tPerm[0]]
		decoyPerson := relativeNames[tPerm[1]]
		targetVal := persona.CoinShaped(seed, fmt.Sprintf("mh|val|%d", c))
		decoyVal := persona.CoinShaped(seed, fmt.Sprintf("mh|dec|%d", c))

		// Target chain: relation intro and leaf fact, in two distinct sessions.
		seedPair(4*c, introOf(rel.target, targetPerson), "Good to know.")
		seedPair(4*c+1, fmt.Sprintf(persona.Expand(r, leaf.fact, "root"), targetPerson, targetVal), "Noted.")
		// Decoy chain: wrong relative on the SAME leaf attribute, two more sessions.
		seedPair(4*c+2, introOf(rel.decoy, decoyPerson), "Got it.")
		seedPair(4*c+3, fmt.Sprintf(persona.Expand(r, leaf.fact, "root"), decoyPerson, decoyVal), "Nice.")

		// METAMORPHIC-INVARIANCE TWINS: ask the SAME join two different ways, sharing a
		// TwinGroup. A harness that overfit one surface but cannot generalize the join
		// answers them inconsistently and is penalized by MetamorphicConsistencyFactor
		// (the CheckList/INV robustness signal). Two distinct ask surfaces from the
		// same leaf.ask grammar; the answer is identical.
		twin := fmt.Sprintf("mhtwin-%d-%d", seed, c)
		q1 := fmt.Sprintf(persona.Expand(r, leaf.ask, "root"), rel.target)
		var q2 string
		for i := 0; i < 6; i++ { // draw a DIFFERENT surface for the twin
			q2 = fmt.Sprintf(persona.Expand(r, leaf.ask, "root"), rel.target)
			if q2 != q1 {
				break
			}
		}
		for i, q := range []string{q1, q2} {
			out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
				ID:                protocol.OpaqueCaseID(seed, "memmh", ordinal),
				QuestionType:      QTMultiHop,
				Question:          q,
				ExpectedAnswer:    targetVal,
				AnswerKind:        protocol.AnswerValue,
				DistractorAnswers: []string{decoyVal},
				TwinGroup:         twin,
			}, RunAfterWave: nWaves - 1})
			ordinal++
			_ = i
		}
	}
	return out
}
