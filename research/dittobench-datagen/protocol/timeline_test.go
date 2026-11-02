package protocol

import (
	"testing"
	"time"
)

func TestOpaqueTimelineIsDeterministicMonotonicAndBusinessHours(t *testing.T) {
	a, b := NewOpaqueTimeline(41, "world"), NewOpaqueTimeline(41, "world")
	var previous time.Time
	gaps := map[time.Duration]int{}
	minutes := map[int]bool{}
	for i := 0; i < 60; i++ {
		got, again := a.Next(), b.Next()
		if got != again {
			t.Fatalf("draw %d is not deterministic: %s vs %s", i, got, again)
		}
		at, err := time.Parse(time.RFC3339, got)
		if err != nil {
			t.Fatalf("draw %d is not RFC3339: %v", i, err)
		}
		if !at.After(previous) {
			t.Fatalf("draw %d %s is not after %s", i, at, previous)
		}
		if at.Hour() < 8 || at.Hour() >= 18 || at.Weekday() == time.Saturday || at.Weekday() == time.Sunday || at.Second() != 0 {
			t.Fatalf("draw %d %s is outside business hours", i, at)
		}
		if i > 0 {
			gaps[at.Sub(previous)]++
		}
		minutes[at.Minute()] = true
		previous = at
	}
	for gap, count := range gaps {
		if count > 3 {
			t.Fatalf("gap %s repeats %d times: the timeline has a constant step", gap, count)
		}
	}
	if len(minutes) < 30 {
		t.Fatalf("only %d distinct minute values across 60 draws", len(minutes))
	}
	if span := previous.Sub(opaqueTimelineWindowStart); span > 365*24*time.Hour {
		t.Fatalf("60 draws span %s; a family timeline must stay inside the shared window", span)
	}
}

func TestOpaqueTimelinesDifferByKindAndSeed(t *testing.T) {
	first := NewOpaqueTimeline(41, "world").Next()
	if NewOpaqueTimeline(41, "program").Next() == first || NewOpaqueTimeline(42, "world").Next() == first {
		t.Fatal("kind or seed did not move the timeline start")
	}
	starts := map[string]bool{}
	for seed := int64(1); seed <= 60; seed++ {
		starts[NewOpaqueTimeline(seed, "world").Next()[:7]] = true
	}
	if len(starts) < 5 {
		t.Fatalf("60 seeds started in only %d distinct months", len(starts))
	}
}
