package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 7 near-miss abstention. The existing abstention cases are
// generic never-mentioned attributes, and the v5 confabulation probe
// (QTAbstainConfab) seeds ONE adjacent possession. Both look unanswerable on a
// close read. A near-miss case is engineered to look ANSWERABLE:
//
//   - the sibling entity's value for the SAME attribute is seeded ("the
//     downtown parking garage entry code is <coined>"), and
//   - the ASKED entity itself is mentioned in the store in an unrelated
//     context ("finally sorted out the airport garage's monthly billing"), so
//     both the attribute words and the asked entity words retrieve real pairs,
//
// yet the asked fact ("the airport parking garage's entry code") was never
// stated. Correct behavior is a grounded decline; the sibling's coined value
// is a scored distractor, so a nearest-neighbor retriever that reports the
// closest match on attribute words is zeroed, not merely uncredited.
//
// Deterministic per (seed, draw position); v7-gated so v6's bytes are
// untouched.
const QTNearMiss = "near-miss-abstention"

type nearMissSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func nearMissEnabled(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV7
}

// nearMissCasesFor is the seed-independent case quota.
func nearMissCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 8
	case n >= 40:
		return 2
	case n >= 20:
		return 1
	default:
		return 0
	}
}

// nmSpec is one near-miss domain: the seeded sibling entity, the asked (never
// valued) entity, the shared attribute, and the unrelated context in which the
// asked entity is mentioned. Entities are chosen so no honest reader could
// take the sibling's value as the asked entity's (distinct places/objects),
// and so neither collides with a lifecycle/deep-chain noun or a persona
// attribute.
type nmSpec struct {
	seeded  string // entity whose attribute IS stated
	asked   string // entity the question asks about (never valued)
	attr    string // the shared attribute
	context string // fmt: none — an unrelated mention of the asked entity
}

var nmSpecs = []nmSpec{
	{seeded: "downtown parking garage", asked: "airport parking garage", attr: "entry code",
		context: "Finally sorted out the airport parking garage's monthly billing today."},
	{seeded: "home safe", asked: "office safe", attr: "combination",
		context: "The office safe got moved next to the printer during the remodel."},
	{seeded: "blue suitcase", asked: "gray duffel", attr: "luggage tag number",
		context: "Packed the gray duffel for the weekend — it barely closed."},
	{seeded: "river cabin", asked: "mountain cabin", attr: "door code",
		context: "The mountain cabin's roof finally got repaired last month."},
	{seeded: "work badge", asked: "gym badge", attr: "badge number",
		context: "My gym badge photo is hilariously bad, for the record."},
	{seeded: "boat trailer", asked: "utility trailer", attr: "hitch lock code",
		context: "Renewed the registration on the utility trailer last week."},
	{seeded: "studio kiln", asked: "community-center kiln", attr: "access code",
		context: "The community-center kiln is booked solid through the spring."},
	{seeded: "rooftop mailbox", asked: "lobby mailbox", attr: "box number",
		context: "The lobby mailbox jammed again; maintenance is on it."},
	{seeded: "beach bike", asked: "city bike", attr: "lock code",
		context: "My city bike needs a new chain, adding it to the list."},
	{seeded: "guest room", asked: "home office", attr: "smart-lock PIN",
		context: "Repainting the home office a soft green next weekend."},
	{seeded: "old laptop", asked: "work laptop", attr: "disk password",
		context: "The work laptop is due for its OS upgrade this cycle."},
	{seeded: "summer locker", asked: "winter locker", attr: "combination",
		context: "The winter locker is down the far hallway now."},
	{seeded: "front gate", asked: "side gate", attr: "keypad code",
		context: "The side gate hinge squeaks; I keep meaning to oil it."},
	{seeded: "personal Dropbox", asked: "shared Dropbox", attr: "folder passcode",
		context: "The shared Dropbox is nearly out of space again."},
	{seeded: "downstairs thermostat", asked: "upstairs thermostat", attr: "schedule PIN",
		context: "The upstairs thermostat reads a couple degrees high, I think."},
	{seeded: "checking account", asked: "savings account", attr: "last four digits",
		context: "Moved my savings account to a new bank branch last month."},
	{seeded: "garden shed", asked: "tool shed", attr: "padlock code",
		context: "The tool shed roof could use a tarp before the rains."},
	{seeded: "weekday alarm", asked: "weekend alarm", attr: "set time",
		context: "My weekend alarm tone is that soft marimba one."},
	{seeded: "primary router", asked: "guest router", attr: "admin password",
		context: "The guest router sits on the shelf by the window."},
	{seeded: "car A", asked: "car B", attr: "glovebox code",
		context: "Car B is due for an oil change around next Friday."},
}

// nmStateGrammar phrases the sibling's stated fact (fmt: entity, attr, value).
var nmStateGrammar = persona.Grammar{
	"root": {
		"#lead# the %[1]s's %[2]s is %[3]s.#trail#",
		"#lead# for the %[1]s, the %[2]s is %[3]s.#trail#",
	},
	"lead":  {"", "By the way,", "Quick note:", "Oh,", "For reference,"},
	"trail": {"", " Keep it handy.", " Don't lose it.", ""},
}

// nmAskGrammar phrases the near-miss question (fmt: attr, entity).
var nmAskGrammar = persona.Grammar{
	"root": {
		"#lead# what's the %[1]s for my %[2]s?",
		"#lead# remind me of the %[2]s's %[1]s.",
		"#lead# what did I say the %[1]s on the %[2]s was?",
	},
	"lead": {"", "Hey,", "Quick one:"},
}

// buildNearMiss generates the v7 near-miss abstention suite.
func buildNearMiss(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) nearMissSuite {
	var out nearMissSuite
	nCases := nearMissCasesFor(n, nWaves)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-nm-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-nm-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	specPerm := r.Perm(len(nmSpecs))
	for c := 0; c < nCases; c++ {
		spec := nmSpecs[specPerm[c%len(specPerm)]]
		bait := persona.CoinShaped(seed, fmt.Sprintf("nm|bait|%d", c))

		// The sibling's fact and the asked entity's unrelated mention land in two
		// different sessions, so a session-window read cannot even see that the
		// asked fact is missing — the store as a whole must be consulted.
		seedPair(2*c, fmt.Sprintf(persona.Expand(r, nmStateGrammar, "root"), spec.seeded, spec.attr, bait), "Noted.")
		seedPair(2*c+1, spec.context, "Thanks for the update.")

		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:                protocol.OpaqueCaseID(seed, "memnm", c),
			QuestionType:      QTNearMiss,
			Question:          fmt.Sprintf(persona.Expand(r, nmAskGrammar, "root"), spec.attr, spec.asked),
			ExpectedAnswer:    abstentionExpectedAnswer,
			AnswerKind:        protocol.AnswerDecline,
			DistractorAnswers: []string{bait},
		}, RunAfterWave: nWaves - 1})
	}
	return out
}
