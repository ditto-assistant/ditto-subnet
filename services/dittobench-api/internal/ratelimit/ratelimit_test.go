package ratelimit

import (
	"fmt"
	"sync"
	"sync/atomic"
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

// Every distinct client IP adds a key; keys idle for a full window must be
// dropped or memory grows without bound on a public endpoint.
func TestIdleKeysAreEvicted(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	cur := base
	l := New(2, time.Minute)
	l.now = func() time.Time { return cur }

	for i := 0; i < 1000; i++ {
		l.Allow(fmt.Sprintf("ip%d", i))
	}
	if len(l.hits) != 1000 {
		t.Fatalf("keys = %d, want 1000", len(l.hits))
	}

	// Within the window nothing is swept.
	cur = base.Add(30 * time.Second)
	l.Allow("ip0")
	if len(l.hits) != 1000 {
		t.Fatalf("keys inside the window = %d, want 1000", len(l.hits))
	}

	// Past the window every idle key goes; only the caller and the key it
	// refreshed at +30s remain.
	cur = base.Add(61 * time.Second)
	l.Allow("fresh")
	if len(l.hits) != 2 {
		t.Fatalf("keys after the window = %d, want 2 (ip0, fresh)", len(l.hits))
	}

	// Once those go idle too, the map drains to the single live caller.
	cur = base.Add(3 * time.Minute)
	l.Allow("last")
	if _, ok := l.hits["last"]; !ok || len(l.hits) != 1 {
		t.Fatalf("keys after all idle = %v, want only last", l.hits)
	}
}

// Run with -race: sweeps and inserts from many goroutines share one map.
func TestConcurrentAllowHonorsLimitAndEvicts(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	var cur atomic.Int64
	l := New(5, time.Minute)
	l.now = func() time.Time { return time.Unix(0, cur.Load()) }

	burst := func(at time.Time) int64 {
		cur.Store(at.UnixNano())
		var allowed atomic.Int64
		var wg sync.WaitGroup
		for g := 0; g < 32; g++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < 50; i++ {
					if l.Allow("shared") {
						allowed.Add(1)
					}
					l.Allow(fmt.Sprintf("%d-%d-%d", at.Unix(), g, i))
				}
			}()
		}
		wg.Wait()
		return allowed.Load()
	}

	if got := burst(base); got != 5 {
		t.Fatalf("shared key allowed %d times, want 5", got)
	}
	// The second burst starts a window later, so its first call sweeps the
	// first burst's keys while the other goroutines insert.
	if got := burst(base.Add(90 * time.Second)); got != 5 {
		t.Fatalf("shared key allowed %d times after the window, want 5", got)
	}
	if want := 1 + 32*50; len(l.hits) != want {
		t.Fatalf("keys after second burst = %d, want %d", len(l.hits), want)
	}

	cur.Store(base.Add(10 * time.Minute).UnixNano())
	l.Allow("after")
	if len(l.hits) != 1 {
		t.Fatalf("keys after all idle = %d, want 1", len(l.hits))
	}
}
