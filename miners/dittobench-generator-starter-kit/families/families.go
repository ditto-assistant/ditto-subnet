package families

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Family emits one case into the builder. i distinguishes repeated uses.
type Family func(b *Builder, i int) challenge.Case

// UpdateChain is the baseline family: a fact, an unrelated decoy, and a
// later correction. Current top agents mostly solve it, so on its own it
// scores as trivial. Keep it as a calibration control.
func UpdateChain(b *Builder, i int) challenge.Case {
	v := b.Pick(cities, 3)
	event := b.Pick(events, 1)[0]
	b.Say(0, fmt.Sprintf("The %s is booked in %s.", event, v[0]), fmt.Sprintf("Great, %s it is.", v[0]))
	b.Say(1, fmt.Sprintf("My cousin moved to %s last month.", v[2]), "Nice, that sounds like a fun place to visit.")
	ev := b.Say(2, fmt.Sprintf("Change of plans, the %s moved to %s.", event, v[1]), "Updated.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("update-chain-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("Where is the %s happening now?", event),
			ExpectedAnswer:    v[1],
			DistractorAnswers: []string{v[0], v[2]},
		},
		EvidencePairIDs: []string{ev},
	}
}

// TwoHop joins two facts stated in different sessions: who holds a role, and
// where that person lives. The question names only the role, so an agent must
// resolve the person before it can retrieve the place. A second person with
// the same kind of fact is the decoy.
func TwoHop(b *Builder, i int) challenge.Case {
	people := b.Pick(firstNames, 2)
	places := b.Pick(cities, 2)
	role := b.Pick(roles, 1)[0]
	ev1 := b.Say(0, fmt.Sprintf("Started with a new %s, %s. Really good so far.", role, people[0]), "Glad it's going well.")
	b.Say(0, fmt.Sprintf("%s from the climbing gym just moved to %s.", people[1], places[1]), "Hope they settle in fast.")
	ev2 := b.Say(1, fmt.Sprintf("Fun fact, %s actually commutes in from %s every day.", people[0], places[0]), "That's quite a commute!")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("two-hop-%d", i),
			QuestionType:      "multi-hop",
			Question:          fmt.Sprintf("Which city does my %s commute from?", role),
			ExpectedAnswer:    places[0],
			DistractorAnswers: []string{places[1]},
		},
		EvidencePairIDs: []string{ev1, ev2},
	}
}

// RetractedPreference states a person's habit, mentions someone else's,
// then retracts the original in a paraphrase with no cessation keyword
// ("no longer", "stopped"). A keyword-driven update tracker keeps the stale
// value. The question names the person, so several of these cases in one
// pack never make each other ambiguous.
func RetractedPreference(b *Builder, i int) challenge.Case {
	drinks := b.Pick(drinksPool, 3)
	who := b.Pick(firstNames, 2)
	b.Say(0, fmt.Sprintf("%s is all about %s in the mornings.", who[0], drinks[0]), fmt.Sprintf("Classic, %s it is.", drinks[0]))
	b.Say(1, fmt.Sprintf("%s swears by %s before work.", who[1], drinks[2]), "Everyone has their ritual.")
	ev := b.Say(2, fmt.Sprintf("%s says their stomach can't take %s anymore, so these days it's %s every morning.", who[0], drinks[0], drinks[1]), "Makes sense, that's a gentler start.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("retracted-pref-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("What does %s drink in the morning lately?", who[0]),
			ExpectedAnswer:    drinks[1],
			DistractorAnswers: []string{drinks[0], drinks[2]},
		},
		EvidencePairIDs: []string{ev},
	}
}

// ScatteredList spreads the members of a list across waves among a decoy
// member that belongs to someone else. The agent must aggregate every
// session, not stop at the first hit.
func ScatteredList(b *Builder, i int) challenge.Case {
	items := b.Pick(allergens, 4)
	person := b.Pick(firstNames, 1)[0]
	friend := b.Pick(firstNames, 1)[0]
	ev1 := b.Say(0, fmt.Sprintf("Planning dinner for %s. Note that %s can't eat %s.", person, person, items[0]), "Noted.")
	b.Say(1, fmt.Sprintf("%s is coming too and avoids %s.", friend, items[3]), "Got it.")
	ev2 := b.Say(1, fmt.Sprintf("Oh, and %s reacts badly to %s as well.", person, items[1]), "Adding that to the list.")
	ev3 := b.Say(2, fmt.Sprintf("Last thing: %s found out %s is also a problem.", person, items[2]), "Good to know before the dinner.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("scattered-list-%d", i),
			QuestionType:      "multi-session-aggregation",
			Question:          fmt.Sprintf("Which foods should I keep away from %s?", person),
			ExpectedAnswer:    fmt.Sprintf("%s, %s, and %s", items[0], items[1], items[2]),
			AnswerKind:        protocol.AnswerList,
			AnswerItems:       items[:3],
			DistractorAnswers: []string{items[3]},
		},
		EvidencePairIDs: []string{ev1, ev2, ev3},
	}
}

// Default is the starter mix. Replace and extend it: the families that earn
// are the ones the current top agents split on.
var Default = []Family{UpdateChain, TwoHop, RetractedPreference, ScatteredList}

// Filler seeds an unrelated pair so cases are not the whole haystack.
func Filler(b *Builder) {
	line := filler[b.Rng.Intn(len(filler))]
	b.Say(b.Rng.Intn(Waves), line[0], line[1])
}
