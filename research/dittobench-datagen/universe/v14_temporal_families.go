package universe

import (
	"fmt"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v14 temporal business families (SN-355, SN-358).
//
// Every v13 business family asks for the state in force now, over a fixed
// three-record history, so "read the latest record" answers it. Generator
// competition rounds showed the opposite shapes discriminate: a date placed
// between two dated changes, and a long sequence of events some of which are
// dated only relative to another (contributed by a generator-competition miner,
// generator C, rounds 2594x-2596x; a single-hop date lookup was trivial in
// every round, generator G). V14 renders two such shapes in place of v13's
// latest-of-three and current-value channel families:
//
//   - event sequence (SN-358, replaces latest-event): five to eight dated
//     events, one or two of them dated only as "N days after/before" an
//     absolutely dated event. The question asks for the nth event in date
//     order, or the event immediately before or after a named one. The answer
//     is never the first or last event by date and its record is never the
//     first or last filed, so neither "latest" nor "record 0" answers it. The
//     counterfactual moves the answer event's date across its neighbour, so
//     the neighbour answers; it changes exactly one record.
//   - channel as of a date (SN-355, replaces current-channel): four dated
//     channel changes, and a question date strictly inside an interval
//     that is neither the first nor the last. The latest value and the first
//     value are both wrong. The counterfactual moves one change across the
//     question date, so a neighbouring value answers, again never the first
//     or the last.
//
// The full profile renders only the first four business groups, so v13's
// families 4-6 never reach a scored run. V14 therefore draws, per seed, which
// of the three temporal families (latest-event, next-action-responsible,
// current-channel) fills cycle position 2; the other two take positions 4 and
// 5. Every v13 seed is untouched: nothing here runs below v14.

// v14TemporalFamilies are the families that share the v14 temporal slot.
var v14TemporalFamilies = []V13Family{V13FamilyLatestEvent, V13FamilyNextActionResponsible, V13FamilyCurrentChannel}

// V14TemporalFamily maps a v13 cycle family to the family the v14 temporal
// slot draws for seed. Families outside the slot map to themselves.
func V14TemporalFamily(seed int64, family V13Family) V13Family {
	for i, f := range v14TemporalFamilies {
		if f == family {
			return v14TemporalFamilies[v13Perm(seed, "v14-temporal-slot", len(v14TemporalFamilies))[i]]
		}
	}
	return family
}

// V14FamilyOf returns the family a generated v14 business case belongs to.
// Provenance-only: never on the wire.
func V14FamilyOf(seed int64, caseIndex int) V13Family {
	return V14TemporalFamily(seed, V13FamilyOf(caseIndex))
}

// V14SequenceEventClasses is the v14 event bank. It is a new contract's own
// bank, sized to the pool floor (#492) rather than extending the seven-class
// v13 bank: a sequence of up to eight events plus a decoy draws nine distinct
// classes from it. Accept clusters are disjoint: no term of one class is a
// bounded substring of a term of another.
var V14SequenceEventClasses = []V13TermClass{
	{Canonical: "kickoff", Accept: []string{"kickoff", "kick-off", "kickoff meeting"}},
	{Canonical: "vendor swap", Accept: []string{"vendor swap", "switched vendors", "supplier change"}},
	{Canonical: "incident", Accept: []string{"incident", "outage", "service disruption"}},
	{Canonical: "handoff", Accept: []string{"handoff", "hand-off", "handover"}},
	{Canonical: "audit", Accept: []string{"audit", "audit visit", "compliance check"}},
	{Canonical: "quote revision", Accept: []string{"quote revision", "revised quote", "requote"}},
	{Canonical: "reference call", Accept: []string{"reference call", "reference check"}},
	{Canonical: "site visit", Accept: []string{"site visit", "site walk", "on-site visit"}},
	{Canonical: "training session", Accept: []string{"training session", "training day"}},
	{Canonical: "board review", Accept: []string{"board review", "board check-in"}},
	{Canonical: "contract renewal", Accept: []string{"contract renewal", "renewed the contract", "contract extension"}},
	{Canonical: "budget approval", Accept: []string{"budget approval", "budget sign-off", "approved the budget"}},
	{Canonical: "security review", Accept: []string{"security review", "security assessment"}},
	{Canonical: "pricing workshop", Accept: []string{"pricing workshop", "pricing session"}},
	{Canonical: "launch rehearsal", Accept: []string{"launch rehearsal", "dress rehearsal"}},
	{Canonical: "data migration", Accept: []string{"data migration", "migrated the data"}},
	{Canonical: "steering meeting", Accept: []string{"steering meeting", "steering committee"}},
	{Canonical: "stakeholder demo", Accept: []string{"stakeholder demo", "demo to stakeholders"}},
	{Canonical: "legal review", Accept: []string{"legal review", "legal sign-off"}},
	{Canonical: "design sprint", Accept: []string{"design sprint"}},
	{Canonical: "customer escalation", Accept: []string{"customer escalation", "escalation from the customer"}},
	{Canonical: "roadmap offsite", Accept: []string{"roadmap offsite", "planning offsite"}},
	{Canonical: "pilot go-live", Accept: []string{"pilot go-live", "pilot launch", "went live with the pilot"}},
	{Canonical: "invoice dispute", Accept: []string{"invoice dispute", "billing dispute"}},
}

// v14TemporalDraw holds the draws of the v14 temporal families.
type v14TemporalDraw struct {
	Seq  *v14Sequence
	AsOf *v14AsOf
}

// Event-sequence query kinds.
const (
	v14SeqNth    = "nth"
	v14SeqBefore = "before"
	v14SeqAfter  = "after"
)

// v14Sequence is one event-sequence draw. Event i is the ith event in base
// date order.
type v14Sequence struct {
	Classes []int       // event class per event (V14SequenceEventClasses)
	Days    []time.Time // base date per event, strictly increasing
	Decoy   int         // decoy workstream's event class
	Query   string      // v14SeqNth, v14SeqBefore or v14SeqAfter
	Answer  int         // base answer event
	Partner int         // event that answers once the counterfactual moves Answer across it
	// CounterDay is the counterfactual date of Answer: just past Partner, on
	// the side away from Answer, and strictly between two base dates.
	CounterDay time.Time
	Named      int // event the question names (before/after); -1 for nth
	// Relative maps a relatively dated event to its absolutely dated anchor.
	Relative map[int]int
	// Log is the story (filing) order of the state records: event indexes,
	// with len(Classes) standing for the neutral milestone record.
	Log []int
}

// v14AsOf is one channel-as-of draw.
type v14AsOf struct {
	Values []int       // channel class per change, in date order
	Decoy  int         // decoy workstream's channel class
	Days   []time.Time // date each value took effect, strictly increasing
	K      int         // question interval: Days[K] < Query < Days[K+1]
	Query  time.Time
	// The counterfactual moves one change across the question date: change K
	// to just after it (answer K-1), or change K+1 to just before it (answer
	// K+1). Whichever is chosen keeps the answer off the first and last value.
	CounterChange int
	CounterDay    time.Time
	CounterAnswer int
	NeutralAt     int // story position of the neutral record among the changes
}

// drawV14Temporal draws the v14 extension of a temporal-family group.
func (d *v13Draws) drawV14Temporal(g *v13Group, group int) {
	switch g.Family {
	case V13FamilyLatestEvent:
		g.T14 = &v14TemporalDraw{Seq: d.drawV14Sequence(group)}
	case V13FamilyCurrentChannel:
		g.T14 = &v14TemporalDraw{AsOf: d.drawV14AsOf(group)}
	}
}

// v14PastAnchor is the calendar day of a group's first case-file record. Every
// date a temporal record reports falls before it, so no record is filed
// before an event it describes.
func v14PastAnchor(seed int64, domain string, group int) time.Time {
	return protocol.OpaqueBusinessInstant(seed, v13CalendarKey(domain, group)).Truncate(24 * time.Hour)
}

func v14Days(a, b time.Time) int { return int(b.Sub(a).Hours() / 24) }

// v14DayProse renders a calendar day with its year, in one of the unambiguous
// V13DateAccept forms. Yearless forms are avoided: a sequence can straddle a
// year boundary.
func v14DayProse(seed int64, salt string, at time.Time) string {
	forms := V13DateAccept(at.Year(), int(at.Month()), at.Day())
	return v13Pick(seed, "v14-day-form-"+salt, []string{forms[0], forms[2], forms[4], forms[6]})
}

func (d *v13Draws) drawV14Sequence(group int) *v14Sequence {
	seed := d.seed
	key := func(name string) string { return fmt.Sprintf("v14-seq-%s-%d", name, group) }
	n := 5 + v13Index(seed, key("n"), 4)
	s := &v14Sequence{Named: -1, Relative: map[int]int{}}
	classes := v13Perm(seed, key("classes"), len(V14SequenceEventClasses))
	s.Classes = append([]int(nil), classes[:n]...)
	s.Decoy = classes[n]
	day := v14PastAnchor(seed, "business", group).AddDate(0, 0, -(120 + v13Index(seed, key("start"), 200)))
	for i := 0; i < n; i++ {
		if i > 0 {
			day = day.AddDate(0, 0, 2+v13Index(seed, fmt.Sprintf("v14-seq-gap-%d-%d", group, i), 5))
		}
		s.Days = append(s.Days, day)
	}
	switch v13Index(seed, key("query"), 3) {
	case 0:
		// The kth event, never the first or the last; the partner follows it,
		// so once the answer moves past it the partner sits at position k.
		s.Query = v14SeqNth
		s.Answer = 1 + v13Index(seed, key("k"), n-2)
		s.Partner = s.Answer + 1
	case 1:
		// Immediately after a named event: the answer and its partner are
		// the next two events, so the answer is never the last.
		s.Query = v14SeqAfter
		s.Named = v13Index(seed, key("named"), n-2)
		s.Answer, s.Partner = s.Named+1, s.Named+2
	default:
		// Immediately before a named event: the answer and its partner are
		// the previous two events, so the answer is never the first.
		s.Query = v14SeqBefore
		s.Named = 2 + v13Index(seed, key("named"), n-2)
		s.Answer, s.Partner = s.Named-1, s.Named-2
	}
	// One relatively dated event (two from seven events), each anchored to its
	// nearest absolutely dated event within four weeks. Neither the relative
	// event nor its anchor is the answer or the partner, so the counterfactual
	// moves one date and no relative date with it.
	swapped := func(i int) bool { return i == s.Answer || i == s.Partner }
	want := 1
	if n >= 7 {
		want = 2
	}
	for _, i := range v13Perm(seed, key("relative"), n) {
		if len(s.Relative) == want {
			break
		}
		if swapped(i) || i == s.Named {
			continue
		}
		best := -1
		for j := 0; j < n; j++ {
			if j == i || swapped(j) {
				continue
			}
			if _, rel := s.Relative[j]; rel {
				continue
			}
			if anchoring := func() bool {
				for _, a := range s.Relative {
					if a == i {
						return true
					}
				}
				return false
			}(); anchoring {
				continue
			}
			if v14AbsDays(s.Days[j], s.Days[i]) > 27 {
				continue
			}
			if best < 0 || v14AbsDays(s.Days[j], s.Days[i]) < v14AbsDays(s.Days[best], s.Days[i]) {
				best = j
			}
		}
		if best >= 0 {
			s.Relative[i] = best
		}
	}
	// The counterfactual date lies strictly between Partner and the next base
	// date beyond it (or up to three days past the end of the sequence).
	step := 1
	if s.Partner < s.Answer {
		step = -1
	}
	beyond := s.Partner + step
	if beyond < 0 || beyond >= n {
		s.CounterDay = s.Days[s.Partner].AddDate(0, 0, step*(1+v13Index(seed, key("counter"), 3)))
	} else {
		span := v14AbsDays(s.Days[s.Partner], s.Days[beyond])
		s.CounterDay = s.Days[s.Partner].AddDate(0, 0, step*(1+v13Index(seed, key("counter"), span-1)))
	}
	// Filing order is independent of date order; the base and counterfactual
	// answers are never the first or last event record filed. The neutral
	// milestone record lands anywhere.
	events := v14KeepInterior(v13Perm(seed, key("log"), n), s.Answer, s.Partner)
	at := v13Index(seed, key("neutral"), n+1)
	s.Log = append(append(append([]int(nil), events[:at]...), n), events[at:]...)
	return s
}

func v14AbsDays(a, b time.Time) int {
	if g := v14Days(a, b); g >= 0 {
		return g
	}
	return v14Days(b, a)
}

// v14KeepInterior moves each forbidden item off the first and last positions
// of order by swapping it with the nearest interior item that is not
// forbidden. order must hold at least len(forbidden)+2 items.
func v14KeepInterior(order []int, forbidden ...int) []int {
	out := append([]int(nil), order...)
	isForbidden := func(v int) bool {
		for _, f := range forbidden {
			if v == f {
				return true
			}
		}
		return false
	}
	for _, edge := range []int{0, len(out) - 1} {
		if !isForbidden(out[edge]) {
			continue
		}
		for k := 1; k < len(out)-1; k++ {
			if !isForbidden(out[k]) {
				out[edge], out[k] = out[k], out[edge]
				break
			}
		}
	}
	return out
}

func (d *v13Draws) drawV14AsOf(group int) *v14AsOf {
	seed := d.seed
	key := func(name string) string { return fmt.Sprintf("v14-asof-%s-%d", name, group) }
	// Four changes plus a decoy use the whole five-class v13 channel bank.
	n := 4
	a := &v14AsOf{}
	classes := v13Perm(seed, key("classes"), len(V13ChannelClasses))
	a.Values = append([]int(nil), classes[:n]...)
	a.Decoy = classes[n]
	day := v14PastAnchor(seed, "business", group).AddDate(0, 0, -(150 + v13Index(seed, key("start"), 150)))
	for i := 0; i < n; i++ {
		if i > 0 {
			day = day.AddDate(0, 0, 9+v13Index(seed, fmt.Sprintf("v14-asof-gap-%d-%d", group, i), 22))
		}
		a.Days = append(a.Days, day)
	}
	// The question interval is neither the first value's nor the last's, and
	// the question date sits at least two days inside it.
	a.K = 1 + v13Index(seed, key("k"), n-2)
	gap := v14Days(a.Days[a.K], a.Days[a.K+1])
	a.Query = a.Days[a.K].AddDate(0, 0, 2+v13Index(seed, key("query"), gap-3))
	later := a.K >= 2
	if a.K+1 <= n-2 && (!later || v13Index(seed, key("counter-side"), 2) == 0) {
		later = false
	}
	if later {
		rest := v14Days(a.Query, a.Days[a.K+1])
		a.CounterChange, a.CounterAnswer = a.K, a.K-1
		a.CounterDay = a.Query.AddDate(0, 0, 1+v13Index(seed, key("counter"), rest-1))
	} else {
		lead := v14Days(a.Days[a.K], a.Query)
		a.CounterChange, a.CounterAnswer = a.K+1, a.K+1
		a.CounterDay = a.Days[a.K].AddDate(0, 0, 1+v13Index(seed, key("counter"), lead-1))
	}
	a.NeutralAt = v13Index(seed, key("neutral"), n+1)
	return a
}

// v14Ordinals name sequence positions.
var v14Ordinals = []string{"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"}

// v14SetRecords places a story-ordered record list into the member.
func v14SetRecords(m *v13Member, records []string) {
	copy(m.Records[:], records)
	if len(records) > len(m.Records) {
		m.Extra = append([]string(nil), records[len(m.Records):]...)
	}
}

// renderV14EventSequence renders the SN-358 event-sequence family.
func renderV14EventSequence(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool, subject string, m *v13Member) {
	seed := d.seed
	s := g.T14.Seq
	neutral := m.Records[2]
	n := len(s.Classes)
	days := append([]time.Time(nil), s.Days...)
	if counter {
		days[s.Answer] = s.CounterDay
	}
	cls := func(i int) V13TermClass { return V14SequenceEventClasses[s.Classes[i]] }
	dateProse := map[int]string{}
	record := func(i int) string {
		ev := cls(i)
		if anchor, ok := s.Relative[i]; ok {
			gap := v14Days(days[anchor], days[i])
			when := fmt.Sprintf("%d days after", gap)
			if gap < 0 {
				when = fmt.Sprintf("%d days before", -gap)
			}
			dateProse[i] = when
			return fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v14-seq-rel-%d-%d", group, i), []string{
				"%[1]s logged a %[2]s: %[4]s, %[3]s the %[5]s.",
				"%[1]s's %[2]s entry reads %[4]s, held %[3]s the %[5]s.",
				"A %[2]s for %[1]s — %[4]s — is on record for %[3]s the %[5]s.",
			}), g.Alias, schema.Event, when, ev.Canonical, cls(anchor).Canonical)
		}
		date := v14DayProse(seed, fmt.Sprintf("seq-%d-%d", group, i), days[i])
		dateProse[i] = date
		return fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v14-seq-abs-%d-%d", group, i), []string{
			"On %[3]s, %[1]s logged a %[2]s: %[4]s.",
			"%[1]s's %[2]s entry dated %[3]s reads %[4]s.",
			"A %[2]s for %[1]s — %[4]s — is on record for %[3]s.",
		}), g.Alias, schema.Event, date, ev.Canonical)
	}
	records := make([]string, 0, n+1)
	for _, i := range s.Log {
		if i == n {
			records = append(records, neutral)
			continue
		}
		records = append(records, record(i))
	}
	v14SetRecords(m, records)
	m.DecoyClause = fmt.Sprintf(" Its only %s is a %s.", schema.Event, V14SequenceEventClasses[s.Decoy].Canonical)

	answer := s.Answer
	if counter {
		answer = s.Partner
	}
	var forms []string
	var args []any
	switch s.Query {
	case v14SeqNth:
		forms = []string{
			"counting from the earliest, which %[2]s was the %[3]s one on %[1]s?",
			"in date order, what was the %[3]s %[2]s recorded for %[1]s?",
			"putting every dated %[2]s on %[1]s in order from the earliest, which comes %[3]s?",
		}
		args = []any{subject, schema.Event, v14Ordinals[s.Answer]}
	case v14SeqAfter:
		forms = []string{
			"which %[2]s on %[1]s came immediately after the %[3]s?",
			"in date order, which %[2]s on %[1]s directly followed the %[3]s?",
			"for %[1]s, what was the next %[2]s after the %[3]s?",
		}
		args = []any{subject, schema.Event, cls(s.Named).Canonical}
	default:
		forms = []string{
			"which %[2]s on %[1]s came immediately before the %[3]s?",
			"in date order, which %[2]s on %[1]s directly preceded the %[3]s?",
			"for %[1]s, what was the last %[2]s before the %[3]s?",
		}
		args = []any{subject, schema.Event, cls(s.Named).Canonical}
	}
	m.Question = v13Question(seed, group, variant, forms,
		[]string{"Name the event.", "Just the event is fine.", "Answer with the event, not the date."}, args...)
	want := cls(answer)
	m.Expected = want.Canonical
	m.AcceptAny = append([]string(nil), want.Accept...)
	// Every other event of the sequence is a wrong same-attribute value, so a
	// reply that lists the whole timeline without naming one asserts them.
	// The event the question names is echoed by honest replies ("right after
	// the audit came ...") and is left out.
	for i := 0; i < n; i++ {
		if i != answer && i != s.Named {
			m.Distractors = append(m.Distractors, cls(i).Canonical)
		}
	}
	m.Distractors = append(m.Distractors, V14SequenceEventClasses[s.Decoy].Canonical)
	m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindEvent, want.Canonical, want.Accept, 1)}
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	m.Program = V10QueryNode{Op: s.Query + "_by_date", Field: schema.Event, Children: []V10QueryNode{resolve, {Op: "resolve_relative_dates", Field: schema.Event}, {Op: "order_by_date", Field: schema.Event}}}
	m.Protected = append(m.Protected, schema.Event)
	for i := 0; i < n; i++ {
		m.Protected = append(m.Protected, cls(i).Canonical, dateProse[i])
	}
	m.Facts = []string{"per-run schema glossary", "entity alias", fmt.Sprintf("%d dated events", n), "relative event dates", "date ordering"}
	m.Constraints = []string{g.Alias, schema.Event, g.Purpose}
	m.Operations = []string{"induce schema", "resolve subject by remit", "resolve each relative date against its anchor", "order the events by date", "select the asked position", "return the event"}
}

// renderV14ChannelAsOf renders the SN-355 channel-as-of family.
func renderV14ChannelAsOf(d *v13Draws, schema v13Schema, group, variant int, g v13Group, counter bool, subject string, m *v13Member) {
	seed := d.seed
	a := g.T14.AsOf
	neutral := m.Records[2]
	days := append([]time.Time(nil), a.Days...)
	answer := a.K
	if counter {
		days[a.CounterChange] = a.CounterDay
		answer = a.CounterAnswer
	}
	ch := func(i int) V13TermClass { return V13ChannelClasses[a.Values[i]] }
	var changes []string
	for i := range a.Values {
		date := v14DayProse(seed, fmt.Sprintf("asof-%d-%d", group, i), days[i])
		m.Protected = append(m.Protected, date)
		if i == 0 {
			changes = append(changes, fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v14-asof-first-%d", group), []string{
				"%[1]s's %[2]s was set up as %[3]s on %[4]s.",
				"On %[4]s, %[1]s opened its %[2]s over %[3]s.",
				"The %[2]s first agreed for %[1]s, on %[4]s, was %[3]s.",
			}), g.Alias, schema.Channel, ch(i).Canonical, date))
			continue
		}
		changes = append(changes, fmt.Sprintf(v13Pick(seed, fmt.Sprintf("v14-asof-change-%d-%d", group, i), []string{
			"From %[4]s, %[1]s's %[2]s moved to %[3]s.",
			"On %[4]s the %[2]s for %[1]s switched to %[3]s.",
			"%[1]s changed its %[2]s to %[3]s with effect from %[4]s.",
		}), g.Alias, schema.Channel, ch(i).Canonical, date))
	}
	records := make([]string, 0, len(changes)+1)
	records = append(records, changes[:a.NeutralAt]...)
	records = append(records, neutral)
	records = append(records, changes[a.NeutralAt:]...)
	v14SetRecords(m, records)
	m.DecoyClause = fmt.Sprintf(" Its %s is %s.", schema.Channel, V13ChannelClasses[a.Decoy].Canonical)
	query := v14DayProse(seed, fmt.Sprintf("asof-query-%d", group), a.Query)
	m.Question = v13Question(seed, group, variant, []string{
		"which %[2]s was in force for %[1]s on %[3]s?",
		"on %[3]s, which %[2]s did %[1]s use to reach the other side?",
		"for %[1]s, what %[2]s applied on %[3]s?",
	}, []string{"Name the channel.", "Just the channel that applied that day.", "Answer with the channel."}, subject, schema.Channel, query)
	want := ch(answer)
	m.Expected = want.Canonical
	m.AcceptAny = append([]string(nil), want.Accept...)
	// The other values of the chain are wrong for that day: the latest one is
	// the "pick the newest record" answer and the first one is record 0.
	for i := range a.Values {
		if i != answer {
			m.Distractors = append(m.Distractors, ch(i).Canonical)
		}
	}
	m.Distractors = append(m.Distractors, V13ChannelClasses[a.Decoy].Canonical)
	m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindChannel, want.Canonical, want.Accept, 1)}
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	m.Program = V10QueryNode{Op: "as_of", Field: schema.Channel, Children: []V10QueryNode{resolve, {Op: "order_by_date", Field: schema.Channel}}}
	m.Protected = append(m.Protected, schema.Channel, query, V13ChannelClasses[a.Decoy].Canonical)
	for i := range a.Values {
		m.Protected = append(m.Protected, ch(i).Canonical)
	}
	m.Facts = []string{"per-run schema glossary", "entity alias", fmt.Sprintf("%d dated channel changes", len(a.Values)), "question date"}
	m.Constraints = []string{g.Alias, schema.Channel, g.Purpose, query}
	m.Operations = []string{"induce schema", "resolve subject by remit", "order the channel changes by date", "find the interval holding the question date", "return the channel in force that day"}
}
