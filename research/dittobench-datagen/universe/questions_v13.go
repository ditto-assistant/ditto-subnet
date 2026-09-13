package universe

import (
	"fmt"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Story v2 oracles (bench_version >= 13, issue #1841). Six per arc:
//
//	owner-current, status-current (or the "records disagree" claim set),
//	latest-event / ordering, next-action {who, what, channel}, one rotating
//	slot (a record-stated quantity, or the lesson key-concept claim set on
//	<= 1 arc-slot per arc), and one cross-record inference oracle that joins
//	the arc to the ordinary world through the new resolveWithEvidence ops
//	latest-by-time -> owner-of -> the owner's current work address.
//
// The four v8 money oracles and the cents-baking summary are gone; money
// survives only as the quantity slot on <= storyV2MoneyArcCap business arcs.
// Every plan also carries typed Claims (QuestionPlan.Claims) for the v13
// typed-claim grader; the wire MemoryCase is populated so today's grader grades
// the same semantics (AnswerValue+AcceptAny, AnswerList+AnswerItemAcceptAny,
// AnswerOrderedList, AnswerMoney, AnswerNumber).
const (
	oracleStoryOwnerCurrent   = "story-owner-current"
	oracleStoryStatusCurrent  = "story-status-current"
	oracleStoryStatusDisagree = "story-status-disagree"
	oracleStoryOrder          = "story-order"
	oracleStoryNextAction     = "story-next-action"
	oracleStoryQuantity       = "story-quantity"
	oracleStoryLessonClaims   = "story-lesson-claims"
	oracleStoryOwnerEmail     = "story-x-owner-email"
)

// Claim is one typed graded assertion of a story v2 plan. It mirrors the
// grader-only protocol.Claim shape planned by the v13 plumbing (#1824:
// {Kind, Expected, Accept, Unit, Critical, Weight}); the adapter onto
// MemoryCase.Claims is a one-line follow-up once that type lands, so nothing
// here crosses the harness wire today.
type Claim struct {
	Kind     string   `json:"kind"`
	Expected string   `json:"expected"`
	Accept   []string `json:"accept,omitempty"`
	Unit     string   `json:"unit,omitempty"`
	Critical bool     `json:"critical"`
	Weight   float64  `json:"weight"`
}

// storyDisagreeMarkers is the reviewed accept set of the conflict-marker claim
// on a records-disagree answer. It is exempt from distractor scanning by
// construction: no story v2 distractor is drawn from it.
var storyDisagreeMarkers = []string{"disagree", "conflict", "conflicting", "contradict", "don't agree", "do not agree", "not the same", "two different", "inconsistent", "mismatch", "differ"}

func isStoryV2Oracle(kind string) bool {
	switch kind {
	case oracleStoryOwnerCurrent, oracleStoryStatusCurrent, oracleStoryStatusDisagree, oracleStoryOrder,
		oracleStoryNextAction, oracleStoryQuantity, oracleStoryLessonClaims, oracleStoryOwnerEmail:
		return true
	}
	return false
}

// storyV13AnchorGrammar: two anchor types (person-first, subject-first), each
// with >= 12 frames. {nick} and {alias} are the two rendered constraints.
var storyV13AnchorGrammar = persona.Grammar{
	"anchor": {"#person#", "#subject#"},
	"person": {
		"Think back to what {nick} and I had going with {alias}.", "Remember {nick}'s thing, {alias}?", "This is about {nick} and {alias}.",
		"Go back to the thread {nick} started, {alias}.", "You know the {alias} saga with {nick}?", "About {alias} — the one from {nick}.",
		"On {nick}'s {alias} thread:", "I mean the {alias} business I got into with {nick}.", "Regarding {alias}, the thing {nick} roped me into —",
		"So, {nick} and {alias}.", "Picking up the {alias} thread with {nick}.", "Quick one on {alias}, {nick}'s project.",
		"{nick} asked about {alias} again.", "Back to {alias} with {nick} for a second.",
	},
	"subject": {
		"On {alias} (the {nick} thread):", "About {alias}, which started with {nick} —", "{alias}, the one I planned with {nick}:",
		"For {alias} — {nick}'s one —", "Regarding {alias} that {nick} and I kicked off:", "The {alias} thread, from {nick}:",
		"{alias}. The thing with {nick}.", "Re {alias}, the {nick} situation:", "Looking at {alias}, the one {nick} brought me:",
		"{alias} — you remember, with {nick} —", "I need something on {alias}, the {nick} thread.", "Where are we with {alias}? The {nick} one.",
		"Help me with {alias}, the thing from {nick}.", "{alias} again, the one {nick} keeps asking about.",
	},
}

// storyV13TaskGrammars: >= 10 task frames per oracle. None names a status,
// entity, number, or channel word that could satisfy the graded value.
var storyV13TaskGrammars = map[string]persona.Grammar{
	oracleStoryOwnerCurrent: {"task": {
		"Who is actually running it now?", "Who owns it at this point?", "Whose is it these days?", "Who should I go to as the owner right now?",
		"Who ended up holding it?", "Who is accountable for it as things stand?", "Who has it now, after all the passing around?", "Who is the current owner?",
		"Who do I chase about it now?", "Who is on point for it these days?", "Who has the thread now?", "Who is responsible for it at the moment?",
	}},
	oracleStoryStatusCurrent: {"task": {
		"Where does it stand right now?", "What state is it in as of the latest note?", "What is the current position on it?", "How would you describe where it landed?",
		"What is its status now?", "Where did it end up?", "As of today, what is the state of it?", "What is the latest on where it is?",
		"Give me the current standing of it.", "What is the state of play?", "Where are we with it?", "What does the most recent record say the state is?",
	}},
	oracleStoryStatusDisagree: {"task": {
		"Where does it stand — and be straight with me if my notes don't line up.", "What state is it in? Tell me if the records clash.", "What is the current position, and do my notes agree on it?",
		"Where did it land, as far as the records go?", "What is its status, and is that consistent across what I wrote down?", "As of the latest notes, what is the state of it, and do they agree?",
		"Give me the current standing, flagging anything the records disagree on.", "What do my notes say the state is — all of them?", "Where are we with it, according to each record?",
		"What state is it in? If two notes say different things, say so.", "Tell me its status and whether the sources match.", "Where does it stand, and is there any disagreement in the record?",
	}},
	oracleStoryOrder: {"task": {
		"Which {noun} came first and which one replaced it?", "Put the two {noun}s in order for me.", "Who did we start with as the {noun}, and who took over?",
		"List the {noun}s in the order they came in.", "What was the sequence of {noun}s?", "First {noun}, then second {noun} — which was which?",
		"Give me the {noun} history in order.", "Which {noun} was original and which is the replacement?", "In what order did the {noun}s change hands?",
		"Walk me through the {noun} changes, earliest first.", "Name the {noun}s, oldest to newest.", "Original {noun} then current {noun}, please.",
	}},
	oracleStoryNextAction: {"task": {
		"What is the next step — who does what, and how are they getting in touch?", "Who owes the next move, what is it, and over which channel?", "What is the open action, who has it, and how will they do it?",
		"Tell me the next action: person, task, and channel.", "Who is doing what next, and by what means?", "What is waiting to happen, who is on it, and how?",
		"Spell out the next move for me — who, what, and the way they will reach out.", "What is the pending action and who carries it, and through what?", "Who has the ball, what do they need to do, and how?",
		"What comes next: the person, the task, and how they will handle it?", "Who is meant to act next, on what, and via which channel?", "Give me the next step with the owner and the channel.",
	}},
	oracleStoryQuantity: {"task": {
		"Working from the records, what does the {unit} figure come to now?", "What is the current {unit} number once you apply what the records say?", "After the change they described, how many {unit} is it?",
		"What do the {unit} work out to, following the operation in the notes?", "Apply the stated change — what is the {unit} total now?", "Where does the {unit} figure land after the adjustment they spelled out?",
		"Using the records as written, what is the resulting {unit} amount?", "What is the {unit} figure after the change described in the thread?", "Follow the notes: what are the {unit} now?",
		"What number of {unit} does the thread arrive at?", "Take the stated change into account — how many {unit}?", "Per the records, what is the final {unit} figure?",
	}},
	oracleStoryLessonClaims: {"task": {
		"What did I take away from the whole thing?", "What was the lesson I wrote down at the end?", "What did I say I would remember next time?",
		"What was the takeaway?", "What rule did I set myself afterwards?", "What did the mess teach me?",
		"What lesson did I note when it wrapped?", "What did I resolve to do differently?", "What was the advice I kept from it?",
		"What did I learn, in my own words?", "What was the moral I recorded?", "What stuck with me at the end of it?",
	}},
	oracleStoryOwnerEmail: {"task": {
		"Whoever owns it now — what is their current work email?", "Give me the present owner's up-to-date work address.", "I need to write to the current owner; which work email is live for them?",
		"Find who holds it now and their current work email.", "What work email reaches the person running it these days?", "Current owner, current work address — what is it?",
		"Which work email should I use for whoever has it now?", "Resolve the present owner and give me their current work email.", "Who owns it now, and what is the right work email for them today?",
		"What is the live work address of the person accountable for it now?", "Track down the current owner's current work email.", "For the person holding it now, which work email is current?",
	}},
}

func (w World) storyQuestionCandidatesV13() []QuestionPlan {
	out := make([]QuestionPlan, 0, len(w.StoryArcs)*6)
	for i, arc := range w.StoryArcs {
		v2 := arc.V2
		if v2 == nil {
			continue
		}
		person := w.People[arc.PersonIndex]
		r := rand.New(rand.NewSource(storyQuestionSeed(w.Seed, arc.ID)))
		anchorSlots := map[string]string{"nick": person.Nickname, "alias": v2.SubjectAlias, "noun": v2.SequenceNoun}
		if v2.Quantity != nil {
			anchorSlots["unit"] = v2.Quantity.Kind
			if v2.Quantity.Kind == "money" {
				anchorSlots["unit"] = "money"
			}
		}
		constraints := []string{person.Nickname, v2.SubjectAlias}
		question := func(kind string) string {
			anchor := storySentence(storyFill(persona.Expand(r, storyV13AnchorGrammar, "anchor"), anchorSlots))
			task := storyFill(persona.Expand(r, storyV13TaskGrammars[kind], "task"), anchorSlots)
			return anchor + " " + task
		}
		plans := make([]QuestionPlan, 0, 6)

		owner := w.People[v2.Owner]
		ownerPlan := w.storyPlanV13(oracleStoryOwnerCurrent, i, question(oracleStoryOwnerCurrent), constraints, owner.Name, protocol.AnswerValue, w.storyV13OwnerDistractors(i), []string{owner.Nickname})
		ownerPlan.Claims = []Claim{{Kind: "entity", Expected: owner.Name, Accept: []string{owner.Nickname}, Critical: true, Weight: 1}}
		plans = append(plans, ownerPlan)

		if v2.Disagree {
			items := []string{v2.Status, v2.StatusAlt, "disagree"}
			plan := w.storyPlanV13(oracleStoryStatusDisagree, i, question(oracleStoryStatusDisagree), constraints, strings.Join(items, "; "), protocol.AnswerList, w.storyV13StatusDistractors(i, v2.Status, v2.StatusAlt), nil)
			plan.Case.AnswerItems = items
			plan.Case.AnswerItemKinds = []string{protocol.AnswerValue, protocol.AnswerValue, protocol.AnswerValue}
			plan.Case.AnswerItemAcceptAny = [][]string{storyStatusAccept(v2.Status, v2.StatusSurface), storyStatusAccept(v2.StatusAlt, v2.StatusAltSurface), append([]string(nil), storyDisagreeMarkers...)}
			plan.Claims = []Claim{
				{Kind: "status", Expected: v2.Status, Accept: plan.Case.AnswerItemAcceptAny[0], Critical: true, Weight: 1},
				{Kind: "status", Expected: v2.StatusAlt, Accept: plan.Case.AnswerItemAcceptAny[1], Critical: true, Weight: 1},
				{Kind: "conflict", Expected: "disagree", Accept: plan.Case.AnswerItemAcceptAny[2], Critical: true, Weight: 1},
			}
			plans = append(plans, plan)
		} else {
			accept := storyStatusAccept(v2.Status, v2.StatusSurface)
			plan := w.storyPlanV13(oracleStoryStatusCurrent, i, question(oracleStoryStatusCurrent), constraints, v2.Status, protocol.AnswerValue, w.storyV13StatusDistractors(i, v2.Status), accept)
			plan.Claims = []Claim{{Kind: "status", Expected: v2.Status, Accept: accept, Critical: true, Weight: 1}}
			plans = append(plans, plan)
		}

		orderPlan := w.storyPlanV13(oracleStoryOrder, i, question(oracleStoryOrder), constraints, strings.Join(v2.Sequence[:], "; "), protocol.AnswerOrderedList, w.storyV13SequenceDistractors(i), nil)
		orderPlan.Case.AnswerItems = append([]string(nil), v2.Sequence[:]...)
		orderPlan.Claims = []Claim{
			{Kind: "entity", Expected: v2.Sequence[0], Critical: true, Weight: 1},
			{Kind: "entity", Expected: v2.Sequence[1], Critical: true, Weight: 1},
			{Kind: "order", Expected: strings.Join(v2.Sequence[:], " -> "), Critical: true, Weight: 1},
		}
		plans = append(plans, orderPlan)

		nextWho := w.People[v2.Next.Who]
		nextItems := []string{nextWho.Name, v2.Next.What, v2.Next.Channel}
		nextPlan := w.storyPlanV13(oracleStoryNextAction, i, question(oracleStoryNextAction), constraints, strings.Join(nextItems, "; "), protocol.AnswerList, w.storyV13NextDistractors(i), nil)
		nextPlan.Case.AnswerItems = nextItems
		nextPlan.Case.AnswerItemKinds = []string{protocol.AnswerValue, protocol.AnswerValue, protocol.AnswerValue}
		nextPlan.Case.AnswerItemAcceptAny = [][]string{{nextWho.Nickname}, append([]string(nil), v2.Next.WhatAccept...), storyChannelAccept(v2.Next.Channel)}
		nextPlan.Claims = []Claim{
			{Kind: "entity", Expected: nextWho.Name, Accept: []string{nextWho.Nickname}, Critical: true, Weight: 1},
			{Kind: "action", Expected: v2.Next.What, Accept: nextPlan.Case.AnswerItemAcceptAny[1], Critical: true, Weight: 1},
			{Kind: "channel", Expected: v2.Next.Channel, Accept: nextPlan.Case.AnswerItemAcceptAny[2], Critical: false, Weight: 1},
		}
		plans = append(plans, nextPlan)

		if v2.Lesson != nil {
			items := make([]string, 0, len(v2.Lesson.concepts))
			accepts := make([][]string, 0, len(v2.Lesson.concepts))
			claims := make([]Claim, 0, len(v2.Lesson.concepts))
			for _, concept := range v2.Lesson.concepts {
				items = append(items, concept.term)
				accepts = append(accepts, append([]string(nil), concept.accept...))
				claims = append(claims, Claim{Kind: "concept", Expected: concept.term, Accept: concept.accept, Critical: false, Weight: 1})
			}
			plan := w.storyPlanV13(oracleStoryLessonClaims, i, question(oracleStoryLessonClaims), constraints, strings.Join(items, "; "), protocol.AnswerList, w.storyV13LessonDistractors(i), nil)
			plan.Case.AnswerItems = items
			plan.Case.AnswerItemKinds = make([]string, len(items))
			for j := range plan.Case.AnswerItemKinds {
				plan.Case.AnswerItemKinds[j] = protocol.AnswerValue
			}
			plan.Case.AnswerItemAcceptAny = accepts
			plan.Claims = claims
			plans = append(plans, plan)
		} else if v2.Quantity != nil {
			q := v2.Quantity
			answerKind := protocol.AnswerNumber
			if q.Kind == "money" {
				answerKind = protocol.AnswerMoney
			}
			plan := w.storyPlanV13(oracleStoryQuantity, i, question(oracleStoryQuantity), constraints, fmt.Sprintf("%d", q.Value), answerKind, w.storyV13QuantityDistractors(i), nil)
			plan.Claims = []Claim{{Kind: "quantity", Expected: fmt.Sprintf("%d", q.Value), Unit: q.Kind, Critical: true, Weight: 1}}
			plans = append(plans, plan)
		}

		emailPlan := w.storyPlanV13(oracleStoryOwnerEmail, i, question(oracleStoryOwnerEmail), constraints, owner.Email, protocol.AnswerValue, w.storyV13OwnerEmailDistractors(i), nil)
		emailPlan.Claims = []Claim{{Kind: "entity", Expected: owner.Email, Critical: true, Weight: 1}}
		plans = append(plans, emailPlan)

		order := r.Perm(len(plans))
		for _, j := range order {
			out = append(out, plans[j])
		}
	}
	return out
}

func storyStatusAccept(status, surface string) []string {
	out := []string{}
	for _, term := range storyStatusVocabulary[status] {
		if !contains(out, term) {
			out = append(out, term)
		}
	}
	if surface != "" && !contains(out, surface) {
		out = append(out, surface)
	}
	return out
}

func storyChannelAccept(channel string) []string {
	for _, c := range storyChannels {
		if c.channel == channel {
			return append([]string(nil), c.accept...)
		}
	}
	return []string{channel}
}

func (w World) storyPlanV13(kind string, index int, question string, constraints []string, answer, answerKind string, distractors, acceptAny []string) QuestionPlan {
	caseValue := memoryCase(w.Seed, kind, index, question, answer, answerKind, distractors)
	caseValue.AcceptAny = append([]string(nil), acceptAny...)
	return QuestionPlan{
		Case: caseValue, RequiredPairIDs: w.storyV13Evidence(kind, index),
		Facts:       storyV13Facts(kind),
		Constraints: append([]string(nil), constraints...),
		Operations:  storyV13Operations(kind), oracleKind: kind, oracleIndex: index,
	}
}

func storyV13Facts(kind string) []string {
	base := []string{"personal anchor and informal subject", "first hidden reference", "second hidden reference"}
	switch kind {
	case oracleStoryOwnerCurrent:
		return append(base, "initial owner", "latest handoff")
	case oracleStoryStatusCurrent:
		return append(base, "status vocabulary", "latest outcome record")
	case oracleStoryStatusDisagree:
		return append(base, "status vocabulary", "latest outcome record", "disagreeing outcome record")
	case oracleStoryOrder:
		return append(base, "first provider", "replacement provider")
	case oracleStoryNextAction:
		return append(base, "latest follow-up owner", "latest follow-up task", "latest follow-up channel")
	case oracleStoryQuantity:
		return append(base, "stated operands", "record-stated operation")
	case oracleStoryLessonClaims:
		return append(base, "outcome record", "explicit lesson")
	case oracleStoryOwnerEmail:
		return append(base, "latest handoff", "owner identity", "owner employer", "owner address correction")
	}
	return base
}

func storyV13Operations(kind string) []string {
	base := []string{"resolve the person-and-subject anchor", "follow the first reference into the thread", "follow the second reference across sessions"}
	switch kind {
	case oracleStoryOwnerCurrent:
		return append(base, "latest-by-time over handoff records", "owner-of")
	case oracleStoryStatusCurrent:
		return append(base, "latest-by-time over outcome records", "status-of")
	case oracleStoryStatusDisagree:
		return append(base, "status-of for each source", "report both statuses with the conflict")
	case oracleStoryOrder:
		return append(base, "order the replacement records by time")
	case oracleStoryNextAction:
		return append(base, "latest-by-time over follow-up records", "next-action")
	case oracleStoryQuantity:
		return append(base, "read the stated operands", "apply the record-stated operation")
	case oracleStoryLessonClaims:
		return append(base, "select the outcome lesson", "state its key concepts")
	case oracleStoryOwnerEmail:
		return append(base, "latest-by-time over handoff records", "owner-of", "join the owner to their current employer", "follow the address correction to the current address")
	}
	return base
}

// storyV13Evidence returns the causal evidence set for one oracle: the two
// join memories plus every memory carrying a fact the resolution needs.
func (w World) storyV13Evidence(kind string, index int) []string {
	v2 := w.StoryArcs[index].V2
	pair := func(memory int) string { return v2.PairIDs[memory] }
	out := []string{pair(0), pair(1)}
	add := func(ids ...string) {
		for _, id := range ids {
			if !contains(out, id) {
				out = append(out, id)
			}
		}
	}
	switch kind {
	case oracleStoryOwnerCurrent:
		add(pair(v2.OwnerMemory))
	case oracleStoryStatusCurrent:
		add(pair(v2.StatusMemory))
	case oracleStoryStatusDisagree:
		add(pair(v2.StatusMemory), pair(v2.StatusAltMemory))
	case oracleStoryOrder:
		add(pair(v2.SequenceMemories[0]), pair(v2.SequenceMemories[1]))
	case oracleStoryNextAction:
		add(pair(v2.NextMemory))
	case oracleStoryQuantity:
		if v2.Quantity != nil {
			add(pair(v2.Quantity.Memory))
		}
	case oracleStoryLessonClaims:
		add(pair(v2.LessonMemory))
	case oracleStoryOwnerEmail:
		owner := w.People[v2.Owner]
		add(pair(v2.OwnerMemory), owner.IdentityPairID, owner.WorkPairID, owner.CorrectionPairID)
	}
	return out
}

// resolveStoryV13 is the counterfactual oracle for story v2 kinds: the answer
// is derivable only when every causal record is available.
func (w World) resolveStoryV13(plan QuestionPlan, available map[string]bool) (string, bool) {
	for _, id := range w.storyV13Evidence(plan.oracleKind, plan.oracleIndex) {
		if !available[id] {
			return "", false
		}
	}
	v2 := w.StoryArcs[plan.oracleIndex].V2
	switch plan.oracleKind {
	case oracleStoryOwnerCurrent:
		// latest-by-time over the handoff records -> owner-of.
		return w.People[v2.Owner].Name, true
	case oracleStoryStatusCurrent:
		return v2.Status, true
	case oracleStoryStatusDisagree:
		return strings.Join([]string{v2.Status, v2.StatusAlt, "disagree"}, "; "), true
	case oracleStoryOrder:
		return strings.Join(v2.Sequence[:], "; "), true
	case oracleStoryNextAction:
		return strings.Join([]string{w.People[v2.Next.Who].Name, v2.Next.What, v2.Next.Channel}, "; "), true
	case oracleStoryQuantity:
		if v2.Quantity == nil {
			return "", false
		}
		return fmt.Sprintf("%d", storyQuantityValue(*v2.Quantity)), true
	case oracleStoryLessonClaims:
		if v2.Lesson == nil {
			return "", false
		}
		items := make([]string, 0, len(v2.Lesson.concepts))
		for _, concept := range v2.Lesson.concepts {
			items = append(items, concept.term)
		}
		return strings.Join(items, "; "), true
	case oracleStoryOwnerEmail:
		return w.People[v2.Owner].Email, true
	}
	return "", false
}

// storyQuantityValue recomputes the quantity from its stated operands and
// operation, so the oracle never trusts a baked total.
func storyQuantityValue(q StoryQuantity) int {
	switch q.Op {
	case "add":
		return q.Base + q.Delta
	case "subtract":
		return q.Base - q.Delta
	default: // replace
		return q.Delta
	}
}

func (w World) storyV13SubjectMatches(plan QuestionPlan) int {
	matches := 0
	for _, arc := range w.StoryArcs {
		if arc.V2 == nil {
			continue
		}
		person := w.People[arc.PersonIndex]
		values := []string{person.Nickname, person.Name, person.Relation, arc.V2.SubjectAlias}
		matched := true
		for _, constraint := range plan.Constraints {
			if !contains(values, constraint) {
				matched = false
				break
			}
		}
		if matched {
			matches++
		}
	}
	return matches
}

// validateStoryPlanV13 is the story v2 evidence proof: every required story
// record is a real arc memory of at least 1.8KB spread over at least two
// sessions, every planted fact sits in the interior of its prompt and never in
// the assistant reply or any short memory, and the question never names a
// hidden reference or the decoy alias.
func (w World) validateStoryPlanV13(plan QuestionPlan) error {
	arc := w.StoryArcs[plan.oracleIndex]
	v2 := arc.V2
	question := strings.ToLower(plan.Case.Question)
	for _, hidden := range []string{v2.JoinKey1, v2.JoinKey2, v2.DecoyAlias, v2.Sequence[0], v2.Sequence[1]} {
		if hidden != "" && strings.Contains(question, strings.ToLower(hidden)) {
			return fmt.Errorf("story question leaks hidden join/state value %q", hidden)
		}
	}
	pairs := make(map[string]protocol.MemoryPair, len(w.Pairs))
	for _, pair := range w.Pairs {
		pairs[pair.PairID] = pair
	}
	stories := make(map[string]Story, len(w.Stories))
	storyPair := make(map[string]bool, len(w.Stories))
	for _, story := range w.Stories {
		stories[story.PairID] = story
		storyPair[story.PairID] = true
	}
	sessions := map[string]bool{}
	storyEvidence := 0
	for _, pairID := range plan.RequiredPairIDs {
		story, ok := stories[pairID]
		if !ok {
			// Cross-record joins may legitimately require ordinary world records.
			if plan.oracleKind == oracleStoryOwnerEmail {
				continue
			}
			return fmt.Errorf("story evidence %s is not a story memory", pairID)
		}
		if story.ArcIndex != plan.oracleIndex || story.PairID == v2.DecoyPairID {
			return fmt.Errorf("story evidence %s belongs to another arc or the decoy", pairID)
		}
		storyEvidence++
		pair := pairs[pairID]
		sessions[pair.SessionID] = true
		if len(pair.Prompt)+len(pair.Response) < 1_800 {
			return fmt.Errorf("story evidence %s is only %d bytes", pairID, len(pair.Prompt)+len(pair.Response))
		}
		if len(story.Facts) < 1 {
			return fmt.Errorf("story evidence %s has no planted facts", pairID)
		}
		if err := validateStoryStructure(story); err != nil {
			return fmt.Errorf("story evidence %s: %w", pairID, err)
		}
		for _, fact := range story.Facts {
			pos := strings.Index(pair.Prompt, fact.Value)
			if pos < 0 {
				return fmt.Errorf("story evidence %s omits fact %s=%q", pairID, fact.Key, fact.Value)
			}
			ratio := float64(pos) / float64(len(pair.Prompt))
			if ratio < 0.15 || ratio > 0.85 {
				return fmt.Errorf("story evidence %s places fact %s at %.2f, outside interior", pairID, fact.Key, ratio)
			}
			if strings.Contains(pair.Response, fact.Value) {
				return fmt.Errorf("agent response duplicates story fact %s=%q", fact.Key, fact.Value)
			}
			for _, other := range w.Pairs {
				if storyPair[other.PairID] {
					continue
				}
				if strings.Contains(other.Prompt+" "+other.Response, fact.Value) {
					return fmt.Errorf("story-only fact %s=%q leaks into short memory %s", fact.Key, fact.Value, other.PairID)
				}
			}
		}
	}
	if storyEvidence < 3 {
		return fmt.Errorf("story plan requires %d story memories, want at least 3", storyEvidence)
	}
	if len(sessions) < 2 {
		return fmt.Errorf("story evidence spans %d session, want at least 2", len(sessions))
	}
	return nil
}

// Distractors. Every set has exactly three distinct values, none equal to the
// expected answer and none contained in an accepted surface form (the grader
// skips such a distractor; the generator simply never emits one).

func (w World) storyV13OtherPeople(index int, excluded map[int]bool, n int) []int {
	out := make([]int, 0, n)
	for step := 1; len(out) < n && step <= len(w.People); step++ {
		candidate := (index*7 + step*3) % len(w.People)
		if excluded[candidate] {
			continue
		}
		excluded[candidate] = true
		out = append(out, candidate)
	}
	return out
}

func (w World) storyV13OwnerDistractors(index int) []string {
	arc := w.StoryArcs[index]
	excluded := map[int]bool{arc.PersonIndex: true}
	for _, owner := range arc.V2.OwnerHistory {
		excluded[owner] = true
	}
	out := make([]string, 0, 3)
	for _, p := range w.storyV13OtherPeople(index, excluded, 3) {
		out = append(out, w.People[p].Name)
	}
	return out
}

func (w World) storyV13OwnerEmailDistractors(index int) []string {
	arc := w.StoryArcs[index]
	owner := w.People[arc.V2.Owner]
	out := []string{}
	if arc.V2.OwnerInitial != arc.V2.Owner {
		out = append(out, w.People[arc.V2.OwnerInitial].Email)
	}
	excluded := map[int]bool{arc.PersonIndex: true}
	for _, o := range arc.V2.OwnerHistory {
		excluded[o] = true
	}
	for _, p := range w.storyV13OtherPeople(index, excluded, 3) {
		if len(out) == 3 {
			break
		}
		if candidate := w.People[p].Email; candidate != owner.Email && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

// storyV13StatusDistractors never names a status the arc actually held:
// superseded values of the user's own update chain are not distractors, and no
// candidate may share a surface term with an accepted status or a conflict
// marker.
func (w World) storyV13StatusDistractors(index int, held ...string) []string {
	arc := w.StoryArcs[index]
	held = append(append([]string(nil), held...), arc.V2.StatusHistory...)
	if arc.V2.StatusAlt != "" {
		held = append(held, arc.V2.StatusAlt)
	}
	out := make([]string, 0, 3)
	for step := 0; len(out) < 3 && step < len(storyStatusOrder); step++ {
		candidate := storyStatusOrder[(index*5+step)%len(storyStatusOrder)]
		ok := !contains(out, candidate)
		for _, h := range held {
			if candidate == h || statusVocabularyOverlaps(candidate, h) {
				ok = false
			}
		}
		for _, marker := range storyDisagreeMarkers {
			if grade.Hit(candidate, marker) || grade.Hit(marker, candidate) {
				ok = false
			}
		}
		if ok {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyV13SequenceDistractors(index int) []string {
	arc := w.StoryArcs[index]
	out := make([]string, 0, 3)
	for step := 1; len(out) < 3 && step <= len(w.StoryArcs); step++ {
		other := w.StoryArcs[(index+step)%len(w.StoryArcs)].V2
		if other == nil {
			continue
		}
		for _, candidate := range other.Sequence {
			if len(out) < 3 && candidate != arc.V2.Sequence[0] && candidate != arc.V2.Sequence[1] && !contains(out, candidate) {
				out = append(out, candidate)
			}
		}
	}
	for j := 1; len(out) < 3; j++ {
		out = append(out, fmt.Sprintf("%s %s %d", strings.Title(arc.V2.SequenceNoun), "Alternative", j))
	}
	return out
}

func (w World) storyV13NextDistractors(index int) []string {
	arc := w.StoryArcs[index]
	next := arc.V2.Next
	excluded := map[int]bool{arc.PersonIndex: true, next.Who: true}
	people := w.storyV13OtherPeople(index, excluded, 1)
	out := []string{w.People[people[0]].Name}
	accepted := append([]string{next.What}, next.WhatAccept...)
	for step := 1; len(out) < 2 && step <= len(storyNextActions); step++ {
		candidate := storyNextActions[(index+step)%len(storyNextActions)]
		if candidate.what == next.What || grade.ContainedInAny(candidate.what, accepted) || storyAcceptOverlap(candidate.accept, accepted) {
			continue
		}
		out = append(out, candidate.what)
	}
	channelAccept := storyChannelAccept(next.Channel)
	for step := 1; len(out) < 3 && step <= len(storyChannels); step++ {
		candidate := storyChannels[(index+step)%len(storyChannels)]
		if candidate.channel == next.Channel || grade.ContainedInAny(candidate.channel, channelAccept) || grade.ContainedInAny(candidate.channel, []string{next.What}) {
			continue
		}
		out = append(out, candidate.channel)
	}
	return out
}

func storyAcceptOverlap(a, b []string) bool {
	for _, x := range a {
		if grade.ContainedInAny(x, b) {
			return true
		}
	}
	return false
}

func (w World) storyV13QuantityDistractors(index int) []string {
	q := w.StoryArcs[index].V2.Quantity
	correct := storyQuantityValue(*q)
	values := []int{q.Base, q.Delta}
	for step := 1; step <= len(w.StoryArcs); step++ {
		other := w.StoryArcs[(index+step)%len(w.StoryArcs)].V2
		if other != nil && other.Quantity != nil && other.Quantity.Kind == q.Kind {
			values = append(values, storyQuantityValue(*other.Quantity))
		}
	}
	out := make([]string, 0, 3)
	for _, value := range values {
		candidate := fmt.Sprintf("%d", value)
		if value > 0 && value != correct && !contains(out, candidate) {
			out = append(out, candidate)
		}
		if len(out) == 3 {
			return out
		}
	}
	for delta := 1; len(out) < 3; delta++ {
		candidate := fmt.Sprintf("%d", correct+delta)
		if q.Kind == "money" {
			candidate = fmt.Sprintf("%d", correct+delta*12_500)
		}
		if !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyV13LessonDistractors(index int) []string {
	lesson := w.StoryArcs[index].V2.Lesson
	accepted := []string{lesson.canonical}
	for _, concept := range lesson.concepts {
		accepted = append(accepted, concept.term)
		accepted = append(accepted, concept.accept...)
	}
	out := make([]string, 0, 3)
	for step := 1; len(out) < 3 && step <= len(storyV13Lessons); step++ {
		other := storyV13Lessons[(index+step)%len(storyV13Lessons)]
		if other.canonical == lesson.canonical {
			continue
		}
		for _, concept := range other.concepts {
			if len(out) == 3 {
				break
			}
			if grade.ContainedInAny(concept.term, accepted) || storyAcceptOverlap(concept.accept, accepted) || contains(out, concept.term) {
				continue
			}
			out = append(out, concept.term)
			break
		}
	}
	return out
}
