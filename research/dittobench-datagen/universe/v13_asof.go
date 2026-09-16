package universe

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 same-turn point-in-time ("as of") programs.
//
// Every v8 correction chain (a person's address, a project's invoice figure, a
// trip's leg length) is two records: the original and the correction, with
// timestamps the harness ingests. An as-of question supplies an ANCHOR DATE
// inside the /run user_input and asks for the state in force on that date. The
// anchor is same-turn context: it exists nowhere in the seeded records, so an
// index compiled at ingest time (which only knows the current state) cannot
// answer it. Each chain yields an as_of_twin pair: the "before" half anchors
// strictly between the two records (answer: the superseded value, and the
// current value is the planted distractor), the "after" half anchors after the
// correction (answer: the current value). A current-state index answers exactly
// one half; the scorer's relation post-pass reads TwinRelation to grade the
// pair. Every plan goes through validatePlan like any v8 program.

const (
	oracleAsOfContact = "as-of-contact"
	oracleAsOfInvoice = "as-of-invoice"
	oracleAsOfTripLeg = "as-of-trip-leg"

	asOfDateLayout = "January 2, 2006"

	asOfBefore = "before"
	asOfAfter  = "after"
)

// asOfCase renders one half of an as-of pair under its own case identity; the
// two halves share an oracle kind and subject index but never a case id.
func asOfCase(seed int64, kind, half string, index int, question, answer, answerKind string, distractors []string) protocol.MemoryCase {
	c := memoryCase(seed, kind+"-"+half, index, question, answer, answerKind, distractors)
	c.QuestionType = "world-" + kind
	return c
}

// V13AsOfPair is one as_of_twin pair over a single correction chain.
type V13AsOfPair struct {
	Kind   string
	Index  int
	Before QuestionPlan
	After  QuestionPlan
}

// V13AsOfPairs builds and validates the as-of pairs for an allocation.
func (w World) V13AsOfPairs(a V13Allocation) ([]V13AsOfPair, error) {
	var out []V13AsOfPair
	for k, index := range a.AsOfPeople {
		pair, err := w.asOfContactPair(index, k)
		if err != nil {
			return nil, fmt.Errorf("as-of contact %d: %w", index, err)
		}
		out = append(out, pair)
	}
	for k, index := range a.AsOfProjects {
		pair, err := w.asOfInvoicePair(index, k)
		if err != nil {
			return nil, fmt.Errorf("as-of invoice %d: %w", index, err)
		}
		out = append(out, pair)
	}
	for k, index := range a.AsOfTrips {
		pair, err := w.asOfTripLegPair(index, k)
		if err != nil {
			return nil, fmt.Errorf("as-of trip leg %d: %w", index, err)
		}
		out = append(out, pair)
	}
	return out, nil
}

// pairTimestamp returns the RFC3339 instant of one seeded record.
func (w World) pairTimestamp(pairID string) (time.Time, bool) {
	for _, pair := range w.Pairs {
		if pair.PairID == pairID {
			ts, err := time.Parse(time.RFC3339, pair.Timestamp)
			return ts, err == nil
		}
	}
	return time.Time{}, false
}

// asOfAnchors derives the two anchor instants of a chain: noon on a calendar
// date strictly between the two records, and noon on a date strictly after the
// correction. Both are rendered as dates, so a harness comparing the printed
// date against record timestamps sees no ambiguity.
func (w World) asOfAnchors(oldPairID, newPairID string) (before, after time.Time, err error) {
	oldTS, ok1 := w.pairTimestamp(oldPairID)
	newTS, ok2 := w.pairTimestamp(newPairID)
	if !ok1 || !ok2 {
		return before, after, fmt.Errorf("chain records %s/%s have no timestamps", oldPairID, newPairID)
	}
	if !oldTS.Before(newTS) {
		return before, after, fmt.Errorf("chain records %s/%s are not in chronological order", oldPairID, newPairID)
	}
	mid := oldTS.Add(newTS.Sub(oldTS) / 2)
	before = time.Date(mid.Year(), mid.Month(), mid.Day(), 12, 0, 0, 0, time.UTC)
	oldDay := time.Date(oldTS.Year(), oldTS.Month(), oldTS.Day(), 0, 0, 0, 0, time.UTC)
	newDay := time.Date(newTS.Year(), newTS.Month(), newTS.Day(), 0, 0, 0, 0, time.UTC)
	beforeDay := before.Truncate(24 * time.Hour)
	if !beforeDay.After(oldDay) || !beforeDay.Before(newDay) {
		return before, after, fmt.Errorf("chain records %s/%s are too close for a date-granular anchor", oldPairID, newPairID)
	}
	after = time.Date(newTS.Year(), newTS.Month(), newTS.Day(), 12, 0, 0, 0, time.UTC).Add(3 * 24 * time.Hour)
	return before, after, nil
}

// chainStateAt reports which record of a two-record chain was in force at the
// anchor: "before" (the original), "after" (the correction), or "" when the
// anchor precedes the chain entirely.
func (w World) chainStateAt(anchor time.Time, oldPairID, newPairID string) string {
	oldTS, ok1 := w.pairTimestamp(oldPairID)
	newTS, ok2 := w.pairTimestamp(newPairID)
	if !ok1 || !ok2 || anchor.IsZero() {
		return ""
	}
	switch {
	case anchor.After(newTS):
		return "after"
	case anchor.After(oldTS):
		return "before"
	default:
		return ""
	}
}

func (w World) asOfContactPair(index, ordinal int) (V13AsOfPair, error) {
	p := w.People[index]
	before, after, err := w.asOfAnchors(p.EmailPairID, p.CorrectionPairID)
	if err != nil {
		return V13AsOfPair{}, err
	}
	build := func(half string, anchor time.Time, answer, other string, variant int) QuestionPlan {
		date := anchor.Format(asOfDateLayout)
		question := []string{
			fmt.Sprintf("As of %s, what work email did I have on file for %s, my %s who handled the %s? I'm reconstructing the paper trail for that date, so use what the records showed then.", date, p.Name, p.Relation, p.Context),
			fmt.Sprintf("Checking my notes as they stood on %s: which work email was saved for %s, my %s from the %s, on that date?", date, p.Name, p.Relation, p.Context),
			fmt.Sprintf("I need the address that was current on %s for %s — my %s from the %s. Which work email did my records hold that day?", date, p.Name, p.Relation, p.Context),
		}[variant%3]
		return QuestionPlan{
			Case:            asOfCase(w.Seed, oracleAsOfContact, half, index, question, answer, protocol.AnswerValue, w.emailDistractors(index, other)),
			RequiredPairIDs: []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID},
			Facts:           []string{"full identity", "relationship and event", "original address", "address correction", "record dates"},
			Constraints:     []string{p.Name, p.Relation, p.Context},
			Operations:      []string{"resolve the relationship and event to a person", "order the address records by time", "select the address in force on the anchor date"},
			oracleKind:      oracleAsOfContact, oracleIndex: index, asOfAnchor: anchor,
		}
	}
	return w.buildAsOfPair(oracleAsOfContact, index, ordinal,
		func(variant int) QuestionPlan { return build(asOfBefore, before, p.PreviousEmail, p.Email, variant) },
		func(variant int) QuestionPlan { return build(asOfAfter, after, p.Email, p.PreviousEmail, variant) })
}

func (w World) asOfInvoicePair(index, ordinal int) (V13AsOfPair, error) {
	p := w.Projects[index]
	before, after, err := w.asOfAnchors(p.LedgerPairID, p.CorrectionPairID)
	if err != nil {
		return V13AsOfPair{}, err
	}
	build := func(half string, anchor time.Time, answer, other int, variant int) QuestionPlan {
		date := anchor.Format(asOfDateLayout)
		question := []string{
			fmt.Sprintf("As of %s, what invoice total was on the AP record for %q, the %s work for %s? I need the figure as it stood that day, not today's.", date, p.Alias, p.Purpose, p.Client),
			fmt.Sprintf("Looking at the books as of %s, what invoice total did we have on file for %q — %s's %s project?", date, p.Alias, p.Client, p.Purpose),
			fmt.Sprintf("For an audit dated %s: what was the invoice total our records showed for %q, the %s engagement for %s, on that date?", date, p.Alias, p.Purpose, p.Client),
		}[variant%3]
		distractors := []string{fmt.Sprintf("%d", other)}
		for _, candidate := range w.moneyDistractors(index, answer) {
			if len(distractors) == 3 {
				break
			}
			if !contains(distractors, candidate) {
				distractors = append(distractors, candidate)
			}
		}
		return QuestionPlan{
			Case:            asOfCase(w.Seed, oracleAsOfInvoice, half, index, question, fmt.Sprintf("%d", answer), protocol.AnswerMoney, distractors),
			RequiredPairIDs: []string{p.ContextPairID, p.LedgerPairID, p.CorrectionPairID},
			Facts:           []string{"project alias", "client and purpose", "original invoice figure", "approved correction", "record dates"},
			Constraints:     []string{p.Alias, p.Client, p.Purpose},
			Operations:      []string{"resolve project alias", "order the invoice records by time", "select the figure in force on the anchor date"},
			oracleKind:      oracleAsOfInvoice, oracleIndex: index, asOfAnchor: anchor,
		}
	}
	return w.buildAsOfPair(oracleAsOfInvoice, index, ordinal,
		func(variant int) QuestionPlan {
			return build(asOfBefore, before, p.OriginalCents, p.CorrectedCents, variant)
		},
		func(variant int) QuestionPlan {
			return build(asOfAfter, after, p.CorrectedCents, p.OriginalCents, variant)
		})
}

func (w World) asOfTripLegPair(index, ordinal int) (V13AsOfPair, error) {
	trip := w.Trips[index]
	changed := changedLeg(trip)
	before, after, err := w.asOfAnchors(trip.PlanPairID, trip.CorrectionPairID)
	if err != nil {
		return V13AsOfPair{}, err
	}
	build := func(half string, anchor time.Time, answer, other int, variant int) QuestionPlan {
		date := anchor.Format(asOfDateLayout)
		country := trip.Countries[changed]
		question := []string{
			fmt.Sprintf("As of %s, how many days did our plan have us in %s on %s, the %s trip from %s?", date, country, trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("On %s, how many days were we planning to spend in %s for %s — our %s trip from %s?", date, country, trip.Alias, trip.Purpose, trip.When),
			fmt.Sprintf("Take the itinerary as it stood on %s: how many days in %s did %s, the %s trip from %s, have at that point?", date, country, trip.Alias, trip.Purpose, trip.When),
		}[variant%3]
		distractors := []string{fmt.Sprintf("%d", other)}
		for _, candidate := range w.tripDistractors(index, answer) {
			if len(distractors) == 3 {
				break
			}
			if !contains(distractors, candidate) {
				distractors = append(distractors, candidate)
			}
		}
		return QuestionPlan{
			Case:            asOfCase(w.Seed, oracleAsOfTripLeg, half, index, question, fmt.Sprintf("%d", answer), protocol.AnswerNumber, distractors),
			RequiredPairIDs: []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID},
			Facts:           []string{"trip alias", "purpose and time", "original leg plan", "leg correction", "record dates"},
			Constraints:     []string{trip.Alias, trip.Purpose, trip.When},
			Operations:      []string{"resolve trip alias", "order the itinerary records by time", "select the leg length in force on the anchor date"},
			oracleKind:      oracleAsOfTripLeg, oracleIndex: index, asOfAnchor: anchor,
		}
	}
	return w.buildAsOfPair(oracleAsOfTripLeg, index, ordinal,
		func(variant int) QuestionPlan {
			return build(asOfBefore, before, trip.OldLegDays[changed], trip.LegDays[changed], variant)
		},
		func(variant int) QuestionPlan {
			return build(asOfAfter, after, trip.LegDays[changed], trip.OldLegDays[changed], variant)
		})
}

// buildAsOfPair renders both halves, walking the surface variants from the
// seed-keyed start until each half passes the v8 plan proof. A variant can
// trip the accidental lexical-shortcut exclusion exactly as an ordinary world
// surface can; structural failures still fail generation.
func (w World) buildAsOfPair(kind string, index, ordinal int, before, after func(variant int) QuestionPlan) (V13AsOfPair, error) {
	pick := func(render func(variant int) QuestionPlan, start int) (QuestionPlan, error) {
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
	beforePlan, err := pick(before, ordinal)
	if err != nil {
		return V13AsOfPair{}, err
	}
	afterPlan, err := pick(after, ordinal+1)
	if err != nil {
		return V13AsOfPair{}, err
	}
	pair := V13AsOfPair{Kind: kind, Index: index, Before: beforePlan, After: afterPlan}
	return pair, w.validateAsOfPair(pair)
}

// validateAsOfPair runs the ordinary v8 plan proof on both halves and then the
// pair invariant: the two halves ask the same chain, the before-half answer is
// the superseded value (it differs from the current value), and each half's
// answer is the other half's planted distractor.
func (w World) validateAsOfPair(pair V13AsOfPair) error {
	for _, half := range []QuestionPlan{pair.Before, pair.After} {
		if err := w.validatePlan(half); err != nil {
			return err
		}
	}
	if pair.Before.Case.ExpectedAnswer == pair.After.Case.ExpectedAnswer {
		return fmt.Errorf("as-of pair %s/%d: the superseded and current values coincide", pair.Kind, pair.Index)
	}
	if !contains(pair.Before.Case.DistractorAnswers, pair.After.Case.ExpectedAnswer) ||
		!contains(pair.After.Case.DistractorAnswers, pair.Before.Case.ExpectedAnswer) {
		return fmt.Errorf("as-of pair %s/%d: halves do not plant each other's answer as a distractor", pair.Kind, pair.Index)
	}
	if !strings.Contains(pair.Before.Case.Question, pair.Before.asOfAnchor.Format(asOfDateLayout)) ||
		!strings.Contains(pair.After.Case.Question, pair.After.asOfAnchor.Format(asOfDateLayout)) {
		return fmt.Errorf("as-of pair %s/%d: a half does not render its anchor date", pair.Kind, pair.Index)
	}
	return nil
}

// AsOfAnchor exposes the same-turn anchor of an as-of plan (zero otherwise).
func (p QuestionPlan) AsOfAnchor() time.Time { return p.asOfAnchor }

// OracleKind exposes the plan's oracle family for tests and audits.

// OracleIndex exposes the plan's oracle subject index for tests and audits.
func (p QuestionPlan) OracleIndex() int { return p.oracleIndex }

// v13OracleEvidence, v13OracleOperations, v13ResolveWithEvidence, and
// v13SubjectMatches are the validatePlan delegates for every v13 oracle kind.
// They are reached only from the default branches of the v8 switches, so no
// v8 kind changes behavior.
func (w World) v13OracleEvidence(plan QuestionPlan) []string {
	switch plan.oracleKind {
	case oracleAsOfContact:
		p := w.People[plan.oracleIndex]
		return []string{p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID}
	case oracleAsOfInvoice:
		p := w.Projects[plan.oracleIndex]
		return []string{p.ContextPairID, p.LedgerPairID, p.CorrectionPairID}
	case oracleAsOfTripLeg:
		trip := w.Trips[plan.oracleIndex]
		return []string{trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID}
	case oracleProbeHandleCurrent:
		probe, ok := w.handleProbe(plan.oracleIndex)
		if !ok {
			return nil
		}
		p := w.People[probe.Person]
		return []string{p.IdentityPairID, p.WorkPairID, probe.HandlePairID}
	default:
		return nil
	}
}

func (w World) v13OracleOperations(plan QuestionPlan) []string {
	switch plan.oracleKind {
	case oracleAsOfContact:
		return []string{"resolve the relationship and event to a person", "order the address records by time", "select the address in force on the anchor date"}
	case oracleAsOfInvoice:
		return []string{"resolve project alias", "order the invoice records by time", "select the figure in force on the anchor date"}
	case oracleAsOfTripLeg:
		return []string{"resolve trip alias", "order the itinerary records by time", "select the leg length in force on the anchor date"}
	case oracleProbeHandleCurrent:
		return []string{"resolve the relationship and event to a person", "select the standing handle"}
	default:
		return nil
	}
}

func (w World) v13ResolveWithEvidence(plan QuestionPlan, available map[string]bool) (string, bool) {
	has := func(ids ...string) bool {
		for _, id := range ids {
			if !available[id] {
				return false
			}
		}
		return true
	}
	switch plan.oracleKind {
	case oracleAsOfContact:
		p := w.People[plan.oracleIndex]
		if !has(p.IdentityPairID, p.WorkPairID, p.EmailPairID, p.CorrectionPairID) {
			return "", false
		}
		switch w.chainStateAt(plan.asOfAnchor, p.EmailPairID, p.CorrectionPairID) {
		case "before":
			return p.PreviousEmail, true
		case "after":
			return p.Email, true
		}
		return "", false
	case oracleAsOfInvoice:
		p := w.Projects[plan.oracleIndex]
		if !has(p.ContextPairID, p.LedgerPairID, p.CorrectionPairID) {
			return "", false
		}
		switch w.chainStateAt(plan.asOfAnchor, p.LedgerPairID, p.CorrectionPairID) {
		case "before":
			return fmt.Sprintf("%d", p.OriginalCents), true
		case "after":
			return fmt.Sprintf("%d", p.CorrectedCents), true
		}
		return "", false
	case oracleAsOfTripLeg:
		trip := w.Trips[plan.oracleIndex]
		if !has(trip.ContextPairID, trip.PlanPairID, trip.CorrectionPairID) {
			return "", false
		}
		changed := changedLeg(trip)
		switch w.chainStateAt(plan.asOfAnchor, trip.PlanPairID, trip.CorrectionPairID) {
		case "before":
			return fmt.Sprintf("%d", trip.OldLegDays[changed]), true
		case "after":
			return fmt.Sprintf("%d", trip.LegDays[changed]), true
		}
		return "", false
	case oracleProbeHandleCurrent:
		probe, ok := w.handleProbe(plan.oracleIndex)
		if !ok || probe.Removed {
			return "", false
		}
		p := w.People[probe.Person]
		if !has(p.IdentityPairID, p.WorkPairID, probe.HandlePairID) {
			return "", false
		}
		return probe.Handle, true
	default:
		// Every unanswerable kind deliberately resolves to nothing: the absence
		// is the oracle result validateUnanswerablePlan proves.
		return "", false
	}
}

func (w World) v13SubjectMatches(plan QuestionPlan) int {
	switch plan.oracleKind {
	case oracleAsOfContact, oracleProbeHandleCurrent, oracleAbsenceStale:
		return w.personSubjectMatches(plan.Constraints)
	case oracleAbsencePure:
		// The coined subject matches nobody; the employer and event it is
		// attached to must resolve to exactly the contact whose address tempts.
		anchor := w.People[plan.oracleIndex]
		matches := 0
		for _, p := range w.People {
			if p.Employer == anchor.Employer && p.Context == anchor.Context {
				matches++
			}
		}
		return matches
	case oracleAsOfInvoice:
		return w.projectSubjectMatches(plan.oracleIndex)
	case oracleAsOfTripLeg, oracleAbsenceFalsePremise:
		return w.tripSubjectMatches(plan.oracleIndex)
	case oracleAbsenceNearMiss:
		// The coined colleague matches nobody; the employer and event they are
		// attached to must resolve to exactly the sibling whose address tempts.
		probe := w.Probes.NearMiss[plan.oracleIndex]
		sibling := w.People[probe.Person]
		matches := 0
		for _, p := range w.People {
			if p.Employer == sibling.Employer && p.Context == sibling.Context {
				matches++
			}
		}
		return matches
	case oracleAbsenceCrossUser:
		// The other graph's person shares only a given name with one primary
		// contact; that given name must be unique so the near-miss is exact.
		anchor := w.People[plan.oracleIndex]
		given := strings.Fields(anchor.Name)[0]
		matches := 0
		for _, p := range w.People {
			if strings.Fields(p.Name)[0] == given {
				matches++
			}
		}
		return matches
	case oracleAbsenceInsufficient:
		thread := w.Probes.Threads[plan.oracleIndex]
		matches := 0
		for _, other := range w.Probes.Threads {
			if other.Alias == thread.Alias {
				matches++
			}
		}
		return matches
	default:
		return 0
	}
}

func (w World) personSubjectMatches(constraints []string) int {
	matches := 0
	for _, p := range w.People {
		values := []string{p.Name, strings.Fields(p.Name)[0], p.Nickname, p.Relation, p.Context, p.Employer, p.City}
		matched := true
		for _, constraint := range constraints {
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

func (w World) projectSubjectMatches(index int) int {
	want := w.Projects[index]
	matches := 0
	for _, p := range w.Projects {
		if p.Alias == want.Alias && p.Client == want.Client && p.Purpose == want.Purpose && p.Vendor == want.Vendor {
			matches++
		}
	}
	return matches
}

func (w World) tripSubjectMatches(index int) int {
	want := w.Trips[index]
	matches := 0
	for _, trip := range w.Trips {
		if trip.Alias == want.Alias && trip.Purpose == want.Purpose && trip.When == want.When && trip.Countries == want.Countries {
			matches++
		}
	}
	return matches
}

func (w World) handleProbe(index int) (V13HandleProbe, bool) {
	if w.Probes == nil || index < 0 || index >= len(w.Probes.Handles) {
		return V13HandleProbe{}, false
	}
	return w.Probes.Handles[index], true
}
