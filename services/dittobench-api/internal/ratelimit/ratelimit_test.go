package ratelimit

import (
	"testing"
	"time"
)

func TestAllowWithinAndOverLimit(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	l := New(3, time.Minute)
	l.now = func() time.Time { return base }

	for i := 0; i < 3; i++ {
		if !l.Allow("ip1") {
			t.Fatalf("event %d should be allowed", i)
		}
	}
	if l.Allow("ip1") {
		t.Fatal("4th event in window should be blocked")
	}
	// Different key is independent.
	if !l.Allow("ip2") {
		t.Fatal("other key should be allowed")
	}
}

func TestWindowSlides(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	cur := base
	l := New(2, time.Minute)
	l.now = func() time.Time { return cur }

	if !l.Allow("k") || !l.Allow("k") {
		t.Fatal("first two should pass")
	}
	if l.Allow("k") {
		t.Fatal("third should be blocked")
	}
	// Advance past the window — old hits expire.
	cur = base.Add(61 * time.Second)
	if !l.Allow("k") {
		t.Fatal("after window, should be allowed again")
	}
}
