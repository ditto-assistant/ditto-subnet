package protocol

import (
	"fmt"
	"hash/fnv"
	"time"
)

// OpaqueTimeline yields strictly increasing, business-hours RFC3339 timestamps
// whose gaps are seeded jitter rather than a constant step. It is the v13
// replacement for the fixed patterns earlier contracts emitted (a 137-hour
// stride from one epoch for world pairs, "2026-01-0<i>T0<9+i>" for program
// records, "2026-03-<ordinal>T<8+ordinal>:20" for compiler families): each of
// those was a per-family fingerprint a /seed reader could classify on before
// reading a single word of prose.
//
// Every timeline draws from the same gap distribution and the same calendar
// window, so the month, day-of-month, weekday, hour, minute, and inter-record
// delta of a pair carry no information about which generator family emitted
// it. Chronological order inside one timeline is preserved (a correction still
// lands after the record it corrects), which is the only temporal structure the
// oracles rely on.
//
// It is a pure function of (seed, kind, ordinal): the same inputs always yield
// the same timestamps, so the artifact stays byte-reproducible from the seed.
type OpaqueTimeline struct {
	seed    int64
	kind    string
	ordinal int
	current time.Time
}

// opaqueTimelineWindowStart is the Monday the v13 calendar window opens on.
// Every timeline starts within the following 180 days, so no family owns a
// distinguishable start month: a six-record program timeline lands anywhere in
// the window, and a 250-record world timeline sweeps most of it.
var opaqueTimelineWindowStart = time.Date(2025, 1, 6, 9, 0, 0, 0, time.UTC)

// NewOpaqueTimeline starts a timeline for one generator family. kind is hash
// input only (it namespaces the stream); it never reaches the wire.
func NewOpaqueTimeline(seed int64, kind string) *OpaqueTimeline {
	t := &OpaqueTimeline{seed: seed, kind: kind}
	// Start on a uniformly drawn WORKING day (the window's 180 calendar days
	// hold 128 of them). Drawing a calendar day and snapping weekends forward
	// would make Monday three times as likely as any other weekday, and a chain
	// member shifted a few working days from a Monday-heavy anchor would then
	// carry its slot in the weekday field.
	// The time of day is drawn over the whole 08:00-17:59 range, exactly as
	// every later record's is (Next), so the first record of a timeline has the
	// same hour distribution as its followers and the hour cannot name slot 0.
	startWorkdays := int(t.draw("start") % opaqueTimelineWindowWorkdays)
	startMinute := int(t.draw("start-minute") % 600)
	t.current = ShiftBusinessDays(opaqueTimelineWindowStart, startWorkdays, startMinute)
	return t
}

// opaqueTimelineWindowWorkdays is the number of weekdays in the 180-day
// calendar window every v13 timeline starts inside.
const opaqueTimelineWindowWorkdays = 128

// Next returns the next timestamp on the timeline. Every record after the
// first lands one to four working days after the previous one at an
// independently drawn time of day, snapped into 08:00-17:59 UTC on a weekday.
// Advancing by whole working days (rather than by minutes inside one day) is
// deliberate: an intra-day stride would make a record's hour grow with its slot,
// and the slot is exactly what the wire must not carry. A 24-record family then
// spans a few months inside the shared window; a six-record program group spans
// two or three weeks.
func (t *OpaqueTimeline) Next() string {
	if t.ordinal > 0 {
		days := 1 + int(t.draw("gap-days")%4)
		minute := int(t.draw("time-of-day") % 600)
		t.current = ShiftBusinessDays(t.current, days, minute)
	}
	t.ordinal++
	return t.current.Format(time.RFC3339)
}

// draw is a splitmix64 mix of (seed, kind, ordinal, salt): independent of every
// other generator RNG stream, so adding a timeline never perturbs world draws.
func (t *OpaqueTimeline) draw(salt string) uint64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-opaque-timeline|%d|%s|%d|%s", t.seed, t.kind, t.ordinal, salt)
	z := h.Sum64() + 0x9E3779B97F4A7C15
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

// snapToBusinessHours moves an instant forward to the next weekday moment
// between 08:00 and 17:59 UTC, preserving the minute within the hour so the
// minute field stays uniformly distributed rather than snapping to :00.
func snapToBusinessHours(at time.Time) time.Time {
	at = at.Truncate(time.Minute)
	for {
		switch at.Weekday() {
		case time.Saturday:
			at = time.Date(at.Year(), at.Month(), at.Day()+2, 8, at.Minute(), 0, 0, time.UTC)
			continue
		case time.Sunday:
			at = time.Date(at.Year(), at.Month(), at.Day()+1, 8, at.Minute(), 0, 0, time.UTC)
			continue
		}
		if at.Hour() < 8 {
			return time.Date(at.Year(), at.Month(), at.Day(), 8, at.Minute(), 0, 0, time.UTC)
		}
		if at.Hour() >= 18 {
			at = time.Date(at.Year(), at.Month(), at.Day()+1, 8, at.Minute(), 0, 0, time.UTC)
			continue
		}
		return at
	}
}

// OpaqueBusinessInstant returns one seeded business-hours instant drawn
// uniformly from the shared v13 calendar window: the start of the timeline
// (seed, kind) would produce. It is the building block for stamps that must be
// independent of one another (no emission-order or slot structure) while
// staying inside the same window every timeline uses.
func OpaqueBusinessInstant(seed int64, kind string) time.Time {
	return NewOpaqueTimeline(seed, kind).current
}

// ShiftBusinessDays moves at forward by days weekdays and sets the time of day
// from minuteOfDay (0..599 -> 08:00..17:59), so a follow-up record lands on a
// later working day with a time of day that carries no information about how
// many records preceded it.
func ShiftBusinessDays(at time.Time, days int, minuteOfDay int) time.Time {
	if days < 0 {
		days = 0
	}
	minuteOfDay = ((minuteOfDay % 600) + 600) % 600
	at = time.Date(at.Year(), at.Month(), at.Day(), 8+minuteOfDay/60, minuteOfDay%60, 0, 0, time.UTC)
	for days > 0 {
		at = at.AddDate(0, 0, 1)
		if at.Weekday() != time.Saturday && at.Weekday() != time.Sunday {
			days--
		}
	}
	return snapToBusinessHours(at)
}
