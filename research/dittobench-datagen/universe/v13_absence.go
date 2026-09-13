package universe

import (
	"errors"
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 grounded-abstention programs.
//
// An unanswerable plan is proved the same way an answerable one is, with the
// polarity reversed: validateUnanswerablePlan shows that the records an honest
// harness searches (RequiredPairIDs) do not establish the asked fact (the
// oracle resolves to nothing), that every tempting value is genuinely planted in
// those records (so the trap is real, not a pool guess), and that a grounded
// decline has something specific to cite (GroundingTokens present in the
// records and absent from the question). Each unanswerable plan is paired with
// an answerable decision twin drawn from the same family and oracle under a
// different surface, so wording alone cannot tell the two apart.

// AbsenceExpectedAnswer is the validator-only sentinel an unanswerable case
// carries. It never reaches the harness; the grader keys on AnswerAbsence.
const AbsenceExpectedAnswer = "(The asked fact is not established by any record you hold. The correct behavior is a grounded decline that cites what you did find — never a fabricated, superseded, borrowed, or partially derived value.)"

const (
	oracleProbeHandleCurrent  = "probe-handle-current"
	oracleAbsencePure         = "absence-pure"
	oracleAbsenceNearMiss     = "absence-near-miss"
	oracleAbsenceStale        = "absence-stale-removed"
	oracleAbsenceFalsePremise = "absence-false-premise"
	oracleAbsenceCrossUser    = "absence-cross-user"
	oracleAbsenceInsufficient = "absence-insufficient"
)

// V13DecisionPair is one decision_twin pair: an unanswerable plan and the
// answerable plan it is distributionally matched to.
type V13DecisionPair struct {
	Family       string
	Unanswerable QuestionPlan
	Answerable   QuestionPlan
}

// V13CrossUserFact is a contact held only by ANOTHER user's memory graph. The
// caller (which owns the isolation projection) supplies it; Anchor is the
// primary person who shares only a given name with it.
type V13CrossUserFact struct {
	Anchor   int
	Name     string
	Employer string
	Context  string
	Email    string
}

// V13DecisionPairs builds and validates every decision pair for an allocation.
// crossUser carries the other-graph facts for a.CrossPeople in order; a
// shorter slice simply yields fewer cross-user pairs.
func (w World) V13DecisionPairs(a V13Allocation, crossUser []V13CrossUserFact) ([]V13DecisionPair, error) {
	if w.Probes == nil {
		return nil, fmt.Errorf("world was not generated for bench_version >= %d", protocol.BenchVersionV13)
	}
	var out []V13DecisionPair
	add := func(family string, unanswerable, answerable QuestionPlan, err error) error {
		if err != nil {
			return fmt.Errorf("%s twin: %w", family, err)
		}
		unanswerable.Family, answerable.Family = family, family
		if err := w.validatePlan(unanswerable); err != nil {
			return fmt.Errorf("%s unanswerable: %w", family, err)
		}
		if err := w.validatePlan(answerable); err != nil {
			return fmt.Errorf("%s answerable: %w", family, err)
		}
		if answerable.Unanswerable || answerable.Case.AnswerKind == protocol.AnswerAbsence {
			return fmt.Errorf("%s answerable twin is not answerable", family)
		}
		out = append(out, V13DecisionPair{Family: family, Unanswerable: unanswerable, Answerable: answerable})
		return nil
	}

	for k, index := range a.PurePeople {
		twin, err := w.ContactCurrentPlan(index)
		if err := add(V13FamilyPureAbsence, w.pureAbsencePlan(index, k), twin, err); err != nil {
			return nil, err
		}
	}
	for k, probe := range w.Probes.NearMiss {
		twin, err := w.ContactCurrentPlan(probe.Person)
		if err := add(V13FamilyNearMiss, w.nearMissPlan(k), twin, err); err != nil {
			return nil, err
		}
	}
	for k := range a.StalePairs {
		removed, kept := 2*k, 2*k+1
		if kept >= len(w.Probes.Handles) {
			break
		}
		twin, err := w.pickValidated(func(variant int) QuestionPlan { return w.handleCurrentPlan(kept, variant) }, k+1)
		if err := add(V13FamilyStaleRemoved, w.staleHandlePlan(removed, k), twin, err); err != nil {
			return nil, err
		}
	}
	for k, index := range a.FalseTrips {
		twin, err := w.TripChangedLegCurrentPlan(index)
		if err := add(V13FamilyFalsePremise, w.falsePremisePlan(index, k), twin, err); err != nil {
			return nil, err
		}
	}
	for k, fact := range crossUser {
		twin, err := w.ContactCurrentPlan(fact.Anchor)
		if err := add(V13FamilyCrossUser, w.crossUserPlan(fact, k), twin, err); err != nil {
			return nil, err
		}
	}
	for k := range w.Probes.Threads {
		if k >= len(a.InsufficientProjects) {
			break
		}
		twin, err := w.ProjectOutstandingPlan(a.InsufficientProjects[k])
		if err := add(V13FamilyInsufficient, w.insufficientPlan(k), twin, err); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// pickValidated walks a plan's surface variants from a seed-keyed start until
// one passes the v8 plan proof, skipping only the accidental lexical-shortcut
// exclusion (as QuestionPlans does for ordinary surfaces).
func (w World) pickValidated(render func(variant int) QuestionPlan, start int) (QuestionPlan, error) {
	var last error
	for attempt := 0; attempt < 3; attempt++ {
		plan := render(start + attempt)
		if err := w.validatePlan(plan); err != nil {
			last = err
			if errors.Is(err, errLexicalShortcut) {
				continue
			}
			return QuestionPlan{}, err
		}
		return plan, nil
	}
	return QuestionPlan{}, last
}

// absenceCase renders the validator-internal case of an unanswerable plan.
func (w World) absenceCase(kind string, index int, question string, tempting []string) protocol.MemoryCase {
	c := memoryCase(w.Seed, kind, index, question, AbsenceExpectedAnswer, protocol.AnswerAbsence, tempting)
	c.QuestionType = kind
	return c
}

func (w World) pureAbsencePlan(index, ordinal int) QuestionPlan {
	p := w.People[index]
	question := []string{
		fmt.Sprintf("For the %s follow-up, what phone number do I have saved for %s at %s? I'd rather call than email this time.", p.Context, p.Name, p.Employer),
		fmt.Sprintf("What mailing address do I have for %s at %s? I want to send a physical thank-you after the %s.", p.Name, p.Employer, p.Context),
		fmt.Sprintf("When is %s's birthday? They're the one at %s I worked with on the %s, and I want to send something in time.", p.Name, p.Employer, p.Context),
	}[ordinal%3]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsencePure, index, question, []string{p.Email, p.PreviousEmail}),
		RequiredPairIDs: []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID},
		Facts:           []string{"full identity", "event context", "current employer", "recorded contact channels"},
		Constraints:     []string{p.Name, p.Employer, p.Context},
		Operations:      []string{"resolve the person", "enumerate the attributes actually recorded", "decline the unrecorded attribute"},
		Unanswerable:    true, oracleKind: oracleAbsencePure, oracleIndex: index,
	}
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{p.Nickname, p.Relation, p.Role, p.City, p.PreviousEmployer, p.Email, p.PreviousEmail})
	return plan
}

func (w World) nearMissPlan(probeIndex int) QuestionPlan {
	probe := w.Probes.NearMiss[probeIndex]
	p := w.People[probe.Person]
	question := []string{
		fmt.Sprintf("What work email do I have for %s at %s? They're %s's colleague from the %s.", probe.Name, p.Employer, p.Nickname, p.Context),
		fmt.Sprintf("I want to loop in %s, the %s colleague of %s's I met around the %s. Which work email did I save for them?", probe.Name, p.Employer, p.Nickname, p.Context),
		fmt.Sprintf("Remind me of the work address on file for %s — %s's colleague at %s who came up during the %s.", probe.Name, p.Nickname, p.Employer, p.Context),
	}[probeIndex%3]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsenceNearMiss, probeIndex, question, []string{p.Email, p.PreviousEmail}),
		RequiredPairIDs: []string{probe.MentionPairID, p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID},
		Facts:           []string{"colleague mention", "sibling identity", "shared employer", "sibling address"},
		Constraints:     []string{probe.Name, p.Employer, p.Context},
		Operations:      []string{"resolve the named colleague", "separate the sibling's address from the colleague", "decline the unrecorded address"},
		Unanswerable:    true, oracleKind: oracleAbsenceNearMiss, oracleIndex: probeIndex,
	}
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{p.Name, p.Role, p.City, p.Relation, p.PreviousEmployer, p.Email})
	return plan
}

func handleQuestion(p Person, variant int) string {
	return []string{
		fmt.Sprintf("How do I reach %s on Signal? I mean my %s from the %s — what handle did I save?", p.Name, p.Relation, p.Context),
		fmt.Sprintf("What Signal handle do I have for %s, my %s from the %s?", p.Name, p.Relation, p.Context),
		fmt.Sprintf("Pull up the Signal handle for %s — the %s I know from the %s.", p.Name, p.Relation, p.Context),
	}[variant%3]
}

func (w World) staleHandlePlan(probeIndex, ordinal int) QuestionPlan {
	probe := w.Probes.Handles[probeIndex]
	p := w.People[probe.Person]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsenceStale, probeIndex, handleQuestion(p, ordinal), []string{probe.Handle}),
		RequiredPairIDs: []string{p.IdentityPairID, p.WorkPairID, probe.HandlePairID, probe.RemovalPairID},
		Facts:           []string{"full identity", "relationship and event", "withdrawn handle", "removal instruction"},
		Constraints:     []string{p.Name, p.Relation, p.Context},
		Operations:      []string{"resolve the relationship and event to a person", "apply the removal to the earlier handle", "decline the withdrawn value"},
		Unanswerable:    true, oracleKind: oracleAbsenceStale, oracleIndex: probeIndex,
	}
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{p.Nickname, p.Employer, p.Role, p.City, p.Email, p.PreviousEmployer})
	return plan
}

func (w World) handleCurrentPlan(probeIndex, ordinal int) QuestionPlan {
	probe := w.Probes.Handles[probeIndex]
	p := w.People[probe.Person]
	distractors := []string{}
	if probeIndex > 0 {
		distractors = append(distractors, w.Probes.Handles[probeIndex-1].Handle)
	}
	for n := 0; len(distractors) < 3; n++ {
		candidate := persona.CoinShaped(w.Seed, fmt.Sprintf("v13-handle-bait|%d|%d", probeIndex, n))
		if candidate != probe.Handle && !contains(distractors, candidate) {
			distractors = append(distractors, candidate)
		}
	}
	return QuestionPlan{
		Case:            memoryCase(w.Seed, oracleProbeHandleCurrent, probeIndex, handleQuestion(p, ordinal+1), probe.Handle, protocol.AnswerValue, distractors),
		RequiredPairIDs: []string{p.IdentityPairID, p.WorkPairID, probe.HandlePairID},
		Facts:           []string{"full identity", "relationship and event", "work context", "messaging handle"},
		Constraints:     []string{p.Name, p.Relation, p.Context},
		Operations:      []string{"resolve the relationship and event to a person", "select the standing handle"},
		oracleKind:      oracleProbeHandleCurrent, oracleIndex: probeIndex,
	}
}

func (w World) falsePremisePlan(index, ordinal int) QuestionPlan {
	trip := w.Trips[index]
	other := ""
	for offset := 0; offset < len(countryPool) && other == ""; offset++ {
		candidate := countryPool[(ordinal+offset)%len(countryPool)]
		if candidate != trip.Countries[0] && candidate != trip.Countries[1] && candidate != trip.Countries[2] {
			other = candidate
		}
	}
	question := []string{
		fmt.Sprintf("How many days did we spend in %s on %s, our %s trip from %s?", other, trip.Alias, trip.Purpose, trip.When),
		fmt.Sprintf("For %s — the %s trip from %s — how long was the %s stay?", trip.Alias, trip.Purpose, trip.When, strings.TrimPrefix(other, "the ")),
		fmt.Sprintf("Remind me how many days %s, our %s trip from %s, had us in %s.", trip.Alias, trip.Purpose, trip.When, other),
	}[ordinal%3]
	// The original plan states every leg length verbatim; those are the values
	// a lure-following reader borrows for the country that was never visited.
	tempting := []string{}
	for _, days := range trip.OldLegDays {
		if candidate := fmt.Sprintf("%d", days); !contains(tempting, candidate) {
			tempting = append(tempting, candidate)
		}
	}
	companion := w.People[trip.Companion]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsenceFalsePremise, index, question, tempting),
		RequiredPairIDs: []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID},
		Facts:           []string{"trip alias", "purpose and time", "actual countries", "actual leg lengths"},
		Constraints:     []string{trip.Alias, trip.Purpose, trip.When},
		Operations:      []string{"resolve trip alias", "check the claimed country against the itinerary", "decline the leg that never existed"},
		Unanswerable:    true, oracleKind: oracleAbsenceFalsePremise, oracleIndex: index,
	}
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{trip.Countries[0], trip.Countries[1], trip.Countries[2], companion.Nickname})
	return plan
}

func (w World) crossUserPlan(fact V13CrossUserFact, ordinal int) QuestionPlan {
	anchor := w.People[fact.Anchor]
	question := []string{
		fmt.Sprintf("What's the current work email for %s at %s? They handled the %s.", fact.Name, fact.Employer, fact.Context),
		fmt.Sprintf("I need to write to %s about the %s — the one at %s. Which work email do I have for them?", fact.Name, fact.Context, fact.Employer),
		fmt.Sprintf("Which work email did I save for %s, the %s contact from the %s?", fact.Name, fact.Employer, fact.Context),
	}[ordinal%3]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsenceCrossUser, fact.Anchor, question, []string{anchor.Email}),
		RequiredPairIDs: []string{anchor.IdentityPairID, anchor.WorkPairID, anchor.CorrectionPairID},
		Facts:           []string{"given-name sibling identity", "sibling employer and event", "sibling address", "absence of the asked person"},
		Constraints:     []string{fact.Name, fact.Employer, fact.Context},
		Operations:      []string{"search the user's own graph for the person", "separate the given-name sibling", "decline without borrowing another graph"},
		Unanswerable:    true, oracleKind: oracleAbsenceCrossUser, oracleIndex: fact.Anchor,
	}
	plan.Case.ForbiddenAnswer = fact.Email
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{anchor.Name, anchor.Employer, anchor.Nickname, anchor.Role, anchor.City, anchor.Context, anchor.Email})
	return plan
}

func (w World) insufficientPlan(threadIndex int) QuestionPlan {
	thread := w.Probes.Threads[threadIndex]
	question := []string{
		fmt.Sprintf("What do we still owe %s on the %s after our partial payment against %s?", thread.Vendor, thread.Alias, thread.InvoiceID),
		fmt.Sprintf("For the %s: after the partial payment we sent %s on %s, what balance remains?", thread.Alias, thread.Vendor, thread.InvoiceID),
		fmt.Sprintf("Work out the outstanding balance on %s for the %s — %s's invoice — once our partial payment is subtracted.", thread.InvoiceID, thread.Alias, thread.Vendor),
	}[threadIndex%3]
	plan := QuestionPlan{
		Case:            w.absenceCase(oracleAbsenceInsufficient, threadIndex, question, []string{fmt.Sprintf("%d", thread.ApprovedCents)}),
		RequiredPairIDs: []string{thread.ApprovalPairID, thread.PaymentPairID},
		Facts:           []string{"thread alias and vendor", "approved invoice total", "acknowledged partial payment", "missing payment amount"},
		Constraints:     []string{thread.Vendor, thread.Alias, thread.InvoiceID},
		Operations:      []string{"resolve the thread", "identify the missing operand", "decline the underivable balance"},
		Unanswerable:    true, oracleKind: oracleAbsenceInsufficient, oracleIndex: threadIndex,
	}
	plan.Case.GroundingTokens = w.groundingTokens(plan, []string{money(thread.ApprovedCents)})
	return plan
}

// groundingTokens keeps the candidate values that are present in the plan's
// searched records and absent from its question, so citing one proves a read
// rather than an echo of the question.
func (w World) groundingTokens(plan QuestionPlan, candidates []string) []string {
	evidence := w.evidenceBody(plan.RequiredPairIDs)
	var out []string
	for _, candidate := range candidates {
		if candidate == "" || contains(out, candidate) {
			continue
		}
		if grade.Hit(candidate, plan.Case.Question) || !grade.Hit(candidate, evidence) {
			continue
		}
		out = append(out, candidate)
	}
	return out
}

func (w World) evidenceBody(pairIDs []string) string {
	required := make(map[string]bool, len(pairIDs))
	for _, id := range pairIDs {
		required[id] = true
	}
	var b strings.Builder
	for _, pair := range w.Pairs {
		if required[pair.PairID] {
			b.WriteString(pair.Prompt)
			b.WriteString(" ")
			b.WriteString(pair.Response)
			b.WriteString(" ")
		}
	}
	return b.String()
}

// validateUnanswerablePlan is the absence proof. It mirrors validatePlan's
// structure but proves the reverse polarity: the oracle must resolve to
// NOTHING over the full evidence, every tempting value must really be planted
// in the searched records, and the grounding a correct decline cites must exist
// there and not in the question.
func (w World) validateUnanswerablePlan(plan QuestionPlan) error {
	if plan.Case.AnswerKind != protocol.AnswerAbsence || plan.Case.ExpectedAnswer != AbsenceExpectedAnswer {
		return fmt.Errorf("unanswerable plan must grade as %s with the absence sentinel", protocol.AnswerAbsence)
	}
	if len(plan.Facts) < 3 || len(plan.Constraints) < 3 || len(plan.Operations) < 2 {
		return fmt.Errorf("under-specified absence plan: facts=%d constraints=%d operations=%d", len(plan.Facts), len(plan.Constraints), len(plan.Operations))
	}
	if got := renderedConstraintCount(plan); got < 3 {
		return fmt.Errorf("rendered question contains %d declared constraints, want at least 3", got)
	}
	if got := w.subjectMatches(plan); got != 1 {
		return fmt.Errorf("constraints resolve %d subjects, want exactly one", got)
	}
	if len(plan.RequiredPairIDs) < 2 {
		return fmt.Errorf("absence plan searches %d records, want at least 2", len(plan.RequiredPairIDs))
	}
	available := make(map[string]bool, len(plan.RequiredPairIDs))
	pairIDs := make(map[string]bool, len(w.Pairs))
	for _, pair := range w.Pairs {
		pairIDs[pair.PairID] = true
	}
	for _, id := range plan.RequiredPairIDs {
		if available[id] {
			return fmt.Errorf("duplicate evidence pair %s", id)
		}
		if !pairIDs[id] {
			return fmt.Errorf("missing planted evidence pair %s", id)
		}
		available[id] = true
	}
	if got, ok := w.resolveWithEvidence(plan, available); ok {
		return fmt.Errorf("absence plan resolves to %q over its own evidence", got)
	}
	evidence := w.evidenceBody(plan.RequiredPairIDs)
	if len(plan.Case.DistractorAnswers) < 1 {
		return fmt.Errorf("absence plan plants no tempting value")
	}
	seen := map[string]bool{}
	for _, value := range plan.Case.DistractorAnswers {
		if value == "" || value == AbsenceExpectedAnswer || seen[value] {
			return fmt.Errorf("invalid tempting value %q", value)
		}
		seen[value] = true
		if grade.Hit(value, plan.Case.Question) {
			return fmt.Errorf("question leaks tempting value %q", value)
		}
		if !valuePlanted(value, evidence) {
			return fmt.Errorf("tempting value %q is not planted in the searched records", value)
		}
	}
	if forbidden := plan.Case.ForbiddenAnswer; forbidden != "" {
		if grade.Hit(forbidden, evidence) || grade.Hit(forbidden, plan.Case.Question) {
			return fmt.Errorf("forbidden cross-graph value %q leaks into this graph or the question", forbidden)
		}
	}
	if len(plan.Case.GroundingTokens) < 1 {
		return fmt.Errorf("absence plan has no grounding token a decline could cite")
	}
	for _, token := range plan.Case.GroundingTokens {
		if grade.Hit(token, plan.Case.Question) {
			return fmt.Errorf("grounding token %q is a question echo", token)
		}
		if !grade.Hit(token, evidence) {
			return fmt.Errorf("grounding token %q is absent from the searched records", token)
		}
	}
	return nil
}

// valuePlanted reports whether a tempting value is present in the evidence
// text. Minor-unit money values are rendered as currency in prose, so they are
// matched through the same formatting the records use.
func valuePlanted(value, evidence string) bool {
	if grade.Hit(value, evidence) {
		return true
	}
	cents := 0
	for _, r := range value {
		if r < '0' || r > '9' {
			return false
		}
		cents = cents*10 + int(r-'0')
	}
	return value != "" && strings.Contains(evidence, money(cents))
}
