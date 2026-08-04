package gen

import (
	"regexp"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
)

var pitDateRe = regexp.MustCompile(`(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}`)

// TestPointInTimeDateAgreesWithTimestamps is the cross-layer contract for the
// point-in-time modality: the calendar date printed in the question falls
// strictly between the rendered timestamps of the two evidence facts, and the
// expected answer is the earlier fact's value (the state in force on that
// date). This is what makes the question resolvable from the haystack alone.
func TestPointInTimeDateAgreesWithTimestamps(t *testing.T) {
	found := 0
	for seed := int64(1); seed <= 30; seed++ {
		plan := persona.BuildPlan(seed, persona.DefaultOpts())
		pairs, evidence := RenderHaystack(plan)
		tsOf := map[string]string{}
		for _, p := range pairs {
			tsOf[p.PairID] = p.Timestamp
		}
		for _, q := range persona.DeriveQuestions(plan) {
			if q.Type != persona.QTPointInTime {
				continue
			}
			found++
			m := pitDateRe.FindString(q.Text)
			if m == "" {
				t.Fatalf("seed %d: no date in %q", seed, q.Text)
			}
			date, err := time.Parse("January 2, 2006", m)
			if err != nil {
				t.Fatalf("seed %d: unparseable date %q", seed, m)
			}
			if len(q.Evidence) != 2 {
				t.Fatalf("seed %d: evidence %v, want 2 facts", seed, q.Evidence)
			}
			var bounds []time.Time
			for _, fid := range q.Evidence {
				pid, ok := evidence[fid]
				if !ok {
					t.Fatalf("seed %d: evidence fact %s has no rendered pair", seed, fid)
				}
				ts, err := time.Parse(time.RFC3339, tsOf[pid])
				if err != nil {
					t.Fatalf("seed %d: bad pair timestamp %q", seed, tsOf[pid])
				}
				bounds = append(bounds, ts)
			}
			if !bounds[0].Before(date) || !date.Before(bounds[1]) {
				t.Fatalf("seed %d: date %s not strictly between %s and %s",
					seed, date, bounds[0], bounds[1])
			}
			state, ok := plan.FactByID(q.Evidence[0])
			if !ok || state.Value != q.Answer {
				t.Fatalf("seed %d: answer %q does not match evidence state %q", seed, q.Answer, state.Value)
			}
			if state.Current {
				t.Fatalf("seed %d: point-in-time answer is the CURRENT value; it must be superseded", seed)
			}
		}
	}
	if found == 0 {
		t.Fatal("no point-in-time questions across 30 seeds; modality is dead")
	}
}
