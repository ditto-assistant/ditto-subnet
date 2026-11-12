package universe

import (
	"fmt"
	"math/rand"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/internal/publicdata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v19 shortcut-breaker families (DITTO-2737).
//
// The v18 top agents reach about .96 on the public memory half partly by
// writing this generator's habits down as rules. The released agent sources
// carry, among others:
//
//   - LESSON_RE: a question that says lesson, teach, learn, moral, "stuck with
//     me" or "remember next time" is answered with the saying the user wrote
//     down (the story lesson oracle).
//   - DATED_NOTE: go by the date each record was saved, never by the dates its
//     words name (story arcs record each change the day it happens).
//   - HOLDER_CHECK: a company, vendor or supplier is never the holder; holders
//     come only from people (story_v2.go, v14_role_families.go).
//   - a typo budget: any word within 1-3 edits of a name is the same name
//     (story aliases are at least 4 edits apart and people never collide).
//   - TASK_CHECK: in a "person ; task ; channel" next-step answer, drop any
//     time or condition the note adds.
//
// Policy allows these rules (I5 routed-guidance clarification, 2026-10-23),
// so v19 makes each of them cost points. Every family below is one where the
// rule's answer is wrong and a general reader's answer is right:
//
//   - trigger-fact: a question with a trigger word asks for a recorded fact
//     (a dose, a date, a count, a person). The thread also holds a saying
//     the user wrote down, and that saying is a distractor.
//   - late-recorded: a note saved on date R says a change took effect on an
//     earlier date E. An as-of question for a date between E and R needs the
//     new value; the saved-date reading gives the old one.
//   - company-holder: the item passes from a person to a company, which holds
//     it now. The person is the distractor.
//   - near-twin: two people whose first names are 1-2 edits apart (Dan/Dana),
//     each with their own fact. The other twin's later note is the distractor.
//     This is about people: the story subject-alias 4-edit guarantee is
//     untouched.
//   - conditional-next: the next step's task carries the condition or
//     deadline the note states, and the expected task phrase includes it, so
//     a reply that drops it misses the action claim. Grading is the shipped
//     claim grader unchanged: the full sentence contains the expected phrase.
//
// Each case is a self-contained thread in its own session, so the families
// add no state to the v15 world and every older seed is byte-identical. Names
// the world already uses are never drawn. V19BreakerCase.Facts is the
// validator-side record model: the oracle reader and the shortcut readers in
// v19_breakers_test.go both answer from it, and every value they answer is
// also in the rendered note text.

// V19 breaker family question types.
const (
	V19BreakerTriggerFact     = "v19-breaker-trigger-fact"
	V19BreakerLateRecorded    = "v19-breaker-late-recorded"
	V19BreakerCompanyHolder   = "v19-breaker-company-holder"
	V19BreakerNearTwin        = "v19-breaker-near-twin"
	V19BreakerConditionalNext = "v19-breaker-conditional-next"
)

// V19BreakerFamilies is the family bank in a stable order; the per-seed
// order is a permutation of it.
var V19BreakerFamilies = []string{
	V19BreakerTriggerFact, V19BreakerLateRecorded, V19BreakerCompanyHolder,
	V19BreakerNearTwin, V19BreakerConditionalNext,
}

// V19TriggerWords are the words LESSON_RE-style routing keys on. Every
// trigger-fact question carries at least one, protected from typo noise.
var V19TriggerWords = []string{"lesson", "teach", "taught", "learn", "moral", "stuck with me", "remember next time"}

// V19BreakerRevision names the generator contract in case provenance.
const V19BreakerRevision = "v19-shortcut-breakers-v1"

// V19BreakerFact is one recorded fact in a breaker thread. Validator-side
// only; it never reaches the harness.
type V19BreakerFact struct {
	PairID string
	// Saved is the record's timestamp. Effective is when the fact took hold,
	// equal to Saved unless the note names an earlier date.
	Saved, Effective time.Time
	// About is the entity the fact is about: the thread subject, or a
	// person's first name in the near-twin family.
	About string
	// Kind is "fact", "saying", "holder" or "next".
	Kind  string
	Value string
	// Company marks a holder that is an organisation, not a person.
	Company bool
	// Who, Task, Condition and Channel are the parts of a next step.
	Who, Task, Condition, Channel string
}

// V19BreakerCase is one generated breaker case.
type V19BreakerCase struct {
	Family string
	Plan   QuestionPlan
	Pairs  []protocol.MemoryPair
	Facts  []V19BreakerFact
	// About is the entity the question asks about; AsOf is the as-of date of
	// a late-recorded question.
	About string
	AsOf  time.Time
}

// v19BreakerBuilder carries the per-seed draw state: the names in use, so no
// breaker name collides with the world or another breaker case.
type v19BreakerBuilder struct {
	seed  int64
	used  map[string]bool // lower-case first names, full names and org stems
	world []string        // lower-case first names and nicknames of the world
	// draws feeds the humandata and publicdata banks; ordinal steers their
	// head/long-tail strata.
	draws   *rand.Rand
	ordinal int
}

func newV19BreakerBuilder(seed int64, w World) *v19BreakerBuilder {
	b := &v19BreakerBuilder{seed: seed, used: map[string]bool{}, draws: rand.New(rand.NewSource(v10Seed(seed, "v19-breakers:banks")))}
	add := func(name string) {
		name = strings.ToLower(strings.TrimSpace(name))
		if name == "" {
			return
		}
		b.used[name] = true
		if first := strings.Fields(name)[0]; first != name {
			b.used[first] = true
			b.world = append(b.world, first)
		} else {
			b.world = append(b.world, name)
		}
	}
	add(w.UserName)
	for _, p := range w.People {
		add(p.Name)
		add(p.Nickname)
	}
	return b
}

// index draws a seeded index in [0,n) for one named choice.
func (b *v19BreakerBuilder) index(salt string, n int) int {
	return int(v10Seed(b.seed, "v19-breakers:"+salt) % int64(n))
}

// pick draws one entry of bank.
func (b *v19BreakerBuilder) pick(salt string, bank []string) string {
	return bank[b.index(salt, len(bank))]
}

// nameFree reports whether a first name is at least minEdits from every
// world name and every single name already drawn.
func (b *v19BreakerBuilder) nameFree(first string, minEdits int) bool {
	lower := strings.ToLower(first)
	if b.used[lower] || strings.Contains(lower, " ") {
		return false
	}
	for _, name := range b.world {
		if levenshtein(name, lower) < minEdits {
			return false
		}
	}
	for name := range b.used {
		if !strings.Contains(name, " ") && levenshtein(name, lower) < minEdits {
			return false
		}
	}
	return true
}

// given draws a humandata given name at least three edits from every other
// name in the run, so only a near twin is ever a near name.
func (b *v19BreakerBuilder) given() string {
	for {
		name := humandata.GivenName(b.draws, b.ordinal)
		b.ordinal++
		if b.nameFree(name, 3) {
			b.used[strings.ToLower(name)] = true
			return name
		}
	}
}

// person draws an unused full name from the humandata banks.
func (b *v19BreakerBuilder) person() string {
	return b.given() + " " + humandata.Surname(b.draws, b.ordinal)
}

// org draws an unused publicdata organisation stem.
func (b *v19BreakerBuilder) org() string {
	for {
		stem := publicdata.OrgStem(b.draws)
		if key := "org:" + strings.ToLower(stem); !b.used[key] && !strings.Contains(stem, " ") {
			b.used[key] = true
			return stem
		}
	}
}

// city draws an unused publicdata place name.
func (b *v19BreakerBuilder) city() string {
	for {
		city := publicdata.City(b.draws, b.ordinal)
		b.ordinal++
		if key := "city:" + strings.ToLower(city); !b.used[key] {
			b.used[key] = true
			return city
		}
	}
}

// twins draws an unused near-twin pair; both names must be free.
func (b *v19BreakerBuilder) twins(salt string) [2]string {
	start := b.index(salt, len(v19BreakerTwinNames))
	for i := range v19BreakerTwinNames {
		pair := v19BreakerTwinNames[(start+i)%len(v19BreakerTwinNames)]
		if !b.twinFree(pair[0]) || !b.twinFree(pair[1]) {
			continue
		}
		b.used[strings.ToLower(pair[0])], b.used[strings.ToLower(pair[1])] = true, true
		return pair
	}
	panic("v19 breakers: twin-name pool exhausted")
}

// twinFree keeps a twin name at least two edits from every world name and
// every name already drawn, so its twin is its only one-edit neighbour among
// the run's people. Short names cannot keep three edits from a 30-person
// world.
func (b *v19BreakerBuilder) twinFree(first string) bool { return b.nameFree(first, 2) }

// stamp returns a business-day instant days after the case's start.
func (b *v19BreakerBuilder) stamp(ordinal, days int, salt string) time.Time {
	start := time.Date(2025, 1, 6, 9, 0, 0, 0, time.UTC)
	offset := b.index(fmt.Sprintf("start/%d", ordinal), 90)
	at := protocol.ShiftBusinessDays(start, offset, b.index(fmt.Sprintf("start-minute/%d", ordinal), 600))
	return protocol.ShiftBusinessDays(at, days, b.index(fmt.Sprintf("minute/%d/%s", ordinal, salt), 600))
}

// filler draws one aside, so a thread reads like a real note.
func (b *v19BreakerBuilder) filler(salt string) string {
	return b.pick("filler/"+salt, v19BreakerFillers)
}

func v19Date(at time.Time) string { return fmt.Sprintf("%s %d", at.Month(), at.Day()) }

func v19DateYear(at time.Time) string {
	return fmt.Sprintf("%s %d, %d", at.Month(), at.Day(), at.Year())
}

func v19DateClaim(at time.Time) protocol.Claim {
	accept := V13DateAccept(at.Year(), int(at.Month()), at.Day())
	return protocol.Claim{Kind: protocol.ClaimKindDate, Expected: at.Format("2006-01-02"), Accept: accept, Unit: "day", Critical: true, Weight: 1}
}

// GenerateV19Breakers returns count breaker cases for a seed, cycling the
// seed's family order so each family gets count/5 cases (the v19 full table
// asks two of each). Bench v19 and later only.
func GenerateV19Breakers(seed int64, w World, count int) ([]V19BreakerCase, error) {
	if count <= 0 {
		return nil, fmt.Errorf("v19 breakers: need a positive case count, got %d", count)
	}
	b := newV19BreakerBuilder(seed, w)
	order := v13Perm(seed, "v19-breaker-families", len(V19BreakerFamilies))
	out := make([]V19BreakerCase, 0, count)
	for ordinal := 0; ordinal < count; ordinal++ {
		family := V19BreakerFamilies[order[ordinal%len(order)]]
		variant := ordinal / len(order)
		var c V19BreakerCase
		switch family {
		case V19BreakerTriggerFact:
			c = b.triggerFact(ordinal, variant)
		case V19BreakerLateRecorded:
			c = b.lateRecorded(ordinal, variant)
		case V19BreakerCompanyHolder:
			c = b.companyHolder(ordinal, variant)
		case V19BreakerNearTwin:
			c = b.nearTwin(ordinal, variant)
		case V19BreakerConditionalNext:
			c = b.conditionalNext(ordinal, variant)
		}
		c.Family = family
		if err := b.finish(&c, ordinal); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, nil
}

// v19Note is one record of a thread before it is stamped and identified.
type v19Note struct {
	at     time.Time
	prompt string
	fact   V19BreakerFact
	facts  []V19BreakerFact // extra facts the same note states
}

// thread renders notes as pairs in one session and binds each fact to its
// record.
func (b *v19BreakerBuilder) thread(c *V19BreakerCase, ordinal int, notes []v19Note) {
	session := protocol.OpaqueCaseID(b.seed, "v19-breaker-session", ordinal)
	for i, note := range notes {
		id := protocol.OpaqueCaseID(b.seed, fmt.Sprintf("v19-breaker-pair/%d", ordinal), i)
		c.Pairs = append(c.Pairs, protocol.MemoryPair{
			PairID: id, SessionID: session, Timestamp: note.at.Format(time.RFC3339),
			Prompt:   note.prompt,
			Response: b.pick(fmt.Sprintf("response/%d/%d", ordinal, i), v19BreakerResponses),
		})
		for _, fact := range append([]V19BreakerFact{note.fact}, note.facts...) {
			if fact.Kind == "" {
				continue
			}
			fact.PairID, fact.Saved = id, note.at
			if fact.Effective.IsZero() {
				fact.Effective = note.at
			}
			c.Facts = append(c.Facts, fact)
		}
	}
}

// finish stamps the case identity and checks the contract every family
// shares: the expected answer is in the notes and never in the question.
func (b *v19BreakerBuilder) finish(c *V19BreakerCase, ordinal int) error {
	id := protocol.OpaqueCaseID(b.seed, "v19-breaker-case", ordinal)
	mc := &c.Plan.Case
	mc.BenchVersion = protocol.BenchVersionV19
	mc.ID, mc.QuestionID, mc.QuestionType = id, id, c.Family
	mc.Claims = append([]protocol.Claim(nil), c.Plan.Claims...)
	mc.WritingProtected = append([]string(nil), c.Plan.Constraints...)
	c.Plan.RequiredPairIDs = nil
	for _, pair := range c.Pairs {
		c.Plan.RequiredPairIDs = append(c.Plan.RequiredPairIDs, pair.PairID)
	}
	var text strings.Builder
	for _, pair := range c.Pairs {
		text.WriteString(pair.Prompt + "\n")
	}
	for _, claim := range c.Plan.Claims {
		if claim.Kind == protocol.ClaimKindDate {
			continue // a date is written in prose; its accept forms cover it
		}
		if !strings.Contains(strings.ToLower(text.String()), strings.ToLower(claim.Expected)) {
			return fmt.Errorf("v19 breaker %s: claim %q is not in its notes", c.Family, claim.Expected)
		}
	}
	for _, claim := range c.Plan.Claims {
		if claim.Kind != protocol.ClaimKindDate && grade.Hit(claim.Expected, mc.Question) {
			return fmt.Errorf("v19 breaker %s: question leaks %q", c.Family, claim.Expected)
		}
	}
	return nil
}

// triggerFact: a trigger-word question about a recorded fact, with the
// user's saying in the same thread as the bait.
func (b *v19BreakerBuilder) triggerFact(ordinal, variant int) V19BreakerCase {
	key := func(s string) string { return fmt.Sprintf("trigger/%d/%s", ordinal, s) }
	frame := v13Perm(b.seed, "v19-trigger-frames", 4)[variant%4]
	saying := storyV13Lessons[b.index(key("saying"), len(storyV13Lessons))].canonical
	var c V19BreakerCase
	var factNote string
	var fact V19BreakerFact
	var question, topic string
	switch frame {
	case 0: // a dose
		pet := b.given()
		med := b.pick(key("med"), v19BreakerMedicines)
		dose := fmt.Sprintf("%d.5 ml", 1+b.index(key("dose"), 4))
		c.About, topic = pet, pet+"'s vet visit"
		factNote = fmt.Sprintf("Took %s to the vet this morning. The vet showed me how to give the %s and was clear about the dose: %s, twice a day with food.", pet, med, dose)
		fact = V19BreakerFact{About: pet, Kind: "fact", Value: dose}
		question = b.pick(key("q"), []string{
			fmt.Sprintf("What did the vet teach me about %s's dose?", pet),
			fmt.Sprintf("What stuck with me from the vet about how much %s to give %s?", med, pet),
		})
		c.Plan.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindValue, dose, []string{dose, strings.Replace(dose, " ml", "ml", 1), strings.Replace(dose, " ml", " millilitres", 1), strings.Replace(dose, " ml", " milliliters", 1)}, 1)}
		c.Plan.Constraints = []string{pet, med}
	case 1: // a date
		thing := b.pick(key("thing"), v19BreakerServices)
		due := b.stamp(ordinal, 120+b.index(key("due"), 60), "due")
		c.About, topic = thing, "the "+thing
		factNote = fmt.Sprintf("The engineer came for the %s. Learned something useful while they worked: the next one is due on %s.", thing, v19DateYear(due))
		fact = V19BreakerFact{About: thing, Kind: "fact", Value: due.Format("2006-01-02")}
		question = b.pick(key("q"), []string{
			fmt.Sprintf("What did I learn from the engineer about when the next %s is due?", thing),
			fmt.Sprintf("The %s visit was a lesson in timing. When is the next one due?", thing),
		})
		c.Plan.Claims = []protocol.Claim{v19DateClaim(due)}
		c.Plan.Constraints = []string{thing, v19DateYear(due)}
	case 2: // a person
		skill := b.pick(key("skill"), v19BreakerSkills)
		who := b.person()
		c.About, topic = skill, "learning to "+skill
		factNote = fmt.Sprintf("%s came round on Saturday and showed me how to %s properly. Took ten minutes once I watched it done.", who, skill)
		fact = V19BreakerFact{About: skill, Kind: "fact", Value: who}
		question = b.pick(key("q"), []string{
			fmt.Sprintf("Who did I learn to %s from?", skill),
			fmt.Sprintf("Who was it that taught me how to %s? The lesson was theirs.", skill),
		})
		c.Plan.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, who, []string{who}, 1)}
		c.Plan.Constraints = []string{who, skill}
	default: // a count
		place := b.pick(key("place"), v19BreakerPlaces)
		days := 14 + 7*b.index(key("days"), 3)
		c.About, topic = "parking ticket", "the parking ticket"
		factNote = fmt.Sprintf("Got a parking ticket outside %s. Rang the council: the clerk said I have %d days to appeal it, and the form is online.", place, days)
		fact = V19BreakerFact{About: "parking ticket", Kind: "fact", Value: fmt.Sprintf("%d days", days)}
		question = b.pick(key("q"), []string{
			"What was the lesson from the parking ticket: how many days do I have to appeal it?",
			"About the parking ticket and the moral of it all: how many days did the council give me to appeal?",
		})
		c.Plan.Claims = []protocol.Claim{{Kind: protocol.ClaimKindQuantity, Expected: fmt.Sprintf("%d", days), Unit: "days", Critical: true, Weight: 1}}
		c.Plan.Constraints = []string{"parking ticket", place}
	}
	lessonNote := fmt.Sprintf("%s Still thinking about %s. The lesson I'm taking from it: \"%s\". That one stuck with me.", b.filler(key("filler")), topic, saying)
	b.thread(&c, ordinal, []v19Note{
		{at: b.stamp(ordinal, 0, "fact"), prompt: factNote, fact: fact},
		{at: b.stamp(ordinal, 1+b.index(key("gap"), 3), "saying"), prompt: lessonNote, fact: V19BreakerFact{About: c.About, Kind: "saying", Value: saying}},
	})
	c.Plan.Case = protocol.MemoryCase{Question: question, AnswerKind: protocol.AnswerValue, ExpectedAnswer: c.Plan.Claims[0].Expected,
		AcceptAny: c.Plan.Claims[0].Accept, DistractorAnswers: []string{saying}}
	if c.Plan.Claims[0].Kind == protocol.ClaimKindDate {
		c.Plan.Case.AnswerKind = protocol.AnswerDate
	}
	if c.Plan.Claims[0].Kind == protocol.ClaimKindQuantity {
		c.Plan.Case.AnswerKind = protocol.AnswerNumber
	}
	c.Plan.Constraints = append(c.Plan.Constraints, saying)
	for _, word := range V19TriggerWords {
		if strings.Contains(strings.ToLower(question), word) {
			c.Plan.Constraints = append(c.Plan.Constraints, word)
		}
	}
	return c
}

// lateRecorded: the change note is saved after the date it took effect, and
// the question asks about a date in between.
func (b *v19BreakerBuilder) lateRecorded(ordinal, variant int) V19BreakerCase {
	key := func(s string) string { return fmt.Sprintf("late/%d/%s", ordinal, s) }
	frame := v13Perm(b.seed, "v19-late-frames", 4)[variant%4]
	var subject, attr, old, current string
	kind := protocol.ClaimKindValue
	switch frame {
	case 0:
		subject, attr = "the "+b.org()+" supplier contract", "account manager"
		old, current = b.person(), b.person()
		kind = protocol.ClaimKindPerson
	case 1:
		subject, attr = "the "+b.org()+" office lease", "unit"
		units := v13Perm(b.seed, key("units"), 40)
		old, current = fmt.Sprintf("Unit %d", 10+units[0]), fmt.Sprintf("Unit %d", 10+units[1])
	case 2:
		subject, attr = "the "+b.org()+" freight account", "collection depot"
		old, current = b.city()+" depot", b.city()+" depot"
	default:
		subject, attr = "the "+b.org()+" support plan", "support line"
		lines := v13Perm(b.seed, key("lines"), 9000)
		old, current = fmt.Sprintf("extension %d", 1000+lines[0]), fmt.Sprintf("extension %d", 1000+lines[1])
	}
	start := b.stamp(ordinal, 0, "start")
	effective := protocol.ShiftBusinessDays(start, 15+b.index(key("effective"), 10), 0)
	asOf := protocol.ShiftBusinessDays(effective, 5+b.index(key("asof"), 5), 0)
	recorded := protocol.ShiftBusinessDays(asOf, 8+b.index(key("recorded"), 8), b.index(key("recorded-minute"), 600))
	c := V19BreakerCase{About: subject, AsOf: time.Date(asOf.Year(), asOf.Month(), asOf.Day(), 12, 0, 0, 0, time.UTC)}
	b.thread(&c, ordinal, []v19Note{
		{at: start, prompt: fmt.Sprintf("Signed off %s today. For the record, the %s is %s. %s", subject, attr, old, b.filler(key("filler0"))),
			fact: V19BreakerFact{About: subject, Kind: "fact", Value: old}},
		{at: recorded, prompt: fmt.Sprintf("%s Only just opened the letter about %s: the %s changed to %s, in effect from %s. Nobody told me at the time.", b.filler(key("filler1")), subject, attr, current, v19Date(effective)),
			fact: V19BreakerFact{About: subject, Kind: "fact", Value: current, Effective: effective}},
	})
	question := b.pick(key("q"), []string{
		fmt.Sprintf("Which %s applied to %s on %s?", attr, subject, v19DateYear(asOf)),
		fmt.Sprintf("On %s, what was the %s for %s?", v19DateYear(asOf), attr, subject),
		fmt.Sprintf("For %s: what %s was in effect as of %s?", subject, attr, v19DateYear(asOf)),
	})
	c.Plan.Claims = []protocol.Claim{v13Claim(kind, current, []string{current}, 1)}
	c.Plan.Case = protocol.MemoryCase{Question: question, AnswerKind: protocol.AnswerValue, ExpectedAnswer: current,
		AcceptAny: []string{current}, DistractorAnswers: []string{old}}
	c.Plan.Constraints = []string{subject, old, current, v19Date(effective), v19DateYear(asOf)}
	return c
}

// companyHolder: an item passes from a person to a company that holds it
// now.
func (b *v19BreakerBuilder) companyHolder(ordinal, variant int) V19BreakerCase {
	key := func(s string) string { return fmt.Sprintf("holder/%d/%s", ordinal, s) }
	frame := v13Perm(b.seed, "v19-holder-frames", len(v19BreakerHoldings))[variant%len(v19BreakerHoldings)]
	h := v19BreakerHoldings[frame]
	company := b.org() + " " + h.trade
	person := b.person()
	role := publicdata.Occupation(b.draws)
	c := V19BreakerCase{About: h.item}
	b.thread(&c, ordinal, []v19Note{
		{at: b.stamp(ordinal, 0, "first"), prompt: fmt.Sprintf("Handed %s to %s (our %s) while %s. %s", h.item, person, role, h.reason, b.filler(key("filler0"))),
			fact: V19BreakerFact{About: h.item, Kind: "holder", Value: person}},
		{at: b.stamp(ordinal, 2+b.index(key("gap"), 3), "second"), prompt: fmt.Sprintf("%s took over %s from %s this morning and keeps %s until %s. %s", company, h.item, person, h.item, h.until, b.filler(key("filler1"))),
			fact: V19BreakerFact{About: h.item, Kind: "holder", Value: company, Company: true}},
	})
	question := b.pick(key("q"), []string{
		fmt.Sprintf("Who has %s now?", h.item),
		fmt.Sprintf("Who is holding %s at the moment?", h.item),
		fmt.Sprintf("Where are things with %s: who has custody right now?", h.item),
	})
	c.Plan.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindOrganisation, company, []string{company}, 1)}
	c.Plan.Case = protocol.MemoryCase{Question: question, AnswerKind: protocol.AnswerValue, ExpectedAnswer: company,
		AcceptAny: []string{company}, DistractorAnswers: []string{person}}
	c.Plan.Constraints = []string{h.item, company, person}
	return c
}

// nearTwin: two people one or two edits apart, each with their own fact; the
// question names the twin whose note came first.
func (b *v19BreakerBuilder) nearTwin(ordinal, variant int) V19BreakerCase {
	key := func(s string) string { return fmt.Sprintf("twin/%d/%s", ordinal, s) }
	pair := b.twins(key("names"))
	asked, other := pair[0], pair[1]
	if b.index(key("which"), 2) == 1 {
		asked, other = other, asked
	}
	relations := v13Perm(b.seed, key("relations"), len(v19BreakerRelations))
	frame := v13Perm(b.seed, "v19-twin-frames", 3)[variant%3]
	first, second := b.stamp(ordinal, 0, "first"), b.stamp(ordinal, 2+b.index(key("gap"), 4), "second")
	c := V19BreakerCase{About: asked}
	var notes, values [2]string
	var protect []string
	var claim protocol.Claim
	var question string
	switch frame {
	case 0:
		vals := v13Perm(b.seed, key("allergy"), len(v19BreakerAllergies))
		values = [2]string{v19BreakerAllergies[vals[0]], v19BreakerAllergies[vals[1]]}
		notes[0] = fmt.Sprintf("%s (%s) mentioned at dinner being allergic to %s, so no more of that at ours.", asked, v19BreakerRelations[relations[0]], values[0])
		notes[1] = fmt.Sprintf("Note for the party: %s (%s) is allergic to %s.", other, v19BreakerRelations[relations[1]], values[1])
		question = fmt.Sprintf("What is %s allergic to?", asked)
		claim = v13Claim(protocol.ClaimKindValue, values[0], []string{values[0]}, 1)
	case 1:
		values = [2]string{b.city(), b.city()}
		notes[0] = fmt.Sprintf("%s (%s) has finally moved to %s. Need to send the new address card.", asked, v19BreakerRelations[relations[0]], values[0])
		notes[1] = fmt.Sprintf("Heard %s (%s) is moving to %s for the new job.", other, v19BreakerRelations[relations[1]], values[1])
		question = fmt.Sprintf("Which city did %s move to?", asked)
		claim = v13Claim(protocol.ClaimKindValue, values[0], []string{values[0]}, 1)
	default:
		days := v13Perm(b.seed, key("birthday"), 200)
		year := time.Date(2025, 1, 1, 12, 0, 0, 0, time.UTC)
		born := [2]time.Time{year.AddDate(0, 0, 20+days[0]), year.AddDate(0, 0, 20+days[1])}
		values = [2]string{born[0].Format("2006-01-02"), born[1].Format("2006-01-02")}
		protect = []string{v19Date(born[0]), v19Date(born[1])}
		notes[0] = fmt.Sprintf("%s's birthday is %s. %s is my %s; put it in the calendar.", asked, v19Date(born[0]), asked, strings.TrimPrefix(v19BreakerRelations[relations[0]], "my "))
		notes[1] = fmt.Sprintf("Reminder: %s (%s) has a birthday on %s.", other, v19BreakerRelations[relations[1]], v19Date(born[1]))
		question = fmt.Sprintf("When is %s's birthday?", asked)
		claim = v19DateClaim(born[0])
	}
	b.thread(&c, ordinal, []v19Note{
		{at: first, prompt: notes[0] + " " + b.filler(key("filler0")), fact: V19BreakerFact{About: asked, Kind: "fact", Value: values[0]}},
		{at: second, prompt: b.filler(key("filler1")) + " " + notes[1], fact: V19BreakerFact{About: other, Kind: "fact", Value: values[1]}},
	})
	c.Plan.Claims = []protocol.Claim{claim}
	distractor := values[1]
	if claim.Kind == protocol.ClaimKindDate {
		at, _ := time.Parse("2006-01-02", values[1])
		distractor = v19Date(at)
	}
	c.Plan.Case = protocol.MemoryCase{Question: question, AnswerKind: protocol.AnswerValue, ExpectedAnswer: claim.Expected,
		AcceptAny: claim.Accept, DistractorAnswers: []string{distractor}}
	if claim.Kind == protocol.ClaimKindDate {
		c.Plan.Case.AnswerKind = protocol.AnswerDate
	}
	c.Plan.Constraints = append([]string{asked, other, values[0], values[1]}, protect...)
	return c
}

// conditionalNext: the next step's task keeps the condition or deadline the
// note states.
func (b *v19BreakerBuilder) conditionalNext(ordinal, variant int) V19BreakerCase {
	key := func(s string) string { return fmt.Sprintf("next/%d/%s", ordinal, s) }
	subject := v19BreakerProjects[v13Perm(b.seed, "v19-next-projects", len(v19BreakerProjects))[variant%len(v19BreakerProjects)]]
	tasks := v13Perm(b.seed, key("tasks"), len(storyNextActions))
	task, otherTask := storyNextActions[tasks[0]], storyNextActions[tasks[1]]
	condition := b.pick(key("condition"), v19BreakerConditions)
	channels := v13Perm(b.seed, key("channels"), len(storyChannels))
	channel, otherChannel := storyChannels[channels[0]].channel, storyChannels[channels[1]].channel
	who, before := b.person(), b.person()
	how := map[string]string{"email": "by email", "phone": "by phone", "chat": "over chat", "in person": "in person"}
	c := V19BreakerCase{About: subject}
	b.thread(&c, ordinal, []v19Note{
		{at: b.stamp(ordinal, 0, "first"), prompt: fmt.Sprintf("Kicked off %s. %s is handling the paperwork for now. %s", subject, before, b.filler(key("filler0")))},
		{at: b.stamp(ordinal, 2+b.index(key("gap"), 3), "second"), prompt: fmt.Sprintf("%s Next step on %s: %s will %s %s, %s.", b.filler(key("filler1")), subject, who, task.what, condition, how[channel]),
			fact: V19BreakerFact{About: subject, Kind: "next", Who: who, Task: task.what, Condition: condition, Channel: channel}},
	})
	action := task.what + " " + condition
	accept := []string{action, condition + ", " + task.what, condition + " " + task.what}
	for _, alt := range task.accept {
		if len(strings.Fields(alt)) > 1 {
			accept = append(accept, alt+" "+condition)
		}
	}
	question := b.pick(key("q"), []string{
		fmt.Sprintf("What is the next step on %s: who does what, and how?", subject),
		fmt.Sprintf("For %s, tell me the next action: person, task, and channel.", subject),
		fmt.Sprintf("Who has the next move on %s, what is it exactly, and over which channel?", subject),
	})
	items := []string{who, action, channel}
	c.Plan.Claims = []protocol.Claim{
		{Kind: protocol.ClaimKindEntity, Expected: who, Accept: []string{who}, Critical: true, Weight: 1},
		{Kind: protocol.ClaimKindAction, Expected: action, Accept: accept, Critical: true, Weight: 1},
		{Kind: protocol.ClaimKindChannel, Expected: channel, Accept: storyChannelAccept(channel), Critical: false, Weight: 1},
	}
	c.Plan.Case = protocol.MemoryCase{Question: question, AnswerKind: protocol.AnswerList, ExpectedAnswer: strings.Join(items, "; "),
		AnswerItems: items, AnswerItemKinds: []string{protocol.AnswerValue, protocol.AnswerValue, protocol.AnswerValue},
		AnswerItemAcceptAny: [][]string{{who}, accept, storyChannelAccept(channel)},
		DistractorAnswers:   []string{before, otherTask.what, otherChannel}}
	c.Plan.Constraints = []string{subject, who, before, condition}
	return c
}

// v19BreakerTwinNames are first-name pairs one or two edits apart.
var v19BreakerTwinNames = [][2]string{
	{"Dan", "Dana"}, {"Jon", "John"}, {"Ana", "Anna"}, {"Carl", "Carla"}, {"Eric", "Erica"},
	{"Mark", "Marc"}, {"Lena", "Lana"}, {"Sara", "Sarah"}, {"Alex", "Alexa"}, {"Ian", "Iain"},
	{"Paul", "Paula"}, {"Toni", "Tony"}, {"Jean", "Joan"}, {"Mila", "Milo"}, {"Bren", "Brent"},
	{"Nico", "Nika"}, {"Rory", "Rosy"}, {"Leon", "Leona"}, {"Ivo", "Ivor"},
	{"Hugo", "Hugh"}, {"Ezra", "Ezri"}, {"Bea", "Bev"}, {"Kai", "Kaia"}, {"Orla", "Orly"},
	{"Theo", "Thea"}, {"Remi", "Remy"}, {"Cass", "Cash"}, {"Fern", "Finn"}, {"Rosa", "Rose"},
}

// v19BreakerHoldings are the work items a company comes to hold: the item,
// the holder's trade, why it left the office, and until when the company
// holds it.
var v19BreakerHoldings = []struct{ item, trade, reason, until string }{
	{"the signed NDA", "Solicitors", "legal reviewed the clauses", "the deal closes"},
	{"the prototype", "Test Labs", "the drop tests were scheduled", "the certification report is out"},
	{"the old file server", "Data Centres", "the office was rewired", "the migration finishes"},
	{"the trade show stand", "Exhibition Storage", "the expo was postponed", "the autumn fair"},
	{"the company van", "Motors", "the fleet was being audited", "the gearbox is fixed"},
	{"the spare office keys", "Facilities", "the locks were changed", "the new fit-out is done"},
	{"the archive boxes", "Records Management", "the basement flooded", "the retention review ends"},
	{"the demo laptops", "IT Services", "the sales team was travelling", "they are re-imaged"},
	{"the payroll records", "Accountants", "the audit was due", "the year-end close"},
	{"the product samples", "Freight", "the warehouse was full", "the client confirms delivery"},
	{"the signed contracts", "Notaries", "the board approved them", "they are apostilled"},
	{"the backup tapes", "Secure Storage", "the server room was cleared", "the next quarterly rotation"},
	{"the event banners", "Print Works", "the colours were wrong", "the reprint is approved"},
	{"the warranty returns", "Repairs", "the support queue was backed up", "the faults are logged"},
	{"the company car", "Leasing", "the lease came up for renewal", "the new lease starts"},
	{"the patent drawings", "Patent Attorneys", "the filing deadline moved", "the filing is lodged"},
	{"the tender documents", "Bid Management", "the deadline was tight", "submission day"},
	{"the office plants", "Interiors", "the floor was being recarpeted", "the move back"},
	{"the forklift", "Plant Hire", "the warehouse was quiet", "the peak season"},
	{"the original invoices", "Debt Recovery", "the client stopped paying", "the claim settles"},
	{"the studio cameras", "AV Hire", "the shoot was cancelled", "the next campaign"},
	{"the staff handbooks", "HR Consulting", "the policies were rewritten", "the review is signed off"},
	{"the shipping pallets", "Logistics", "the loading bay was resurfaced", "the bay reopens"},
	{"the company seal", "Company Secretarial", "the directors changed", "the filings are made"},
}

// v19BreakerRelations are gender-neutral: a drawn name implies nothing.
var v19BreakerRelations = []string{
	"my cousin", "a neighbour", "my colleague", "my teammate", "a friend from climbing", "my old flatmate",
	"a friend from work", "my flatmate", "a school friend", "my running partner", "a friend from choir",
	"my landlord", "a parent from the school run", "my study partner", "my old manager", "my godchild",
	"a friend from book club", "my sibling", "my in-law", "a friend from the allotment", "my dentist",
	"a friend from uni", "my carpool buddy", "my climbing partner", "my former tutor",
}

// v19BreakerAllergies are the allergies a near twin records.
var v19BreakerAllergies = []string{
	"peanuts", "shellfish", "penicillin", "sesame", "latex", "kiwi fruit", "bee stings", "mustard",
	"tree nuts", "soy", "celery", "lupin", "sulphites", "buckwheat", "cat dander", "dust mites",
	"ibuprofen", "aspirin", "strawberries", "mango", "avocado", "pine nuts", "coconut", "wasp stings",
	"horse hair", "sunflower seeds",
}

// v19BreakerMedicines, v19BreakerServices, v19BreakerSkills and
// v19BreakerPlaces are the trigger-fact subjects a question names.
var v19BreakerMedicines = []string{
	"ear drops", "anti-inflammatory", "antibiotic syrup", "joint supplement", "eye drops", "worming paste",
	"pain relief", "allergy tablets", "thyroid tablets", "heart medicine", "liquid antacid", "probiotic paste",
	"steroid syrup", "kidney diet supplement", "flea treatment", "calming drops", "liver tonic",
	"cough syrup", "vitamin drops", "antifungal drops", "insulin", "seizure medicine", "skin supplement",
	"appetite stimulant",
}

var v19BreakerServices = []string{
	"boiler service", "chimney sweep", "gutter clean", "septic tank check", "gas safety check",
	"electrical inspection", "fire alarm test", "water softener service", "lift service", "roof inspection",
	"damp survey", "drain clearance", "aircon service", "solar panel check", "radon test",
	"heat pump service", "pest control visit", "window reseal", "pool filter service", "smoke vent check",
	"well water test", "wood burner sweep", "oil tank inspection", "garage door service",
}

var v19BreakerSkills = []string{
	"bleed the radiators", "reset the fuse box", "descale the kettle", "re-pot the fig tree",
	"change a tap washer", "sharpen the kitchen knives", "prune the roses", "patch a bike tyre",
	"unblock the sink trap", "re-grout the shower", "wire a plug", "darn a sock", "bake sourdough",
	"tune the guitar", "read the electricity meter", "jump-start the car", "fillet a mackerel",
	"oil the garden bench", "seal the bath edge", "replace the toilet flapper", "rehang the door",
	"set the oven clock", "re-string the blinds", "bleed the brakes",
}

var v19BreakerPlaces = []string{
	"the library", "the station car park", "the dentist", "the farmers' market", "the leisure centre",
	"the school gates", "the post office", "the town hall", "the vet's", "the garden centre",
	"the cinema", "the hospital", "the swimming baths", "the bakery", "the church hall", "the museum",
	"the climbing wall", "the bike shop", "the bowling alley", "the theatre", "the community centre",
	"the market square", "the retail park", "the old mill",
}

// v19BreakerProjects are the conditional-next work threads.
var v19BreakerProjects = []string{
	"the supplier onboarding", "the office move", "the Q3 vendor review", "the payroll switch",
	"the website relaunch", "the warehouse lease renewal", "the CRM migration", "the trade show booking",
	"the insurance renewal", "the audit prep", "the new starter's laptop", "the fleet replacement",
	"the client workshop", "the brochure reprint", "the hiring panel", "the expense policy update",
	"the server upgrade", "the packaging redesign", "the pricing review", "the security assessment",
	"the board pack", "the logistics tender", "the annual report", "the office fit-out",
}

// v19BreakerConditions are the deadlines and conditions a next step carries.
var v19BreakerConditions = []string{
	"before Friday", "once the deposit clears", "after the survey comes back", "by the end of the month",
	"once it is signed", "only if the quote stays under budget", "before the 14th", "as soon as the permit arrives",
	"before the bank holiday", "once the builders confirm", "after the inspection", "by Wednesday lunchtime",
	"once the insurer agrees", "before the school term starts", "after the plaster dries", "by the 30th",
	"once the parts arrive", "only if the weather holds", "before the landlord's visit", "after the neighbours sign off",
	"by next Tuesday", "once the invoice is paid", "before the price rise", "as soon as the skip is gone",
}

var v19BreakerFillers = []string{
	"The dog decided the sofa was hers again.", "The heating clicked on for the first time this year.",
	"Lost an hour to a parking app.", "Half my notes are voice memos at this point.",
	"Wrote this on the back of an envelope first.", "The radio kept playing the same song.",
	"Finally found the missing charger in a coat pocket.", "The to-do list grew faster than it shrank.",
	"The washing machine is making that noise again.", "Rain all day, so everything ran late.",
	"Spent the morning on hold to the bank.", "The cat knocked a plant off the windowsill.",
	"Forgot my lunch on the counter again.", "The bins went out a day early by mistake.",
	"The neighbours are having their kitchen done.", "Ran out of milk before breakfast.",
	"The train was cancelled, so I worked from the cafe.", "Another parcel went to the wrong house.",
	"My phone battery died halfway through the day.", "Too many tabs open, as usual.",
	"Someone's car alarm went off at six.", "The printer jammed twice.",
	"The new shoes are already rubbing.", "I finally answered the pile of emails.",
}

var v19BreakerResponses = []string{
	"Noted, I'll keep that with the rest of it.", "Got it. Saved.", "Thanks, I've written that down.",
	"Understood, I'll remember that.", "Saved alongside your earlier notes.", "Okay, that's on file now.",
	"Thanks for the update; I've kept it.", "Logged. I'll hold on to it.", "Right, I've added that.",
	"Saved. Shout if anything changes.", "Got that down.", "Kept, along with the details.",
	"Thanks, that's recorded.", "Noted for later.", "All saved on my side.", "I'll remember that one.",
	"Understood. It's in your notes.", "Filed with the others.", "Got it, thanks for telling me.",
	"Okay, noted.", "Saved that for you.", "That's down now.", "Recorded, with the date.",
	"I've kept a note of it.",
}
