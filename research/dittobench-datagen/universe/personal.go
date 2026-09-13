package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 personal-life event programs.
//
// Nothing in v8–v12 asked about the user's own life outside work: the shared
// world's people, projects, and trips were all business-flavoured, and every
// open program was a ledger. Owner note 4 asks for strong personal AND business
// coverage, so v13 adds a second metamorphic generator over household,
// health, school, family, subscription, travel, and hobby events. Outcomes are
// a person, a date at day granularity, a time of day, a standing status, set
// membership, or a next action — never money.
//
// The structure is the v13 business contract's: four-member metamorphic groups
// (base, renderer invariant, distractor invariant, causal counterfactual), a
// seeded record permutation, grader-only Claims with accept clusters, and
// validator-side provenance. The surface differs: personal records are plain
// household notes rendered as a chat note, a shared-calendar entry, a
// forwarded text message, or a snapshot of a shared list, with no workspace
// schema labels — a household does not have a glossary.
//
// Six groups per full seed draw six of the seven domains by a seeded
// permutation, so every domain appears across seeds while no seed repeats one.

const V13PersonalProvenanceRevision = "dittobench-v13-personal-spec-v1"

// V13PersonalQuestionType is the validator-internal question type shared by
// every v13 personal-program case.
const V13PersonalQuestionType = "v13-personal-program"

// V13PersonalDomain names one personal-life event schema.
type V13PersonalDomain string

const (
	V13PersonalHousehold     V13PersonalDomain = "household-chores"
	V13PersonalAppointments  V13PersonalDomain = "appointments-medication"
	V13PersonalSchool        V13PersonalDomain = "school-logistics"
	V13PersonalMilestones    V13PersonalDomain = "family-milestones"
	V13PersonalSubscriptions V13PersonalDomain = "subscriptions-renewals"
	V13PersonalTravel        V13PersonalDomain = "travel-logistics"
	V13PersonalHobbies       V13PersonalDomain = "hobbies-clubs"
)

// V13PersonalDomains is the schema catalogue.
var V13PersonalDomains = []V13PersonalDomain{
	V13PersonalHousehold, V13PersonalAppointments, V13PersonalSchool, V13PersonalMilestones,
	V13PersonalSubscriptions, V13PersonalTravel, V13PersonalHobbies,
}

// V13PersonalRenderer is a personal-record surface class. Provenance-only.
type V13PersonalRenderer string

const (
	V13PersonalChatRenderer     V13PersonalRenderer = "chat_note"
	V13PersonalCalendarRenderer V13PersonalRenderer = "calendar_entry"
	V13PersonalTextRenderer     V13PersonalRenderer = "forwarded_text"
	V13PersonalListRenderer     V13PersonalRenderer = "shared_list_snapshot"
)

var v13PersonalRenderers = []V13PersonalRenderer{
	V13PersonalChatRenderer, V13PersonalCalendarRenderer, V13PersonalTextRenderer, V13PersonalListRenderer,
}

// ── Vocabulary ───────────────────────────────────────────────────────────────

var v13Relations = []string{"my partner", "my sister", "my brother", "our eldest", "our youngest", "my dad", "my mum", "my brother-in-law", "my flatmate", "my neighbour"}

// v13ChildRelations are the relations school logistics attach to.
var v13ChildRelations = []string{"our eldest", "our youngest", "my niece", "my nephew", "my sister's kid", "the twins"}

var v13Chores = []string{"the recycling run", "school pickup", "the dog walk", "emptying the dishwasher", "putting the bins out", "the laundry", "the weekly meal plan", "mowing the lawn"}

var v13Appointments = []string{"dentist check-up", "physio session", "eye test", "vaccination booster", "orthodontist fitting", "allergy clinic visit", "blood test"}

var v13SchoolEvents = []string{"swim gala", "field trip", "camp week", "sports day", "residential"}

var v13SchoolItems = []string{"swim goggles", "signed permission slip", "packed lunch", "spare socks", "sunscreen", "water bottle", "reading log", "library book", "PE kit", "raincoat", "bus pass", "art smock", "sleeping bag", "torch"}

var v13Milestones = []string{"wedding", "housewarming", "graduation party", "baby shower", "retirement do", "fortieth"}

// V13PersonalStatusClasses are plan statuses for a family event.
var V13PersonalStatusClasses = []V13TermClass{
	{Canonical: "confirmed", Accept: []string{"confirmed", "definitely on", "going ahead", "locked in"}},
	{Canonical: "postponed", Accept: []string{"postponed", "pushed back", "delayed", "moved to a later date"}},
	{Canonical: "cancelled", Accept: []string{"cancelled", "canceled", "called off", "not happening"}},
	{Canonical: "tentative", Accept: []string{"tentative", "pencilled in", "penciled in", "still up in the air", "provisional"}},
}

var v13Services = []string{"the streaming bundle", "the meal-kit box", "the cloud storage plan", "the gym membership", "the language app", "the news subscription", "the audiobook plan"}

// V13PersonalActionClasses are next steps on a subscription.
var V13PersonalActionClasses = []V13TermClass{
	{Canonical: "cancel it", Accept: []string{"cancel it", "cancel", "cancellation", "stop paying for it", "end the subscription"}},
	{Canonical: "downgrade it", Accept: []string{"downgrade it", "downgrade", "drop to the cheaper tier", "move to the basic tier"}},
	{Canonical: "pause it", Accept: []string{"pause it", "pause", "put it on pause", "freeze it for a few months"}},
	{Canonical: "upgrade it", Accept: []string{"upgrade it", "upgrade", "move to the premium tier", "go up a tier"}},
	{Canonical: "switch it to annual billing", Accept: []string{"switch it to annual billing", "annual billing", "go yearly", "pay for the year"}},
}

var v13Trips = []string{"the coast trip", "the Edinburgh weekend", "the visit to Grandma", "the ski week", "the city break", "the wedding trip north"}

var v13Legs = []string{"train", "coach", "flight", "ferry"}

var v13Clubs = []string{"the Tuesday five-a-side", "the book club", "the allotment society", "the choir", "the climbing meet", "the pottery evening", "the running group"}

var v13PersonalQuestionOpeners = []string{
	"Quick one from my own notes.",
	"Checking something I told you earlier.",
	"From my household notes:",
	"Help me keep the family logistics straight.",
	"Before I forget —",
	"Looking back at what I saved:",
}

var v13PersonalAcks = []string{
	"Got it — I'll keep that with your household notes.",
	"Noted; I'll remember the latest version of that.",
	"Saved. If it changes again just tell me.",
	"Filed under family logistics.",
	"Okay, updating what I have.",
	"Thanks — I've got that down.",
}

// ── Latent scenario ──────────────────────────────────────────────────────────

type v13PersonalGroup struct {
	Domain   V13PersonalDomain
	Relation string
	People   [4]string // [0]=first, [1]=current, [2]=counterfactual, [3]=decoy
	Subject  string    // chore / appointment / event / service / trip / club
	Decoy    string    // the decoy subject of the same domain
	Classes  [4]int    // term-class indexes: [0]=first, [1]=current, [2]=counterfactual, [3]=decoy
	Dates    [4]v13Date
	Times    [4]v13Time
	Items    []string // school list items: base set + dropped + added
	DropBase int      // index (within Items[:4]) dropped in the base reading
	DropCF   int      // index dropped in the counterfactual reading
	Added    string
	DecoyItm string
}

// v13Time is a time of day.
type v13Time struct{ Hour, Minute int }

// V13TimeAccept returns every unambiguous rendering of a time of day.
func V13TimeAccept(hour, minute int) []string {
	h12 := hour % 12
	if h12 == 0 {
		h12 = 12
	}
	ampm := "am"
	if hour >= 12 {
		ampm = "pm"
	}
	out := []string{
		fmt.Sprintf("%02d:%02d", hour, minute),
		fmt.Sprintf("%d:%02d %s", h12, minute, ampm),
		fmt.Sprintf("%d:%02d%s", h12, minute, ampm),
		fmt.Sprintf("%d.%02d %s", h12, minute, ampm),
		fmt.Sprintf("%d:%02d %s", h12, minute, strings.ToUpper(ampm)),
		fmt.Sprintf("%d:%02d%s", h12, minute, strings.ToUpper(ampm)),
		fmt.Sprintf("%02d%02d", hour, minute),
	}
	if hour < 10 {
		out = append(out, fmt.Sprintf("%d:%02d", hour, minute))
	}
	return out
}

func v13TimeProse(seed int64, salt string, t v13Time) string {
	forms := V13TimeAccept(t.Hour, t.Minute)
	// Prose uses one of the first four (the 24h form and the three common 12h
	// forms); the remaining accept forms are answer-side only.
	return forms[v13Index(seed, "time-form-"+salt, 4)]
}

func (d *v13Draws) drawPersonalGroup(group int, domain V13PersonalDomain) v13PersonalGroup {
	seed := d.seed
	g := v13PersonalGroup{Domain: domain}
	g.Relation = v13Pick(seed, fmt.Sprintf("p-relation-%d", group), v13Relations)
	if domain == V13PersonalSchool {
		g.Relation = v13Pick(seed, fmt.Sprintf("p-relation-%d", group), v13ChildRelations)
	}
	for i := range g.People {
		g.People[i] = d.name()
	}
	pick2 := func(salt string, bank []string) (string, string) {
		perm := v13Perm(seed, salt, len(bank))
		return bank[perm[0]], bank[perm[1]]
	}
	days := v13Perm(seed, fmt.Sprintf("p-days-%d", group), 28)
	months := v13Perm(seed, fmt.Sprintf("p-months-%d", group), 12)
	for i := range g.Dates {
		g.Dates[i] = v13Date{Month: 1 + months[i], Day: 1 + days[i]}
	}
	hours := v13Perm(seed, fmt.Sprintf("p-hours-%d", group), 15)
	minutes := []int{0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
	minutePerm := v13Perm(seed, fmt.Sprintf("p-minutes-%d", group), len(minutes))
	for i := range g.Times {
		g.Times[i] = v13Time{Hour: 6 + hours[i], Minute: minutes[minutePerm[i]]}
	}
	switch domain {
	case V13PersonalHousehold:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-chores-%d", group), v13Chores)
	case V13PersonalAppointments:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-appts-%d", group), v13Appointments)
	case V13PersonalSchool:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-school-%d", group), v13SchoolEvents)
		perm := v13Perm(seed, fmt.Sprintf("p-items-%d", group), len(v13SchoolItems))
		for i := 0; i < 4; i++ {
			g.Items = append(g.Items, v13SchoolItems[perm[i]])
		}
		g.Added = v13SchoolItems[perm[4]]
		g.DecoyItm = v13SchoolItems[perm[5]]
		g.DropBase = v13Index(seed, fmt.Sprintf("p-drop-%d", group), 4)
		g.DropCF = (g.DropBase + 1 + v13Index(seed, fmt.Sprintf("p-drop-cf-%d", group), 3)) % 4
	case V13PersonalMilestones:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-milestones-%d", group), v13Milestones)
		g.Classes = d.distinctClasses(fmt.Sprintf("p-status-%d", group), len(V13PersonalStatusClasses), 4)
	case V13PersonalSubscriptions:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-services-%d", group), v13Services)
		g.Classes = d.distinctClasses(fmt.Sprintf("p-actions-%d", group), len(V13PersonalActionClasses), 4)
	case V13PersonalTravel:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-trips-%d", group), v13Trips)
	case V13PersonalHobbies:
		g.Subject, g.Decoy = pick2(fmt.Sprintf("p-clubs-%d", group), v13Clubs)
	}
	return g
}

// renderV13PersonalMember materialises one member of a personal group.
func renderV13PersonalMember(d *v13Draws, group, variant int, g v13PersonalGroup, counter bool) v13Member {
	seed := d.seed
	m := v13Member{Kind: protocol.AnswerValue}
	// Every group carries one neutral record so the graded fact never sits
	// alone in the seeded evidence.
	m.Records[2] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-neutral-%d", group), []string{
		"%s asked me to remind everyone about the family calendar on Sunday evenings.",
		"We agreed with %s to keep the shared shopping list on the fridge tablet.",
		"%s wants the car booked for the MOT before the end of the month.",
	}), g.Relation)
	first, current := g.People[0], g.People[1]
	if counter {
		current = g.People[2]
	}
	relation := g.Relation
	switch g.Domain {
	case V13PersonalHousehold:
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-chore-a-%d", group), []string{
			"On the rota, %[2]s is %[1]s's job.",
			"%[1]s has %[2]s on the chores rota.",
			"We put %[1]s down for %[2]s this term.",
		}), first, g.Subject)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-chore-b-%d", group), []string{
			"Change of plan: %[1]s and %[2]s swapped, so %[2]s has %[3]s from now on.",
			"%[2]s took over %[3]s from %[1]s — that swap is permanent.",
			"Rota update: %[3]s moves from %[1]s to %[2]s going forward.",
		}), first, current, g.Subject)
		m.DecoyClause = fmt.Sprintf("Separately, %s is %s's job.", g.Decoy, g.People[3])
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"after the swap, who is on %[1]s now?",
			"whose job is %[1]s these days?",
			"who has %[1]s on the rota as it stands?",
		}, []string{"Just the name.", "Name only, please.", "Give me the person."}, g.Subject)
		m.Expected, m.AcceptAny = current, []string{current}
		m.Distractors = []string{g.People[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, current, []string{current}, 1)}
		m.Program = V10QueryNode{Op: "latest", Field: "chore_owner", Children: []V10QueryNode{{Op: "resolve_entity", Field: "chore"}}}
		m.Protected = []string{first, current, g.People[3], g.Subject, g.Decoy}
		m.Facts = []string{"initial rota assignment", "rota swap", "chore identity"}
		m.Constraints = []string{g.Subject}
		m.Operations = []string{"resolve the chore", "apply the swap", "return the person"}

	case V13PersonalAppointments:
		date := g.Dates[1]
		if counter {
			date = g.Dates[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-appt-a-%d", group), []string{
			"%[1]s's %[2]s was booked for %[3]s.",
			"Booked %[1]s in for the %[2]s on %[3]s.",
			"The %[2]s for %[1]s: %[3]s.",
		}), relation, g.Subject, v13DateProse(seed, fmt.Sprintf("p-appt-a-%d", group), g.Dates[0].Month, g.Dates[0].Day))
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-appt-b-%d", group), []string{
			"The clinic moved %[1]s's %[2]s to %[3]s; the old slot is gone.",
			"Rescheduled: %[1]s's %[2]s is now %[3]s instead.",
			"Update on the %[2]s for %[1]s — new date %[3]s, replacing the earlier booking.",
		}), relation, g.Subject, v13DateProse(seed, fmt.Sprintf("p-appt-b-%d-%v", group, counter), date.Month, date.Day))
		m.DecoyClause = fmt.Sprintf("Unrelated: %s's %s is on %s.", g.People[3], g.Decoy, v13DateProse(seed, fmt.Sprintf("p-appt-decoy-%d", group), g.Dates[3].Month, g.Dates[3].Day))
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"when is %[1]s's %[2]s now?",
			"what date did %[1]s's %[2]s end up on after the change?",
			"which day is %[1]s's %[2]s, as it stands?",
		}, []string{"The day is enough.", "Just the date.", "Any clear date format is fine."}, relation, g.Subject)
		m.Expected = fmt.Sprintf("2026-%02d-%02d", date.Month, date.Day)
		m.AcceptAny = V13DateAccept(2026, date.Month, date.Day)
		m.Distractors = []string{fmt.Sprintf("2026-%02d-%02d", g.Dates[3].Month, g.Dates[3].Day)}
		m.Distractors = append(m.Distractors, V13DateAccept(2026, g.Dates[3].Month, g.Dates[3].Day)[1:]...)
		m.Claims = []protocol.Claim{{Kind: protocol.ClaimKindDate, Expected: m.Expected, Accept: m.AcceptAny, Unit: "day", Critical: true, Weight: 1}}
		m.Program = V10QueryNode{Op: "latest", Field: "appointment_date", Children: []V10QueryNode{{Op: "resolve_entity", Field: "appointment"}}}
		m.Protected = []string{relation, g.Subject, g.Decoy, g.People[3]}
		m.Facts = []string{"original booking", "rescheduled booking", "appointment identity"}
		m.Constraints = []string{relation, g.Subject}
		m.Operations = []string{"resolve the appointment", "apply the reschedule", "return the date at day granularity"}

	case V13PersonalSchool:
		drop := g.DropBase
		if counter {
			drop = g.DropCF
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-school-a-%d", group), []string{
			"%[1]s's %[2]s list from school: %[3]s.",
			"For %[1]s's %[2]s the school wants: %[3]s.",
			"Packing list for %[1]s's %[2]s — %[3]s.",
		}), relation, g.Subject, strings.Join(g.Items[:4], ", "))
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-school-b-%d", group), []string{
			"The school changed the %[1]s list: they dropped the %[2]s and added a %[3]s.",
			"Update for the %[1]s: no %[2]s after all, but they do now want a %[3]s.",
			"Revised %[1]s list — %[2]s is off it, %[3]s is on it.",
		}), g.Subject, g.Items[drop], g.Added)
		m.DecoyClause = fmt.Sprintf("Separately, %s's %s list is just a %s.", g.People[3], g.Decoy, g.DecoyItm)
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"what is on %[1]s's %[2]s list now?",
			"after the school's change, what does %[1]s need for the %[2]s?",
			"list everything currently required for %[1]s's %[2]s.",
		}, []string{"Every item, please.", "Give the full current list.", "All of the items."}, relation, g.Subject)
		m.Kind = protocol.AnswerList
		for i, item := range g.Items[:4] {
			if i != drop {
				m.Items = append(m.Items, item)
			}
		}
		m.Items = append(m.Items, g.Added)
		m.ItemKinds = make([]string, len(m.Items))
		m.Expected = strings.Join(m.Items, "; ")
		// The dropped item is a wrong answer to "what is on the list NOW"; under
		// the v12+ slot-scoped scan it zeroes only when the answer slot lists it.
		m.Distractors = []string{g.Items[drop], g.DecoyItm}
		for _, item := range m.Items {
			m.Claims = append(m.Claims, v13Claim(protocol.ClaimKindSetMember, item, []string{item}, 1/float64(len(m.Items))))
		}
		m.Program = V10QueryNode{Op: "set_after_update", Field: "packing_list", Children: []V10QueryNode{{Op: "resolve_entity", Field: "school_event"}}}
		m.Protected = append([]string{relation, g.Subject, g.Decoy, g.Added, g.DecoyItm, g.People[3]}, g.Items...)
		m.Facts = []string{"original list", "dropped item", "added item"}
		m.Constraints = []string{relation, g.Subject}
		m.Operations = []string{"resolve the event", "apply the drop and the add", "return the current set"}

	case V13PersonalMilestones:
		cls := V13PersonalStatusClasses
		currentClass := g.Classes[1]
		if counter {
			currentClass = g.Classes[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-mile-a-%d", group), []string{
			"%[1]s's %[2]s is %[3]s for %[4]s.",
			"The %[2]s for %[1]s: %[3]s, %[4]s.",
			"%[1]s has the %[2]s down as %[3]s on %[4]s.",
		}), relation, g.Subject, cls[g.Classes[0]].Canonical, v13DateProse(seed, fmt.Sprintf("p-mile-%d", group), g.Dates[0].Month, g.Dates[0].Day))
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-mile-b-%d", group), []string{
			"Heard from %[1]s: the %[2]s is now %[3]s — that replaces what I said before.",
			"Update — %[1]s's %[2]s has gone from what it was to %[3]s.",
			"%[1]s messaged: the %[2]s plan is %[3]s as of today.",
		}), relation, g.Subject, cls[currentClass].Canonical)
		m.DecoyClause = fmt.Sprintf("Separately, %s's %s is %s.", g.People[3], g.Decoy, cls[g.Classes[3]].Canonical)
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"what's the status of %[1]s's %[2]s now?",
			"is %[1]s's %[2]s still on — what did it end up as?",
			"where does %[1]s's %[2]s stand after the latest message?",
		}, []string{"A word or two is fine.", "Just the status.", "Plain English is fine."}, relation, g.Subject)
		m.Expected = cls[currentClass].Canonical
		m.AcceptAny = append([]string(nil), cls[currentClass].Accept...)
		m.Distractors = []string{cls[g.Classes[3]].Canonical}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindStatus, m.Expected, m.AcceptAny, 1)}
		m.Program = V10QueryNode{Op: "latest", Field: "plan_status", Children: []V10QueryNode{{Op: "resolve_entity", Field: "milestone"}}}
		m.Protected = []string{relation, g.Subject, g.Decoy, g.People[3], cls[g.Classes[0]].Canonical, m.Expected, cls[g.Classes[3]].Canonical}
		m.Facts = []string{"initial plan status", "superseding status message", "event identity"}
		m.Constraints = []string{relation, g.Subject}
		m.Operations = []string{"resolve the event", "select the latest status", "return the status"}

	case V13PersonalSubscriptions:
		cls := V13PersonalActionClasses
		currentClass := g.Classes[1]
		if counter {
			currentClass = g.Classes[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-sub-a-%d", group), []string{
			"%[1]s renews on %[2]s; the plan was to %[3]s before then.",
			"Renewal for %[1]s lands %[2]s — I'd decided to %[3]s.",
			"%[1]s: renewal %[2]s, decision so far is to %[3]s.",
		}), g.Subject, v13DateProse(seed, fmt.Sprintf("p-sub-%d", group), g.Dates[0].Month, g.Dates[0].Day), cls[g.Classes[0]].Canonical)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-sub-b-%d", group), []string{
			"Changed my mind on %[1]s: the plan now is to %[2]s instead.",
			"Scrap the earlier plan for %[1]s — we'll %[2]s.",
			"Final call on %[1]s: %[2]s. Ignore what I said before.",
		}), g.Subject, cls[currentClass].Canonical)
		m.DecoyClause = fmt.Sprintf("Separately, for %s the plan is to %s.", g.Decoy, cls[g.Classes[3]].Canonical)
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"what did we settle on doing about %[1]s before it renews?",
			"what's the next action on %[1]s, as it stands?",
			"remind me — what are we doing with %[1]s?",
		}, []string{"Just the action.", "One short phrase.", "The decision, not the history."}, g.Subject)
		m.Expected = cls[currentClass].Canonical
		m.AcceptAny = append([]string(nil), cls[currentClass].Accept...)
		m.Distractors = []string{cls[g.Classes[3]].Canonical}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindAction, m.Expected, m.AcceptAny, 1)}
		m.Program = V10QueryNode{Op: "latest", Field: "next_action", Children: []V10QueryNode{{Op: "resolve_entity", Field: "subscription"}}}
		m.Protected = []string{g.Subject, g.Decoy, cls[g.Classes[0]].Canonical, m.Expected, cls[g.Classes[3]].Canonical}
		m.Facts = []string{"renewal date", "initial decision", "superseding decision"}
		m.Constraints = []string{g.Subject}
		m.Operations = []string{"resolve the subscription", "select the latest decision", "return the action"}

	case V13PersonalTravel:
		leg := v13Pick(seed, fmt.Sprintf("p-leg-%d", group), v13Legs)
		t := g.Times[1]
		if counter {
			t = g.Times[2]
		}
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-trip-a-%d", group), []string{
			"For %[1]s the %[2]s leaves at %[3]s.",
			"%[1]s: %[2]s departure %[3]s.",
			"Our %[2]s for %[1]s is the %[3]s.",
		}), g.Subject, leg, v13TimeProse(seed, fmt.Sprintf("p-trip-a-%d", group), g.Times[0]))
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-trip-b-%d", group), []string{
			"Rebooked the %[2]s for %[1]s onto the %[3]s departure; the earlier one is cancelled on our booking.",
			"%[1]s change: we're now on the %[3]s %[2]s, not the earlier one.",
			"New plan for %[1]s — the %[2]s at %[3]s. Forget the original time.",
		}), g.Subject, leg, v13TimeProse(seed, fmt.Sprintf("p-trip-b-%d-%v", group, counter), t))
		m.DecoyClause = fmt.Sprintf("Separately, %s leaves at %s.", g.Decoy, v13TimeProse(seed, fmt.Sprintf("p-trip-decoy-%d", group), g.Times[3]))
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"what time does our %[2]s for %[1]s leave now?",
			"after the rebooking, when does the %[2]s for %[1]s depart?",
			"which %[2]s time are we on for %[1]s?",
		}, []string{"Just the time.", "The departure time is enough.", "Any clear time format."}, g.Subject, leg)
		m.Expected = fmt.Sprintf("%02d:%02d", t.Hour, t.Minute)
		m.AcceptAny = V13TimeAccept(t.Hour, t.Minute)
		m.Distractors = V13TimeAccept(g.Times[3].Hour, g.Times[3].Minute)
		m.Claims = []protocol.Claim{{Kind: protocol.ClaimKindTime, Expected: m.Expected, Accept: m.AcceptAny, Unit: "minute", Critical: true, Weight: 1}}
		m.Program = V10QueryNode{Op: "latest", Field: "departure_time", Children: []V10QueryNode{{Op: "resolve_entity", Field: "trip"}}}
		m.Protected = []string{g.Subject, g.Decoy, leg}
		m.Facts = []string{"original departure", "rebooked departure", "trip identity"}
		m.Constraints = []string{g.Subject, leg}
		m.Operations = []string{"resolve the trip", "apply the rebooking", "return the time"}

	case V13PersonalHobbies:
		m.Records[0] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-club-a-%d", group), []string{
			"%[1]s is run by %[2]s.",
			"%[2]s organises %[1]s.",
			"The person behind %[1]s is %[2]s.",
		}), g.Subject, first)
		m.Records[1] = fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-club-b-%d", group), []string{
			"%[2]s has taken over organising %[1]s from %[3]s.",
			"%[1]s changed hands: %[2]s runs it now, not %[3]s.",
			"Since last month %[2]s is the organiser of %[1]s; %[3]s stepped back.",
		}), g.Subject, current, first)
		m.DecoyClause = fmt.Sprintf("Separately, %s is run by %s.", g.Decoy, g.People[3])
		m.Question = v13PersonalQuestion(seed, group, variant, []string{
			"who runs %[1]s now?",
			"who is organising %[1]s these days?",
			"after the handover, who's in charge of %[1]s?",
		}, []string{"Just the name.", "Name only.", "Give me the person."}, g.Subject)
		m.Expected, m.AcceptAny = current, []string{current}
		m.Distractors = []string{g.People[3]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, current, []string{current}, 1)}
		m.Program = V10QueryNode{Op: "latest", Field: "organiser", Children: []V10QueryNode{{Op: "resolve_entity", Field: "club"}}}
		m.Protected = []string{first, current, g.People[3], g.Subject, g.Decoy}
		m.Facts = []string{"initial organiser", "handover", "club identity"}
		m.Constraints = []string{g.Subject}
		m.Operations = []string{"resolve the club", "apply the handover", "return the person"}
	default:
		panic("unhandled v13 personal domain")
	}
	return m
}

// v13Capitalize upper-cases the first letter of a household note so a record
// that opens with a relation ("my dad ...") reads as a sentence.
func v13Capitalize(s string) string {
	if s == "" {
		return s
	}
	r := []rune(s)
	return strings.ToUpper(string(r[0])) + string(r[1:])
}

func v13PersonalQuestion(seed int64, group, variant int, forms, closers []string, args ...any) string {
	body := fmt.Sprintf(v13Pick(seed, fmt.Sprintf("p-qform-%d", group), forms), args...)
	return v13Pick(seed, fmt.Sprintf("p-qopen-%d-%d", group, variant), v13PersonalQuestionOpeners) + " " +
		strings.ToUpper(body[:1]) + body[1:] + " " +
		v13Pick(seed, fmt.Sprintf("p-qclose-%d-%d", group, variant), closers)
}

// renderV13PersonalRecord wraps one household note in the renderer's framing.
func renderV13PersonalRecord(seed int64, group int, renderer V13PersonalRenderer, index int, row string) (string, string) {
	ack := v13Pick(seed, fmt.Sprintf("p-ack-%d-%d", group, index), v13PersonalAcks)
	switch renderer {
	case V13PersonalChatRenderer:
		return v13Pick(seed, fmt.Sprintf("p-chatlead-%d-%d", group, index), []string{"", "Note for you: ", "Adding to my notes — ", "Remember this: "}) + row, ack
	case V13PersonalCalendarRenderer:
		return fmt.Sprintf("Shared calendar entry %d\n%s", index+1, row), ack
	case V13PersonalTextRenderer:
		return fmt.Sprintf("Forwarding a text I got: \"%s\"", row), ack
	case V13PersonalListRenderer:
		return fmt.Sprintf("From the fridge list (photo transcription, line %d): %s", index+1, row), ack
	default:
		panic("unhandled v13 personal renderer")
	}
}

// GenerateV13PersonalPrograms returns count scored personal-life cases in
// complete four-member metamorphic groups. Count must be a positive multiple
// of four; 24 covers six of the seven domains (a seeded permutation).
func GenerateV13PersonalPrograms(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v13 personal program count must be a positive multiple of four, got %d", count)
	}
	d := newV13Draws(seed, "personal")
	domainPerm := v13Perm(seed, "personal-domains", len(V13PersonalDomains))
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		domain := V13PersonalDomains[domainPerm[group%len(domainPerm)]]
		g := d.drawPersonalGroup(group, domain)
		base := renderV13PersonalMember(d, group, 0, g, false)
		counter := renderV13PersonalMember(d, group, 3, g, true)
		if base.Expected == counter.Expected {
			return nil, fmt.Errorf("v13 personal causal mutation did not change group %d (%s) answer", group, domain)
		}
		groupID := protocol.OpaqueCaseID(seed, "v13-personal-group", group)
		variants := []struct {
			relation string
			answer   string
			counter  bool
			distract bool
		}{
			{relation: protocol.RelationBase, answer: "base"},
			{relation: protocol.RelationRendererInvariant, answer: "same"},
			{relation: protocol.RelationDistractorInvariant, answer: "same", distract: true},
			{relation: protocol.RelationCausalCounterfactual, answer: "changed", counter: true},
		}
		for variant, spec := range variants {
			member := renderV13PersonalMember(d, group, variant, g, spec.counter)
			renderer := v13PersonalRenderers[(group+variant)%len(v13PersonalRenderers)]
			out = append(out, materializeV13PersonalCase(seed, group, variant, groupID, renderer, g, member, spec.relation, spec.answer, spec.distract))
		}
	}
	return out, nil
}

func materializeV13PersonalCase(seed int64, group, variant int, groupID string, renderer V13PersonalRenderer, g v13PersonalGroup, m v13Member, relation, answerRelation string, includeDistractor bool) V10GeneratedCase {
	prefix := fmt.Sprintf("v13p-%d-%d", group, variant)
	rows := []string{m.Records[0], m.Records[1], m.Records[2]}
	if includeDistractor {
		rows = append(rows, m.DecoyClause)
	}
	perm := v13Perm(seed, fmt.Sprintf("p-rows-%d-%d", group, len(rows)), len(rows))
	ordered := make([]string, 0, len(rows))
	for _, p := range perm {
		ordered = append(ordered, rows[p])
	}
	pairIDs := make([]string, 0, len(ordered))
	pairs := make([]protocol.MemoryPair, 0, len(ordered))
	for i, row := range ordered {
		row = v13Capitalize(row)
		id := protocol.OpaqueCaseID(seed, fmt.Sprintf("%s-record", prefix), i)
		pairIDs = append(pairIDs, id)
		prompt, response := renderV13PersonalRecord(seed, group, renderer, i, row)
		pairs = append(pairs, protocol.MemoryPair{
			PairID:    id,
			SessionID: protocol.OpaqueCaseID(seed, fmt.Sprintf("v13p-session-%d", group), i),
			Timestamp: V13Timestamp(seed, prefix, i),
			Prompt:    prompt, Response: response,
		})
	}
	caseID := protocol.OpaqueCaseID(seed, "v13-personal-case", group*4+variant)
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      V13PersonalQuestionType,
		Question:          m.Question,
		ExpectedAnswer:    m.Expected,
		AnswerKind:        m.Kind,
		AcceptAny:         append([]string(nil), m.AcceptAny...),
		AnswerItems:       append([]string(nil), m.Items...),
		AnswerItemKinds:   append([]string(nil), m.ItemKinds...),
		DistractorAnswers: append([]string(nil), m.Distractors...),
		WritingProtected:  append([]string(nil), m.Protected...),
		Claims:            append([]protocol.Claim(nil), m.Claims...),
	}
	if relation != protocol.RelationCausalCounterfactual {
		caseValue.TwinGroup = groupID
	}
	plan := QuestionPlan{
		Case:            caseValue,
		RequiredPairIDs: append([]string(nil), pairIDs...),
		Facts:           append([]string(nil), m.Facts...),
		Constraints:     append([]string(nil), m.Constraints...),
		Operations:      append([]string(nil), m.Operations...),
	}
	provenance := V10CaseProvenance{
		Revision:         V13PersonalProvenanceRevision,
		SchemaSHA256:     "",
		Ontology:         []V10OntologyTerm{{Semantic: "personal-life domain", Wire: string(g.Domain)}},
		Program:          m.Program,
		Renderer:         V10Renderer(renderer),
		MetamorphicGroup: groupID,
		Relation:         relation,
		AnswerRelation:   answerRelation,
		EvidencePairIDs:  append([]string(nil), pairIDs...),
	}
	return V10GeneratedCase{Plan: plan, Pairs: pairs, Provenance: provenance}
}
