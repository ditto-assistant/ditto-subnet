package universe

import (
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// QuestionPlan is the validator-internal bridge between world generation and a
// rendered MemoryCase. RequiredPairIDs identify the planted needles; Facts,
// Constraints, and Operations make the intended reasoning burden auditable.
// Only Case crosses the harness /run boundary.
type QuestionPlan struct {
	Case            protocol.MemoryCase
	RequiredPairIDs []string
	Facts           []string
	Constraints     []string
	Operations      []string

	oracleKind  string
	oracleIndex int
}

const (
	oracleContactCurrent         = "contact-current"
	oracleContactPrevious        = "contact-previous"
	oracleProjectOutstanding     = "project-outstanding"
	oracleProjectLeadCurrent     = "project-lead-current"
	oracleProjectLeadPrevious    = "project-lead-previous"
	oracleTripCurrent            = "trip-current"
	oracleTripChangedLegPrevious = "trip-changed-leg-previous"
	oracleTripChangedLegCurrent  = "trip-changed-leg-current"
	oracleTripLongestCurrent     = "trip-longest-current"
	oracleStoryBalanceCurrent    = "story-balance-current"
	oracleStoryBudgetDelta       = "story-budget-delta"
	oracleStoryPostApproval      = "story-post-approval-balance"
	oracleStoryLaterNetChange    = "story-later-net-change"
	oracleStoryContactCurrent    = "story-contact-current"
	oracleStoryLesson            = "story-lesson"
	oracleStoryOutcomeSummary    = "story-outcome-summary"
)

var errLexicalShortcut = errors.New("lexical shortcut")

// QuestionPlans executes the v8 contract in three explicit phases:
//
//  1. Generate produced one coherent world and planted its facts in Pairs.
//  2. candidates derives latent multi-fact plans from that world.
//  3. validatePlan proves every rendered question has one answer, all needles
//     exist, and the expected answer is the deterministic oracle result.
//
// The final seed-keyed shuffle varies the public mix without changing the
// answerability proof. Accidental lexical shortcuts are deterministically
// excluded; every structural ambiguity, missing needle, or oracle mismatch
// fails generation instead of entering a scored dataset.
func (w World) QuestionPlans(count int) ([]QuestionPlan, error) {
	if count <= 0 {
		return nil, nil
	}
	candidates := w.questionCandidates()
	if count > len(candidates) {
		return nil, fmt.Errorf("world has %d validated question candidates, need %d", len(candidates), count)
	}
	r := rand.New(rand.NewSource(questionSeed(w.Seed)))
	r.Shuffle(len(candidates), func(i, j int) { candidates[i], candidates[j] = candidates[j], candidates[i] })
	storyPlans := make([]QuestionPlan, 0, len(w.StoryArcs)*5)
	ordinaryPlans := make([]QuestionPlan, 0, len(candidates))
	seenQuestions := make(map[string]bool, len(candidates))
	for i := range candidates {
		if err := w.validatePlan(candidates[i]); err != nil {
			// A random surface can occasionally create an accidental lexical
			// shortcut. Exclude that surface deterministically; structural
			// ambiguity, missing evidence, and oracle drift still fail generation.
			if errors.Is(err, errLexicalShortcut) {
				continue
			}
			return nil, fmt.Errorf("candidate %s: %w", candidates[i].Case.QuestionType, err)
		}
		q := strings.ToLower(strings.TrimSpace(candidates[i].Case.Question))
		if seenQuestions[q] {
			return nil, fmt.Errorf("duplicate rendered question %q", candidates[i].Case.Question)
		}
		seenQuestions[q] = true
		if isStoryOracle(candidates[i].oracleKind) {
			storyPlans = append(storyPlans, candidates[i])
		} else {
			ordinaryPlans = append(ordinaryPlans, candidates[i])
		}
	}
	if len(storyPlans)+len(ordinaryPlans) < count {
		return nil, fmt.Errorf("world has only %d shortcut-free question candidates, need %d", len(storyPlans)+len(ordinaryPlans), count)
	}
	// Every scored medium/full profile carries a fixed number of deep-story
	// programs. Seed changes their entities, joins, state transitions, wording,
	// and placement, but not the difficulty-class count.
	storyQuota := storyQuestionQuota(count, len(storyPlans))
	if len(ordinaryPlans) < count-storyQuota {
		storyQuota = count - len(ordinaryPlans)
	}
	selected := make([]QuestionPlan, 0, count)
	selected = append(selected, storyPlans[:storyQuota]...)
	selected = append(selected, ordinaryPlans[:count-storyQuota]...)
	r.Shuffle(len(selected), func(i, j int) { selected[i], selected[j] = selected[j], selected[i] })
	return selected, nil
}

func storyQuestionQuota(count, available int) int {
	want := count / 5
	if count >= 40 {
		want = available
	}
	if want > available {
		want = available
	}
	return want
}

func isStoryOracle(kind string) bool { return strings.HasPrefix(kind, "story-") }

func (w World) questionCandidates() []QuestionPlan {
	out := make([]QuestionPlan, 0, 2*len(w.People)+3*len(w.Projects)+4*len(w.Trips))
	for i, p := range w.People {
		current := contactCurrentQuestion(p, i)
		out = append(out, w.personPlan(oracleContactCurrent, i, current, p.Email, p.PreviousEmail))

		previous := []string{
			fmt.Sprintf("Before %s changed addresses, which email had I saved for my %s in %s from the %s?", p.Nickname, p.Relation, p.City, p.Context),
			fmt.Sprintf("What was the earlier email for my %s in %s I call %s from the %s, before the contact correction?", p.Relation, p.City, p.Nickname, p.Context),
			fmt.Sprintf("I need the pre-correction email for %s — my %s who handled the %s in %s. What was it?", p.Nickname, p.Relation, p.Context, p.City),
			fmt.Sprintf("Looking back before the update, which email did I first have for %s, my %s from the %s who lives in %s?", p.Nickname, p.Relation, p.Context, p.City),
		}[i%4]
		out = append(out, w.personPlan(oracleContactPrevious, i, previous, p.PreviousEmail, p.Email))
	}

	for i, p := range w.Projects {
		lead := w.People[p.Lead]
		outstanding := []string{
			fmt.Sprintf("For %q, the %s work for %s, what is still owed to %s once the approved correction and the payment already sent are reconciled?", p.Alias, p.Purpose, p.Client, p.Vendor),
			fmt.Sprintf("AP needs the remaining balance for %s's invoice on %q for %s. Use the corrected total, not the draft, and account for our payment.", p.Vendor, p.Alias, p.Client),
			fmt.Sprintf("What remains on the corrected %s bill tied to %q, the %s project for %s, after what we already paid?", p.Vendor, p.Alias, p.Purpose, p.Client),
			fmt.Sprintf("Reconcile %q for %s: after replacing the original %s invoice figure with the approved one and subtracting the partial payment, what balance remains?", p.Alias, p.Client, p.Vendor),
		}[i%4]
		out = append(out, QuestionPlan{
			Case:            memoryCase(w.Seed, oracleProjectOutstanding, i, outstanding, fmt.Sprintf("%d", p.OutstandingCents), protocol.AnswerMoney, w.moneyDistractors(i, p.OutstandingCents)),
			RequiredPairIDs: []string{p.ContextPairID, p.LedgerPairID, p.CorrectionPairID},
			Facts:           []string{"project alias", "client and purpose", "draft invoice and payment", "approved correction", "vendor identity"},
			Constraints:     []string{p.Alias, p.Client, p.Vendor, p.Purpose}, Operations: []string{"resolve project alias", "replace draft with correction", "subtract payment"},
			oracleKind: oracleProjectOutstanding, oracleIndex: i,
		})

		current := []string{
			fmt.Sprintf("Who should get the %q handoff internally, and what current email should I use? I mean the %s work for %s, not an outside recipient.", p.Alias, p.Purpose, p.Client),
			fmt.Sprintf("For %s's %s project that we call %q, give me the corrected email for its internal owner.", p.Client, p.Purpose, p.Alias),
			fmt.Sprintf("What up-to-date email belongs to the person running %q on our side — %s's %s engagement?", p.Alias, p.Client, p.Purpose),
			fmt.Sprintf("I am sending the %q update. Resolve the internal owner from the %s work for %s, then use their current rather than original email.", p.Alias, p.Purpose, p.Client),
		}[i%4]
		out = append(out, w.projectLeadPlan(oracleProjectLeadCurrent, i, current, lead.Email, lead.PreviousEmail))

		previous := []string{
			fmt.Sprintf("Before the address correction, what email did I have for the internal lead on %q, the %s work for %s?", p.Alias, p.Purpose, p.Client),
			fmt.Sprintf("Find the earlier email for whoever owns %q, our %s project for %s — not their current one.", p.Alias, p.Purpose, p.Client),
			fmt.Sprintf("What was the original email for the internal owner of the %s project for %s that we call %q?", p.Purpose, p.Client, p.Alias),
			fmt.Sprintf("Looking back before the update, which email was saved for %q's internal owner on the %s work for %s?", p.Alias, p.Purpose, p.Client),
		}[i%4]
		out = append(out, w.projectLeadPlan(oracleProjectLeadPrevious, i, previous, lead.PreviousEmail, lead.Email))
	}

	for i, trip := range w.Trips {
		changed := changedLeg(trip)
		changedCountry := strings.TrimPrefix(trip.Countries[changed], "the ")
		commonEvidence := []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID}
		constraints := []string{trip.Alias, trip.Purpose, trip.When, changedCountry}

		current := []string{
			fmt.Sprintf("How many days is %s now — the %s trip we took in %s through %s, %s, and %s — after the change?", trip.Alias, trip.Purpose, trip.When, trip.Countries[0], trip.Countries[1], trip.Countries[2]),
			fmt.Sprintf("We changed the %s part of %s, our %s trip from %s. How many days is the whole trip now?", changedCountry, trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Can you piece together the updated stays for %s, our %s trip from %s? How long is the trip altogether now?", trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Remind me how long %s is now — the %s trip from %s — after we changed the %s stay.", trip.Alias, trip.Purpose, trip.When, changedCountry),
		}[i%4]
		out = append(out, tripPlan(w, oracleTripCurrent, i, current, trip.CurrentDays, commonEvidence, constraints))

		previous := []string{
			fmt.Sprintf("Before we changed one of the stays on %s, our %s trip from %s, how many days had we planned for that stay?", trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Thinking back to the first version of %s — the %s trip from %s — how long was the stay we later changed?", trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("How many days had we originally planned for the stay we later revised on %s, our %s trip from %s?", trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("In our first plan for %s, the %s trip from %s, how long was the stay that eventually changed?", trip.Alias, trip.Purpose, trip.When),
		}[i%4]
		out = append(out, tripPlan(w, oracleTripChangedLegPrevious, i, previous, trip.OldLegDays[changed], commonEvidence, []string{trip.Alias, trip.Purpose, trip.When}))

		leg := []string{
			fmt.Sprintf("On %s, the %s trip from %s, how many days are we spending in %s after the change?", trip.Alias, trip.Purpose, trip.When, trip.Countries[changed]),
			fmt.Sprintf("How long is the updated stay in %s for %s, the %s trip from %s?", trip.Countries[changed], trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("For %s, our %s trip, how many days is the changed %s stay now?", trip.Alias, trip.Purpose, changedCountry),
			fmt.Sprintf("After changing the %s part of %s, our trip from %s, how many days are we spending there?", changedCountry, trip.Alias, trip.When),
		}[i%4]
		out = append(out, tripPlan(w, oracleTripChangedLegCurrent, i, leg, trip.LegDays[changed], commonEvidence, constraints))

		longest := trip.LegDays[0]
		for _, days := range trip.LegDays[1:] {
			if days > longest {
				longest = days
			}
		}
		change := []string{
			fmt.Sprintf("After changing the %s stay, what is the longest amount of time we spend in any one country on %s, our %s trip from %s?", changedCountry, trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Looking across the updated plan for %s, the %s trip from %s, how many days is our longest stay?", trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Once the %s change is included, what is the longest stay on %s, our %s trip from %s?", changedCountry, trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("For %s in %s, our %s trip with the changed %s stay, how many days is the longest stop?", trip.Alias, trip.When, trip.Purpose, changedCountry),
		}[i%4]
		out = append(out, tripPlan(w, oracleTripLongestCurrent, i, change, longest, commonEvidence, constraints))
	}
	out = append(out, w.storyQuestionCandidates()...)
	return out
}

func contactCurrentQuestion(p Person, index int) string {
	return []string{
		fmt.Sprintf("For the %s follow-up, what email should I actually use now for %s at %s? I want to avoid sending the note to an inbox nobody checks anymore, so please double-check before I hit send.", p.Context, p.Name, p.Employer),
		fmt.Sprintf("I need to reach %s at %s about the %s. Which email is current? I remember we had to replace an older one, and I would rather verify than have this disappear.", p.Name, p.Employer, p.Context),
		fmt.Sprintf("Which up-to-date email belongs to %s at %s, the person from the %s? I am pulling together the final details before I send anything and want to make sure it reaches them.", p.Name, p.Employer, p.Context),
		fmt.Sprintf("What is the corrected email for %s at %s? This is for the %s follow-up, and I do not want the message disappearing into their old workplace. Please check the latest one.", p.Name, p.Employer, p.Context),
	}[index%4]
}

// ContactCurrentPlan exposes one fully validated person-contact program for
// graph-isolation composition. Callers may change only case identity, scope, and
// the forbidden cross-graph answer; the planted evidence and oracle remain the
// same world contract used by ordinary V8 questions.
func (w World) ContactCurrentPlan(index int) (QuestionPlan, error) {
	if index < 0 || index >= len(w.People) {
		return QuestionPlan{}, fmt.Errorf("contact index %d out of range", index)
	}
	p := w.People[index]
	plan := w.personPlan(oracleContactCurrent, index, contactCurrentQuestion(p, index), p.Email, p.PreviousEmail)
	if err := w.validatePlan(plan); err != nil {
		return QuestionPlan{}, err
	}
	return plan, nil
}

func (w World) personPlan(kind string, index int, question, answer, extraDistractor string) QuestionPlan {
	p := w.People[index]
	evidence := []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID}
	facts := []string{"full identity", "nickname and relationship", "work and city context", "original address"}
	operations := []string{"resolve nickname", "join identity to work context", "select prior address state"}
	constraints := []string{p.Nickname, p.Relation, p.Context, p.Employer}
	if kind == oracleContactCurrent {
		evidence = []string{p.IdentityPairID, p.WorkPairID, p.CorrectionPairID}
		facts = []string{"full identity", "nickname and relationship", "event context", "current employer", "current address"}
		operations = []string{"resolve the relationship and event to a person", "join the person to their current employer", "follow the nickname-and-employer correction to the current address"}
		constraints = []string{p.Name, p.Employer, p.Context}
	}
	return QuestionPlan{
		Case:            memoryCase(w.Seed, kind, index, question, answer, protocol.AnswerValue, w.emailDistractors(index, extraDistractor)),
		RequiredPairIDs: evidence,
		Facts:           facts,
		Constraints:     constraints, Operations: operations,
		oracleKind: kind, oracleIndex: index,
	}
}

func (w World) projectLeadPlan(kind string, index int, question, answer, extraDistractor string) QuestionPlan {
	p := w.Projects[index]
	lead := w.People[p.Lead]
	evidence := []string{p.ContextPairID, lead.WorkPairID, lead.EmailPairID}
	facts := []string{"project alias", "client and purpose", "internal owner", "owner's prior employer", "owner's original address"}
	operations := []string{"resolve project alias", "follow ownership edge", "select the owner's prior employer", "select prior address state"}
	if kind == oracleProjectLeadCurrent {
		evidence = []string{p.ContextPairID, lead.IdentityPairID, lead.WorkPairID, lead.CorrectionPairID}
		facts = []string{"project alias", "client and purpose", "internal owner", "owner's current employer", "owner's current address"}
		operations = []string{"resolve project alias", "follow the project ownership edge", "resolve the owner's nickname", "join the owner to their current employer", "follow the nickname-and-employer correction to the current address"}
	}
	return QuestionPlan{
		Case:            memoryCase(w.Seed, kind, index, question, answer, protocol.AnswerValue, w.emailDistractors(p.Lead, extraDistractor)),
		RequiredPairIDs: evidence,
		Facts:           facts,
		Constraints:     []string{p.Alias, p.Client, p.Purpose}, Operations: operations,
		oracleKind: kind, oracleIndex: index,
	}
}

func tripPlan(w World, kind string, index int, question string, answer int, evidence, constraints []string) QuestionPlan {
	return QuestionPlan{
		Case:            memoryCase(w.Seed, kind, index, question, fmt.Sprintf("%d", answer), protocol.AnswerNumber, w.tripDistractors(index, answer)),
		RequiredPairIDs: append([]string(nil), evidence...),
		Facts:           []string{"trip alias", "purpose and time", "travel companion", "three original leg durations", "changed country", "corrected leg duration"},
		Constraints:     append([]string(nil), constraints...), Operations: []string{"resolve trip alias", "follow companion link", "apply itinerary correction", "select requested state", "sum or compare legs"},
		oracleKind: kind, oracleIndex: index,
	}
}

func (w World) storyQuestionCandidates() []QuestionPlan {
	out := make([]QuestionPlan, 0, len(w.StoryArcs)*7)
	for i, arc := range w.StoryArcs {
		person := w.People[arc.PersonIndex]
		trip := w.Trips[arc.TripIndex]
		r := rand.New(rand.NewSource(storyQuestionSeed(w.Seed, arc.ID)))
		anchor, constraints := storyAnchor(r, person, trip)

		balanceTask := []string{
			"Where did the available budget land after everything?",
			"What is the actual amount we have left now?",
			"After all the changes and payments, what remains?",
			"Can you work out the final available balance for me?",
		}[r.Intn(4)]
		balancePlan := w.storyPlan(oracleStoryBalanceCurrent, i, composeStoryQuestion(r, anchor, balanceTask), constraints,
			fmt.Sprintf("%d", arc.CurrentBalanceCents), protocol.AnswerMoney, w.storyBalanceDistractors(i), nil,
			[]string{"personal relationship and event", "support case", "purchase order", "original budget", "budget correction", "prior payment", "later cost", "credit"},
			storyBalanceOperations(arc))

		deltaTask := []string{
			"How much was the budget correction itself?",
			"What was the size of the later budget change on its own?",
			"How much did the approval correction add or remove?",
			"What amount did finance change the budget by?",
		}[r.Intn(4)]
		deltaPlan := w.storyPlan(oracleStoryBudgetDelta, i, composeStoryQuestion(r, anchor, deltaTask), constraints,
			fmt.Sprintf("%d", absInt(arc.BudgetDeltaCents)), protocol.AnswerMoney, w.storyDeltaDistractors(i), nil,
			[]string{"personal relationship and event", "support case", "purchase order", "original budget", "budget correction", "separate payment", "separate later cost and credit"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "isolate the budget correction", "convert the correction magnitude to cents"})

		postTask := []string{
			"What balance did we have after the corrected approval and first payment, before the later charges?",
			"How much was left after the approved change and payment, but before the extra cost and credit?",
			"What was available at the middle point, right after the revised approval and first payment?",
			"Before the last expense and credit arrived, what balance were we working with?",
		}[r.Intn(4)]
		postAnswer := arc.BaseBudgetCents + arc.BudgetDeltaCents - arc.PaidCents
		postPlan := w.storyPlan(oracleStoryPostApproval, i, composeStoryQuestion(r, anchor, postTask), constraints,
			fmt.Sprintf("%d", postAnswer), protocol.AnswerMoney, w.storyPostApprovalDistractors(i), nil,
			[]string{"personal relationship and event", "support case", "purchase order", "original budget", "budget correction", "prior payment", "later cost boundary", "later credit boundary"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "apply the budget correction", "subtract the prior payment", "exclude later cost and credit", "convert the intermediate result to cents"})

		net := arc.BudgetDeltaCents - arc.UnexpectedCostCents + arc.CreditCents
		direction := "increase"
		wrongDirection := "decrease"
		if net < 0 {
			direction, wrongDirection = wrongDirection, direction
		}
		netTask := []string{
			"Taken together, did the later correction, cost, and credit raise or lower the balance, and by how much?",
			"What was the net effect of the later budget change, expense, and credit? Give me the direction and amount.",
			"Across those final three changes, did we end up gaining or losing money, and how much?",
			"Did the follow-up changes move the balance up or down overall, and by how much?",
		}[r.Intn(4)]
		netItems := []string{direction, fmt.Sprintf("%d", absInt(net))}
		netPlan := w.storyPlan(oracleStoryLaterNetChange, i, composeStoryQuestion(r, anchor, netTask), constraints,
			strings.Join(netItems, "; "), protocol.AnswerList, storyNetDistractors(arc, net, wrongDirection), nil,
			[]string{"personal relationship and event", "support case", "purchase order", "budget correction", "later cost", "credit"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "combine the later budget change cost and credit", "classify the net direction", "convert the net magnitude to cents"})
		netPlan.Case.AnswerItems = netItems
		netPlan.Case.AnswerItemKinds = []string{protocol.AnswerDirection, protocol.AnswerMoney}

		financialPlans := []QuestionPlan{balancePlan, deltaPlan, postPlan, netPlan}
		financialOrder := r.Perm(len(financialPlans))
		for _, index := range financialOrder {
			out = append(out, financialPlans[index])
		}

		contactTask := []string{
			"What email should I use now?",
			"Which email is the right one now?",
			"Where should I send the note?",
			"What email did we end up using?",
		}[r.Intn(4)]
		out = append(out, w.storyPlan(oracleStoryContactCurrent, i, composeStoryQuestion(r, anchor, contactTask), constraints,
			arc.CurrentContact, protocol.AnswerValue, w.storyContactDistractors(i), nil,
			[]string{"personal relationship and event", "requested working name", "support case", "purchase order", "original work channel", "channel correction"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "replace the stale work-only contact"}))

		lessonTask := []string{
			"What advice did I take from the whole mess?",
			"What did I say I would do differently next time?",
			"What practical lesson did I learn from this?",
			"What was the rule I wanted to remember afterward?",
		}[r.Intn(4)]
		out = append(out, w.storyPlan(oracleStoryLesson, i, composeStoryQuestion(r, anchor, lessonTask), constraints,
			arc.Lesson, protocol.AnswerValue, w.storyLessonDistractors(i), arc.LessonAcceptAny,
			[]string{"personal relationship and event", "support case", "purchase order", "corrected outcome", "explicit lesson"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "select the outcome lesson"}))

		summaryTask := []string{
			"Can you give me the current reviewer email, final available balance, and the advice I wrote down?",
			"Remind me of the final reviewer email, the balance we landed on, and the lesson from it.",
			"Where did this leave us: which email, how much money, and what rule for next time?",
			"Pull together the current contact, the final amount, and what I learned from the experience.",
		}[r.Intn(4)]
		summaryItems := []string{arc.CurrentContact, fmt.Sprintf("%d", arc.CurrentBalanceCents), arc.Lesson}
		summaryAnswer := strings.Join(summaryItems, "; ")
		summary := w.storyPlan(oracleStoryOutcomeSummary, i, composeStoryQuestion(r, anchor, summaryTask), constraints,
			summaryAnswer, protocol.AnswerList, w.storyOutcomeDistractors(i), nil,
			[]string{"personal relationship and event", "support case", "purchase order", "original budget and payment", "budget correction", "later cost and credit", "contact correction", "explicit lesson"},
			[]string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "reconcile the current balance", "replace the stale work-only contact", "select the outcome lesson"})
		summary.Case.AnswerItems = summaryItems
		summary.Case.AnswerItemKinds = []string{protocol.AnswerValue, protocol.AnswerMoney, protocol.AnswerValue}
		summary.Case.AnswerItemAcceptAny = [][]string{nil, nil, append([]string(nil), arc.LessonAcceptAny...)}
		out = append(out, summary)
	}
	return out
}

func storyAnchor(r *rand.Rand, person Person, trip Trip) (string, []string) {
	type surface struct {
		text        string
		constraints []string
	}
	options := []surface{
		{fmt.Sprintf("Think back to the %s with %s.", trip.Alias, person.Nickname), []string{trip.Alias, person.Nickname}},
		{fmt.Sprintf("Go back to when %s and I were talking about the %s.", person.Nickname, trip.Alias), []string{person.Nickname, trip.Alias}},
		{fmt.Sprintf("This is about the %s, the one I planned with %s.", trip.Alias, person.Nickname), []string{trip.Alias, person.Nickname}},
		{fmt.Sprintf("Remember my conversation with %s about the %s?", person.Nickname, trip.Alias), []string{person.Nickname, trip.Alias}},
		{fmt.Sprintf("I mean the thread with %s that began around the %s.", person.Nickname, trip.Alias), []string{person.Nickname, trip.Alias}},
	}
	choice := options[r.Intn(len(options))]
	return choice.text, append([]string(nil), choice.constraints...)
}

func composeStoryQuestion(_ *rand.Rand, anchor, task string) string {
	return anchor + " " + task
}

func (w World) storyPlan(kind string, index int, question string, constraints []string, answer, answerKind string, distractors, acceptAny []string, facts, operations []string) QuestionPlan {
	arc := w.StoryArcs[index]
	caseValue := memoryCase(w.Seed, kind, index, question, answer, answerKind, distractors)
	caseValue.AcceptAny = append([]string(nil), acceptAny...)
	return QuestionPlan{
		Case: caseValue, RequiredPairIDs: append([]string(nil), arc.StoryPairIDs[:]...),
		Facts:       append([]string(nil), facts...),
		Constraints: append([]string(nil), constraints...),
		Operations:  append([]string(nil), operations...), oracleKind: kind, oracleIndex: index,
	}
}

func storyBalanceOperations(arc StoryArc) []string {
	direction := "apply the budget increase"
	if arc.BudgetDeltaCents < 0 {
		direction = "apply the budget reduction"
	}
	return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", direction, "subtract the prior payment", "subtract the later cost", "add the separate credit", "convert the result to cents"}
}

func (w World) storyBalanceDistractors(index int) []string {
	arc := w.StoryArcs[index]
	values := []int{arc.BaseBudgetCents, arc.BaseBudgetCents + arc.BudgetDeltaCents - arc.PaidCents, arc.CurrentBalanceCents - arc.CreditCents}
	for j := 1; len(values) < 3 && j < len(w.StoryArcs); j++ {
		values = append(values, w.StoryArcs[(index+j)%len(w.StoryArcs)].CurrentBalanceCents)
	}
	out := make([]string, 0, 3)
	for _, value := range values {
		candidate := fmt.Sprintf("%d", value)
		if value > 0 && value != arc.CurrentBalanceCents && !contains(out, candidate) {
			out = append(out, candidate)
		}
		if len(out) == 3 {
			break
		}
	}
	for delta := 12_500; len(out) < 3; delta += 12_500 {
		candidate := fmt.Sprintf("%d", arc.CurrentBalanceCents+delta)
		if !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyDeltaDistractors(index int) []string {
	arc := w.StoryArcs[index]
	values := []int{arc.UnexpectedCostCents, arc.CreditCents, arc.PaidCents}
	out := make([]string, 0, 3)
	for _, value := range values {
		candidate := fmt.Sprintf("%d", value)
		if value != absInt(arc.BudgetDeltaCents) && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	for delta := 12_500; len(out) < 3; delta += 12_500 {
		candidate := fmt.Sprintf("%d", absInt(arc.BudgetDeltaCents)+delta)
		if !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyPostApprovalDistractors(index int) []string {
	arc := w.StoryArcs[index]
	correct := arc.BaseBudgetCents + arc.BudgetDeltaCents - arc.PaidCents
	values := []int{arc.BaseBudgetCents - arc.PaidCents, arc.CurrentBalanceCents, arc.BaseBudgetCents + arc.BudgetDeltaCents}
	out := make([]string, 0, 3)
	for _, value := range values {
		candidate := fmt.Sprintf("%d", value)
		if value > 0 && value != correct && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	for delta := 12_500; len(out) < 3; delta += 12_500 {
		candidate := fmt.Sprintf("%d", correct+delta)
		if !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func storyNetDistractors(arc StoryArc, net int, wrongDirection string) []string {
	out := []string{wrongDirection}
	correct := absInt(net)
	for _, value := range []int{arc.UnexpectedCostCents, arc.CreditCents, absInt(arc.BudgetDeltaCents), arc.PaidCents} {
		candidate := fmt.Sprintf("%d", value)
		if value != correct && !contains(out, candidate) {
			out = append(out, candidate)
		}
		if len(out) == 3 {
			return out
		}
	}
	for delta := 12_500; len(out) < 3; delta += 12_500 {
		candidate := fmt.Sprintf("%d", correct+delta)
		if !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyContactDistractors(index int) []string {
	arc := w.StoryArcs[index]
	out := []string{arc.OriginalContact}
	for j := 1; len(out) < 3 && j < len(w.StoryArcs)+1; j++ {
		candidate := w.StoryArcs[(index+j)%len(w.StoryArcs)].CurrentContact
		if candidate != arc.CurrentContact && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	for len(out) < 3 {
		out = append(out, fmt.Sprintf("unused.review%d@post.team", index+len(out)+1))
	}
	return out
}

func (w World) storyLessonDistractors(index int) []string {
	arc := w.StoryArcs[index]
	out := []string{}
	for j := 1; len(out) < 3 && j < len(w.StoryArcs)+1; j++ {
		candidate := w.StoryArcs[(index+j)%len(w.StoryArcs)].Lesson
		if candidate != arc.Lesson && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	fallbacks := []string{"move quickly and document later", "keep every context in one flat note", "treat the first number as final"}
	for _, candidate := range fallbacks {
		if len(out) == 3 {
			break
		}
		if candidate != arc.Lesson && !contains(out, candidate) {
			out = append(out, candidate)
		}
	}
	return out
}

func (w World) storyOutcomeDistractors(index int) []string {
	arc := w.StoryArcs[index]
	wrongBalance := w.storyBalanceDistractors(index)[0]
	wrongLesson := w.storyLessonDistractors(index)[0]
	return []string{arc.OriginalContact, wrongBalance, wrongLesson}
}

func storyQuestionSeed(seed int64, id string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v8-story-question:%d:%s", seed, id)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

func memoryCase(seed int64, kind string, index int, question, answer, answerKind string, distractors []string) protocol.MemoryCase {
	id := protocol.OpaqueCaseID(seed, "world-memory-"+kind, index)
	return protocol.MemoryCase{
		BenchVersion: protocol.BenchVersionV8, ID: id, QuestionID: id,
		QuestionType: "world-" + kind, Question: question, ExpectedAnswer: answer,
		AnswerKind: answerKind, DistractorAnswers: distractors,
	}
}

func (w World) validatePlan(plan QuestionPlan) error {
	minConstraints := 3
	if isStoryOracle(plan.oracleKind) {
		minConstraints = 2
	}
	if len(plan.Facts) < 4 || len(plan.Constraints) < minConstraints || len(plan.Operations) < 2 {
		return fmt.Errorf("under-specified plan: facts=%d constraints=%d operations=%d", len(plan.Facts), len(plan.Constraints), len(plan.Operations))
	}
	if grade.Hit(plan.Case.ExpectedAnswer, plan.Case.Question) {
		return fmt.Errorf("question leaks expected answer %q", plan.Case.ExpectedAnswer)
	}
	required := make(map[string]bool, len(plan.RequiredPairIDs))
	for _, pairID := range plan.RequiredPairIDs {
		required[pairID] = true
	}
	for _, pair := range w.Pairs {
		if !required[pair.PairID] {
			continue
		}
		body := pair.Prompt + " " + pair.Response
		if !grade.Hit(plan.Case.ExpectedAnswer, body) {
			continue
		}
		if isStoryOracle(plan.oracleKind) {
			// These questions are deliberately short and conversational, so a few
			// generic words can dominate a lexical-overlap ratio. The answer-leak
			// check above plus exact three-record counterfactual removal below are
			// the authoritative shortcut proofs for this composed family.
			continue
		}
		if overlap := contentOverlap(plan.Case.Question, body); overlap > 0.34 {
			return fmt.Errorf("%w: answer-bearing pair %s overlaps question at %.2f", errLexicalShortcut, pair.PairID, overlap)
		}
	}
	if got, ok := w.resolveWithEvidence(plan, required); !ok || got != plan.Case.ExpectedAnswer {
		return fmt.Errorf("oracle resolved %q, case expects %q", got, plan.Case.ExpectedAnswer)
	}
	if got := renderedConstraintCount(plan); got < minConstraints {
		return fmt.Errorf("rendered question contains %d declared constraints, want at least %d", got, minConstraints)
	}
	if got := w.subjectMatches(plan); got != 1 {
		return fmt.Errorf("constraints resolve %d subjects, want exactly one", got)
	}
	wantEvidence := w.oracleEvidence(plan)
	if !sameStrings(plan.RequiredPairIDs, wantEvidence) {
		return fmt.Errorf("declared evidence %v does not match oracle dependency %v", plan.RequiredPairIDs, wantEvidence)
	}
	wantOperations := w.oracleOperations(plan)
	if !sameStrings(plan.Operations, wantOperations) {
		return fmt.Errorf("declared operations %v do not match oracle operations %v", plan.Operations, wantOperations)
	}
	if err := w.validateStoryPlan(plan); err != nil {
		return err
	}
	for _, omitted := range plan.RequiredPairIDs {
		counterfactual := make(map[string]bool, len(required)-1)
		for pairID := range required {
			if pairID != omitted {
				counterfactual[pairID] = true
			}
		}
		if got, ok := w.resolveWithEvidence(plan, counterfactual); ok && got == plan.Case.ExpectedAnswer {
			return fmt.Errorf("evidence pair %s is not causally required", omitted)
		}
	}
	pairPos := make(map[string]int, len(w.Pairs))
	for i, pair := range w.Pairs {
		pairPos[pair.PairID] = i
	}
	lo, hi := len(w.Pairs), -1
	seenEvidence := map[string]bool{}
	for _, id := range plan.RequiredPairIDs {
		if seenEvidence[id] {
			return fmt.Errorf("duplicate evidence pair %s", id)
		}
		seenEvidence[id] = true
		pos, ok := pairPos[id]
		if !ok {
			return fmt.Errorf("missing planted evidence pair %s", id)
		}
		if pos < lo {
			lo = pos
		}
		if pos > hi {
			hi = pos
		}
	}
	if len(plan.RequiredPairIDs) < 3 || hi-lo < len(w.Pairs)/6 {
		return fmt.Errorf("evidence is not long-distance: pairs=%d span=%d/%d", len(plan.RequiredPairIDs), hi-lo, len(w.Pairs))
	}
	if len(plan.Case.DistractorAnswers) != 3 {
		return fmt.Errorf("distractor count=%d, want 3", len(plan.Case.DistractorAnswers))
	}
	seenDistractors := map[string]bool{}
	for _, value := range plan.Case.DistractorAnswers {
		if value == plan.Case.ExpectedAnswer || seenDistractors[value] {
			return fmt.Errorf("invalid distractor %q", value)
		}
		seenDistractors[value] = true
	}
	return nil
}

func (w World) oracleEvidence(plan QuestionPlan) []string {
	switch plan.oracleKind {
	case oracleContactCurrent:
		p := w.People[plan.oracleIndex]
		return []string{p.IdentityPairID, p.WorkPairID, p.CorrectionPairID}
	case oracleContactPrevious:
		p := w.People[plan.oracleIndex]
		return []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID}
	case oracleProjectOutstanding:
		p := w.Projects[plan.oracleIndex]
		return []string{p.ContextPairID, p.LedgerPairID, p.CorrectionPairID}
	case oracleProjectLeadCurrent:
		p := w.Projects[plan.oracleIndex]
		lead := w.People[p.Lead]
		return []string{p.ContextPairID, lead.IdentityPairID, lead.WorkPairID, lead.CorrectionPairID}
	case oracleProjectLeadPrevious:
		p := w.Projects[plan.oracleIndex]
		lead := w.People[p.Lead]
		return []string{p.ContextPairID, lead.WorkPairID, lead.EmailPairID}
	case oracleTripCurrent, oracleTripChangedLegPrevious, oracleTripChangedLegCurrent, oracleTripLongestCurrent:
		trip := w.Trips[plan.oracleIndex]
		return []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID}
	case oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval, oracleStoryLaterNetChange, oracleStoryContactCurrent, oracleStoryLesson, oracleStoryOutcomeSummary:
		arc := w.StoryArcs[plan.oracleIndex]
		return append([]string(nil), arc.StoryPairIDs[:]...)
	default:
		return nil
	}
}

func (w World) oracleOperations(plan QuestionPlan) []string {
	switch plan.oracleKind {
	case oracleContactCurrent:
		return []string{"resolve the relationship and event to a person", "join the person to their current employer", "follow the nickname-and-employer correction to the current address"}
	case oracleContactPrevious:
		return []string{"resolve nickname", "join identity to work context", "select prior address state"}
	case oracleProjectOutstanding:
		return []string{"resolve project alias", "replace draft with correction", "subtract payment"}
	case oracleProjectLeadCurrent:
		return []string{"resolve project alias", "follow the project ownership edge", "resolve the owner's nickname", "join the owner to their current employer", "follow the nickname-and-employer correction to the current address"}
	case oracleProjectLeadPrevious:
		return []string{"resolve project alias", "follow ownership edge", "select the owner's prior employer", "select prior address state"}
	case oracleTripCurrent:
		return []string{"resolve trip alias", "follow companion link", "apply itinerary correction", "select requested state", "sum or compare legs"}
	case oracleTripChangedLegPrevious:
		return []string{"resolve trip alias", "follow companion link", "apply itinerary correction", "select requested state", "sum or compare legs"}
	case oracleTripChangedLegCurrent:
		return []string{"resolve trip alias", "follow companion link", "apply itinerary correction", "select requested state", "sum or compare legs"}
	case oracleTripLongestCurrent:
		return []string{"resolve trip alias", "follow companion link", "apply itinerary correction", "select requested state", "sum or compare legs"}
	case oracleStoryBalanceCurrent:
		return storyBalanceOperations(w.StoryArcs[plan.oracleIndex])
	case oracleStoryBudgetDelta:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "isolate the budget correction", "convert the correction magnitude to cents"}
	case oracleStoryPostApproval:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "apply the budget correction", "subtract the prior payment", "exclude later cost and credit", "convert the intermediate result to cents"}
	case oracleStoryLaterNetChange:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "combine the later budget change cost and credit", "classify the net direction", "convert the net magnitude to cents"}
	case oracleStoryContactCurrent:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "replace the stale work-only contact"}
	case oracleStoryLesson:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "select the outcome lesson"}
	case oracleStoryOutcomeSummary:
		return []string{"resolve the interpersonal anchor", "follow the support case into the work story", "follow the purchase order into the outcome", "reconcile the current balance", "replace the stale work-only contact", "select the outcome lesson"}
	default:
		return nil
	}
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	aa, bb := append([]string(nil), a...), append([]string(nil), b...)
	sort.Strings(aa)
	sort.Strings(bb)
	for i := range aa {
		if aa[i] != bb[i] {
			return false
		}
	}
	return true
}

func renderedConstraintCount(plan QuestionPlan) int {
	question := strings.ToLower(plan.Case.Question)
	n := 0
	for _, constraint := range plan.Constraints {
		if strings.Contains(question, strings.ToLower(constraint)) {
			n++
		}
	}
	return n
}

// resolveWithEvidence is the generation-time counterfactual oracle. It only
// exposes the deterministic answer after every source row in the canonical
// reasoning graph is available. validatePlan removes each row in turn and
// proves that the same answer can no longer be derived, catching decorative
// evidence declarations that inflate apparent depth without adding causality.
func (w World) resolveWithEvidence(plan QuestionPlan, available map[string]bool) (string, bool) {
	has := func(ids ...string) bool {
		for _, id := range ids {
			if !available[id] {
				return false
			}
		}
		return true
	}
	switch plan.oracleKind {
	case oracleContactCurrent:
		p := w.People[plan.oracleIndex]
		if !has(p.IdentityPairID, p.WorkPairID, p.CorrectionPairID) {
			return "", false
		}
		return p.Email, true
	case oracleContactPrevious:
		p := w.People[plan.oracleIndex]
		if !has(p.IdentityPairID, p.WorkPairID, p.EmailPairID) {
			return "", false
		}
		return p.PreviousEmail, true
	case oracleProjectOutstanding:
		p := w.Projects[plan.oracleIndex]
		if !has(p.ContextPairID, p.LedgerPairID, p.CorrectionPairID) {
			return "", false
		}
		return fmt.Sprintf("%d", p.CorrectedCents-p.PaidCents), true
	case oracleProjectLeadCurrent, oracleProjectLeadPrevious:
		p := w.Projects[plan.oracleIndex]
		lead := w.People[p.Lead]
		if plan.oracleKind == oracleProjectLeadPrevious {
			if !has(p.ContextPairID, lead.WorkPairID, lead.EmailPairID) {
				return "", false
			}
			return lead.PreviousEmail, true
		}
		if !has(p.ContextPairID, lead.IdentityPairID, lead.WorkPairID, lead.CorrectionPairID) {
			return "", false
		}
		return lead.Email, true
	case oracleTripCurrent, oracleTripChangedLegPrevious, oracleTripChangedLegCurrent, oracleTripLongestCurrent:
		trip := w.Trips[plan.oracleIndex]
		if !has(trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID) {
			return "", false
		}
		changed := changedLeg(trip) // identified by the correction row
		legs := trip.OldLegDays     // supplied by the original-plan row
		if plan.oracleKind == oracleTripChangedLegPrevious {
			return fmt.Sprintf("%d", legs[changed]), true
		}
		legs[changed] += trip.LegDays[changed] - trip.OldLegDays[changed]
		if plan.oracleKind == oracleTripChangedLegCurrent {
			return fmt.Sprintf("%d", legs[changed]), true
		}
		if plan.oracleKind == oracleTripCurrent {
			return fmt.Sprintf("%d", sum3(legs)), true
		}
		longest := legs[0]
		for _, days := range legs[1:] {
			if days > longest {
				longest = days
			}
		}
		return fmt.Sprintf("%d", longest), true
	case oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval, oracleStoryLaterNetChange, oracleStoryContactCurrent, oracleStoryLesson, oracleStoryOutcomeSummary:
		arc := w.StoryArcs[plan.oracleIndex]
		if !has(arc.StoryPairIDs[0], arc.StoryPairIDs[1], arc.StoryPairIDs[2]) {
			return "", false
		}
		switch plan.oracleKind {
		case oracleStoryBalanceCurrent:
			return fmt.Sprintf("%d", arc.BaseBudgetCents+arc.BudgetDeltaCents-arc.PaidCents-arc.UnexpectedCostCents+arc.CreditCents), true
		case oracleStoryBudgetDelta:
			return fmt.Sprintf("%d", absInt(arc.BudgetDeltaCents)), true
		case oracleStoryPostApproval:
			return fmt.Sprintf("%d", arc.BaseBudgetCents+arc.BudgetDeltaCents-arc.PaidCents), true
		case oracleStoryLaterNetChange:
			net := arc.BudgetDeltaCents - arc.UnexpectedCostCents + arc.CreditCents
			direction := "increase"
			if net < 0 {
				direction = "decrease"
			}
			return strings.Join([]string{direction, fmt.Sprintf("%d", absInt(net))}, "; "), true
		case oracleStoryContactCurrent:
			return arc.CurrentContact, true
		case oracleStoryOutcomeSummary:
			return strings.Join([]string{arc.CurrentContact, fmt.Sprintf("%d", arc.CurrentBalanceCents), arc.Lesson}, "; "), true
		default:
			return arc.Lesson, true
		}
	default:
		return "", false
	}
}

func (w World) subjectMatches(plan QuestionPlan) int {
	matches := 0
	switch plan.oracleKind {
	case oracleContactCurrent, oracleContactPrevious:
		for _, p := range w.People {
			values := []string{p.Name, strings.Fields(p.Name)[0], p.Nickname, p.Relation, p.Context, p.Employer, p.City}
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
	case oracleProjectOutstanding, oracleProjectLeadCurrent, oracleProjectLeadPrevious:
		want := w.Projects[plan.oracleIndex]
		for _, p := range w.Projects {
			if p.Alias == want.Alias && p.Client == want.Client && p.Purpose == want.Purpose && p.Vendor == want.Vendor {
				matches++
			}
		}
	case oracleTripCurrent, oracleTripChangedLegPrevious, oracleTripChangedLegCurrent, oracleTripLongestCurrent:
		want := w.Trips[plan.oracleIndex]
		for _, trip := range w.Trips {
			if trip.Alias == want.Alias && trip.Purpose == want.Purpose && trip.When == want.When && trip.Countries == want.Countries {
				matches++
			}
		}
	case oracleStoryBalanceCurrent, oracleStoryBudgetDelta, oracleStoryPostApproval, oracleStoryLaterNetChange, oracleStoryContactCurrent, oracleStoryLesson, oracleStoryOutcomeSummary:
		for _, arc := range w.StoryArcs {
			person := w.People[arc.PersonIndex]
			trip := w.Trips[arc.TripIndex]
			values := []string{person.Nickname, person.Relation, person.Context, person.City, trip.Alias, trip.Purpose}
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
	}
	return matches
}

// PairWaves partitions the already-spread memory stream into staged seed calls.
// A correction can therefore arrive much later than the identity or draft it
// supersedes, while timestamps preserve the underlying event chronology.
func (w World) PairWaves(n int) [][]protocol.MemoryPair {
	if n < 1 {
		n = 1
	}
	out := make([][]protocol.MemoryPair, n)
	for i, pair := range w.Pairs {
		wave := i * n / len(w.Pairs)
		if wave >= n {
			wave = n - 1
		}
		out[wave] = append(out[wave], pair)
	}
	return out
}

// UnlockWave is the first wave after which every planted needle is available.
func (w World) UnlockWave(plan QuestionPlan, n int) int {
	if n < 1 {
		n = 1
	}
	positions := make(map[string]int, len(w.Pairs))
	for i, pair := range w.Pairs {
		positions[pair.PairID] = i
	}
	last := 0
	for _, id := range plan.RequiredPairIDs {
		wave := positions[id] * n / len(w.Pairs)
		if wave >= n {
			wave = n - 1
		}
		if wave > last {
			last = wave
		}
	}
	return last
}

func changedLeg(trip Trip) int {
	for i := range trip.LegDays {
		if trip.LegDays[i] != trip.OldLegDays[i] {
			return i
		}
	}
	return 0
}

func questionSeed(seed int64) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v8-world-questions:%d", seed)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

func contentOverlap(question, memory string) float64 {
	q := contentTokens(question)
	if len(q) == 0 {
		return 0
	}
	m := contentTokens(memory)
	hits := 0
	for token := range q {
		if m[token] {
			hits++
		}
	}
	return float64(hits) / float64(len(q))
}

func contentTokens(text string) map[string]bool {
	stop := map[string]bool{
		"what": true, "which": true, "where": true, "when": true, "have": true,
		"with": true, "that": true, "this": true, "from": true, "before": true,
		"after": true, "current": true, "earlier": true, "address": true, "email": true,
		"their": true, "person": true, "contact": true, "update": true, "saved": true,
	}
	out := map[string]bool{}
	for _, token := range strings.FieldsFunc(strings.ToLower(text), func(r rune) bool {
		return (r < 'a' || r > 'z') && (r < '0' || r > '9')
	}) {
		if len(token) >= 4 && !stop[token] {
			out[token] = true
		}
	}
	return out
}

// SortedEvidenceIDs is a public-safe audit helper used by tests and local
// probes. It exposes identities already present in the public dataset, never an
// answer or a hidden provider value.
func (p QuestionPlan) SortedEvidenceIDs() []string {
	out := append([]string(nil), p.RequiredPairIDs...)
	sort.Strings(out)
	return out
}
