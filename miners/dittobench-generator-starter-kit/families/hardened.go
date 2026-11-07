package families

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The hardened families replace the four defaults in rounds at grading
// policy 18 or later. Every default family was trivial in generator rounds
// (each top agent solved every case), so the public default generator, which
// fills every empty dueling column, scored nobody apart. Each hardened family
// is a small re-implementation of one of the public first-party bench v14
// families (research/dittobench-datagen/universe/v14_*.go) and keeps it within
// two hops:
//
//   - Revert: a value is changed and later reverted without restating it.
//   - Custody: the person who handed an item over is not the person holding it.
//   - DatedStatus: the question date falls inside a middle interval, so neither
//     the first nor the latest value answers.
//   - Membership: a list member is removed and later re-added.
//
// They draw from the same pools as the defaults, at the same or lower rate,
// plus the custody item pool, so MaxCases still fills without reuse.

// Revert books an event, moves it, and then moves it back without naming the
// original place. The latest-mentioned city is the stale answer; another
// city in the haystack is the decoy.
func Revert(b *Builder, i int) challenge.Case {
	v := b.Pick(cities, 3)
	event := b.Pick(events, 1)[0]
	ev := b.Say(0, fmt.Sprintf("The %s is booked in %s.", event, v[0]), fmt.Sprintf("Great, %s it is.", v[0]))
	b.Say(1, fmt.Sprintf("Change of plans, the %s moved to %s.", event, v[1]), "Updated.")
	b.Say(1, fmt.Sprintf("My cousin moved to %s last month.", v[2]), "Nice, that sounds like a fun place to visit.")
	back := b.Say(2, fmt.Sprintf("Ugh, the new venue fell through, so the %s is back where we first booked it.", event), "Okay, back to the original plan.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("revert-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("Which city is the %s in now?", event),
			ExpectedAnswer:    v[0],
			DistractorAnswers: []string{v[1], v[2]},
		},
		EvidencePairIDs: []string{ev, back},
	}
}

// Custody hands an item from one person to a second, then the second passes
// it on, naming it only by its bare noun. The question asks who has it now:
// the person who signed it out and the one in the middle are the distractors.
func Custody(b *Builder, i int) challenge.Case {
	who := b.Pick(firstNames, 3)
	item := custodyItems[b.Rng.Intn(len(custodyItems))]
	for b.used["item:"+item[0]] {
		item = custodyItems[b.Rng.Intn(len(custodyItems))]
	}
	b.used["item:"+item[0]] = true
	first := b.Say(0, fmt.Sprintf("%s signed out the %s on Monday and handed it to %s.", who[0], item[0], who[1]), "Good to know who has it.")
	second := b.Say(1, fmt.Sprintf("%s passed the %s on to %s before the trip.", who[1], item[1], who[2]), "Noted.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("custody-%d", i),
			QuestionType:      "multi-hop",
			Question:          fmt.Sprintf("Who has the %s right now?", item[0]),
			ExpectedAnswer:    who[2],
			DistractorAnswers: []string{who[0], who[1]},
		},
		EvidencePairIDs: []string{first, second},
	}
}

// DatedStatus records three dated changes to a person's morning drink and
// asks about a date between the second and third, so the first value and the
// latest value are both wrong.
func DatedStatus(b *Builder, i int) challenge.Case {
	drinks := b.Pick(drinksPool, 3)
	who := b.Pick(firstNames, 1)[0]
	// Months 1..12; m1 < m2 < q < m3 with a full month between m2 and m3.
	m1 := 1 + b.Rng.Intn(3)
	m2 := m1 + 2 + b.Rng.Intn(2)
	q := m2 + 1 + b.Rng.Intn(2)
	m3 := q + 1 + b.Rng.Intn(2)
	day := func() int { return 3 + b.Rng.Intn(20) }
	b.Say(0, fmt.Sprintf("Since %s %d, %s has had %s every morning.", monthNames[m1], day(), who, drinks[0]), fmt.Sprintf("%s every morning, got it.", drinks[0]))
	ev := b.Say(1, fmt.Sprintf("On %s %d %s switched to %s in the mornings.", monthNames[m2], day(), who, drinks[1]), "Noted the switch.")
	b.Say(2, fmt.Sprintf("As of %s %d, %s is on %s every morning instead.", monthNames[m3], day(), who, drinks[2]), "Updated.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("dated-status-%d", i),
			QuestionType:      "temporal-reasoning",
			Question:          fmt.Sprintf("What was %s drinking in the mornings in the middle of %s?", who, monthNames[q]),
			ExpectedAnswer:    drinks[1],
			DistractorAnswers: []string{drinks[0], drinks[2]},
		},
		EvidencePairIDs: []string{ev},
	}
}

// Membership lists three foods a person avoids, clears one, and later adds it
// back. A tracker that applies only the removal answers with two items; the
// friend's food is the decoy.
func Membership(b *Builder, i int) challenge.Case {
	items := b.Pick(allergens, 4)
	people := b.Pick(firstNames, 2)
	person, friend := people[0], people[1]
	ev1 := b.Say(0, fmt.Sprintf("Planning dinner for %s, who can't eat %s or %s.", person, items[0], items[1]), "Noted.")
	b.Say(1, fmt.Sprintf("%s is coming too and avoids %s.", friend, items[3]), "Got it.")
	b.Say(1, fmt.Sprintf("Good news, %s's allergist says %s is fine now.", person, items[1]), "That makes the menu easier.")
	ev2 := b.Say(2, fmt.Sprintf("Spoke too soon: %s reacted again, so %s is off the menu for them after all, and %s is a problem too.", person, items[1], items[2]), "Adding both back to the list.")
	return challenge.Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("membership-%d", i),
			QuestionType:      "multi-session-aggregation",
			Question:          fmt.Sprintf("Which foods should I keep away from %s now?", person),
			ExpectedAnswer:    fmt.Sprintf("%s, %s, and %s", items[0], items[1], items[2]),
			AnswerKind:        protocol.AnswerList,
			AnswerItems:       items[:3],
			DistractorAnswers: []string{items[3]},
		},
		EvidencePairIDs: []string{ev1, ev2},
	}
}

// Hardened is the policy 18+ mix.
var Hardened = []Family{Revert, Custody, DatedStatus, Membership}

var monthNames = []string{"", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"}

// custodyItems pairs an item's full name with the bare noun the second hop
// uses. Neither half appears inside a value of any other pool.
var custodyItems = [][2]string{
	{"label printer", "printer"}, {"van keys", "keys"}, {"site badge", "badge"}, {"tripod case", "case"},
	{"spare laptop", "laptop"}, {"storage unit fob", "fob"}, {"projector bag", "bag"}, {"camera kit", "kit"},
	{"loaner tablet", "tablet"}, {"ladder", "ladder"}, {"drill set", "drill"}, {"microphone stand", "stand"},
	{"cargo bike", "bike"}, {"parking pass", "pass"}, {"tool chest", "chest"}, {"lockbox", "lockbox"},
	{"pressure washer", "washer"}, {"folding tables", "tables"}, {"walkie-talkies", "radios"}, {"sewing machine", "machine"},
	{"tent", "tent"}, {"cooler", "cooler"}, {"telescope", "telescope"}, {"kayak", "kayak"},
}
