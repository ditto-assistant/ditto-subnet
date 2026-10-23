package challenge

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// ReferenceGeneratorName identifies packs built by ReferencePack.
const ReferenceGeneratorName = "reference-update-chains"

// ReferenceGeneratorVersion is bumped whenever ReferencePack's output changes.
const ReferenceGeneratorVersion = "1"

// ReferencePack is the reference generator: a small, deterministic program
// that emits a valid v1 pack for a seed. It exists so the contract has a
// known-good producer (tests, the CLI's `example` command, and the generator
// starter kit build on it), not to win: every case is an "update chain" (a
// fact, a decoy belonging to someone or something else, and a later
// correction) that current agents mostly solve.
func ReferencePack(seed int64, minerHotkey string) Pack {
	rng := rand.New(rand.NewSource(seed))
	b := &refBuilder{rng: rng, waves: make([]protocol.SeedRequest, 3)}
	for i := range b.waves {
		b.waves[i].Wave = i
	}
	surnames := b.pick(refSurnames, 12)
	cities := b.pick(refCities, 12)
	allergens := b.pick(refAllergens, 12)
	people := b.pick(refFirstNames, 8)
	things := b.pick(refCodeThings, 8)
	roles := b.pick(refRoles, 4)
	sites := b.pick(refEvents, 4)

	var cases []Case
	for i := 0; i < 4; i++ {
		cases = append(cases, b.codeUpdate(i, things[2*i], things[2*i+1]))
		cases = append(cases, b.personUpdate(i, roles[i], surnames[3*i], surnames[3*i+1], surnames[3*i+2]))
		cases = append(cases, b.allergyList(i, people[2*i], people[2*i+1], allergens[3*i], allergens[3*i+1], allergens[3*i+2]))
		cases = append(cases, b.venueMove(i, sites[i], cities[3*i], cities[3*i+1], cities[3*i+2]))
		b.filler()
	}
	return Pack{
		Contract:            ContractV1,
		GradingBenchVersion: GradingBenchVersion,
		MinerHotkey:         minerHotkey,
		Generator: Generator{
			Name:    ReferenceGeneratorName,
			Version: ReferenceGeneratorVersion,
			Seed:    SeedFromInt64(seed),
		},
		MemoryWaves: b.waves,
		Cases:       cases,
	}
}

type refBuilder struct {
	rng    *rand.Rand
	waves  []protocol.SeedRequest
	nPairs int
	codes  map[int]bool
}

var refEpoch = time.Date(2026, time.January, 5, 9, 0, 0, 0, time.UTC)

func (b *refBuilder) pick(pool []string, n int) []string {
	idx := b.rng.Perm(len(pool))[:n]
	out := make([]string, n)
	for i, j := range idx {
		out[i] = pool[j]
	}
	return out
}

func (b *refBuilder) code() string {
	if b.codes == nil {
		b.codes = map[int]bool{}
	}
	for {
		n := 1000 + b.rng.Intn(9000)
		if !b.codes[n] {
			b.codes[n] = true
			return fmt.Sprintf("%d", n)
		}
	}
}

// add seeds one pair in a wave and returns its id.
func (b *refBuilder) add(wave int, prompt, response string) string {
	b.nPairs++
	id := fmt.Sprintf("p%04d", b.nPairs)
	b.waves[wave].Pairs = append(b.waves[wave].Pairs, protocol.MemoryPair{
		PairID:    id,
		SessionID: fmt.Sprintf("s%02d", wave*10+b.nPairs%7),
		Timestamp: refEpoch.Add(time.Duration(b.nPairs) * 37 * time.Minute).Format(time.RFC3339),
		Prompt:    prompt,
		Response:  response,
	})
	return id
}

func (b *refBuilder) filler() {
	line := refFiller[b.rng.Intn(len(refFiller))]
	b.add(b.rng.Intn(len(b.waves)), line[0], line[1])
}

func (b *refBuilder) codeUpdate(i int, thing, decoyThing string) Case {
	old, decoy, fresh := b.code(), b.code(), b.code()
	b.add(0, fmt.Sprintf("Quick note so I don't forget: my %s is %s.", thing, old), fmt.Sprintf("Noted, your %s is %s.", thing, old))
	b.add(1, fmt.Sprintf("Also, the %s is %s.", decoyThing, decoy), fmt.Sprintf("Got it, %s is %s.", decoyThing, decoy))
	ev := b.add(2, fmt.Sprintf("They reset my %s this morning. The new one is %s; %s no longer works.", thing, fresh, old), fmt.Sprintf("Updated: your %s is now %s.", thing, fresh))
	return Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("code-update-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("What is my %s right now?", thing),
			ExpectedAnswer:    fresh,
			AnswerKind:        protocol.AnswerNumber,
			DistractorAnswers: []string{old, decoy},
		},
		RunAfterWave:    2,
		EvidencePairIDs: []string{ev},
	}
}

func (b *refBuilder) personUpdate(i int, role, former, current, other string) Case {
	b.add(0, fmt.Sprintf("My %s is %s, been with them for years.", role, former), fmt.Sprintf("Good to know your %s is %s.", role, former))
	b.add(1, fmt.Sprintf("My brother keeps raving about his %s, %s.", role, other), fmt.Sprintf("Sounds like your brother likes %s.", other))
	ev := b.add(2, fmt.Sprintf("I finally switched: my %s is %s now, I stopped seeing %s.", role, current, former), fmt.Sprintf("Noted, your %s is now %s.", role, current))
	return Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("person-update-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("Who is my %s these days?", role),
			ExpectedAnswer:    current,
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: []string{former, other},
		},
		RunAfterWave:    2,
		EvidencePairIDs: []string{ev},
	}
}

func (b *refBuilder) allergyList(i int, person, friend, first, second, decoy string) Case {
	ev := b.add(0, fmt.Sprintf("Cooking for %s this weekend. Careful: %s is allergic to %s and %s.", person, person, first, second), fmt.Sprintf("Understood, nothing with %s or %s for %s.", first, second, person))
	b.add(1, fmt.Sprintf("%s is coming too; %s can't have %s.", friend, friend, decoy), fmt.Sprintf("Noted, keep %s away from %s.", decoy, friend))
	return Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("allergy-list-%d", i),
			QuestionType:      "multi-item-recall",
			Question:          fmt.Sprintf("Which foods is %s allergic to?", person),
			ExpectedAnswer:    first + " and " + second,
			AnswerKind:        protocol.AnswerList,
			AnswerItems:       []string{first, second},
			DistractorAnswers: []string{decoy},
		},
		RunAfterWave:    2,
		EvidencePairIDs: []string{ev},
	}
}

func (b *refBuilder) venueMove(i int, event, from, to, decoy string) Case {
	b.add(0, fmt.Sprintf("The %s is booked in %s.", event, from), fmt.Sprintf("Great, the %s is in %s.", event, from))
	b.add(1, fmt.Sprintf("My cousin just moved to %s, maybe I'll visit.", decoy), fmt.Sprintf("Nice, %s could be a fun trip.", decoy))
	ev := b.add(2, fmt.Sprintf("Change of plans: the %s moved from %s to %s.", event, from, to), fmt.Sprintf("Updated, the %s is now in %s.", event, to))
	return Case{
		MemoryCase: protocol.MemoryCase{
			ID:                fmt.Sprintf("venue-move-%d", i),
			QuestionType:      "knowledge-update",
			Question:          fmt.Sprintf("Which city is the %s in?", event),
			ExpectedAnswer:    to,
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: []string{from, decoy},
		},
		RunAfterWave:    2,
		EvidencePairIDs: []string{ev},
	}
}

var refSurnames = []string{
	"Okonkwo", "Haldane", "Varga", "Nakashima", "Pereira", "Lindqvist", "Abernathy",
	"Moreau", "Castellano", "Iwu", "Kowalczyk", "Fairbanks", "Rahimi", "Delacroix",
	"Thorsen", "Achterberg", "Quiroga", "Mbeki", "Szabo", "Ellingsen",
}

var refFirstNames = []string{
	"Ines", "Tobias", "Marisol", "Henrik", "Aiyana", "Dmitri", "Noor", "Callum",
	"Yuki", "Bastian", "Leilani", "Oskar",
}

var refCities = []string{
	"Lisbon", "Tallinn", "Porto", "Ljubljana", "Valencia", "Krakow", "Bergen",
	"Ghent", "Bologna", "Tbilisi", "Seville", "Gdansk", "Leipzig", "Utrecht",
}

var refAllergens = []string{
	"peanuts", "shellfish", "sesame", "kiwi", "walnuts", "mustard", "celery",
	"lupin", "buckwheat", "mango", "soy", "cashews", "hazelnuts", "fennel",
}

var refCodeThings = []string{
	"gym locker code", "office door PIN", "storage unit gate code", "bike lock combination",
	"garage keypad code", "mailbox combination", "alarm panel code", "building intercom code",
	"server room PIN", "parking garage code",
}

var refRoles = []string{"dentist", "landlord", "team lead", "vet", "piano teacher", "accountant"}

var refEvents = []string{"team offsite", "book club retreat", "alumni reunion", "product summit", "family reunion"}

var refFiller = [][2]string{
	{"Remind me to water the plants on Thursday.", "I'll remind you Thursday about the plants."},
	{"I watched a great documentary about octopuses last night.", "Octopuses are fascinating, glad you enjoyed it."},
	{"Thinking about learning to bake sourdough.", "Sourdough is a fun project, start with a simple starter."},
	{"The weather has been unusually windy this week.", "Windy weeks can be draining, stay warm."},
	{"I need to renew my passport sometime this year.", "Good idea to start the renewal early."},
}
