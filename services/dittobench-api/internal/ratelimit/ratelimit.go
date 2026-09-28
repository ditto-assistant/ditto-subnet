// Package ratelimit provides a small in-memory sliding-window limiter keyed by
// client (IP). It is a first-line abuse guard for the public submit endpoint;
// it is per-instance (not global across Cloud Run replicas), which is adequate
// for slowing abuse without external state.
package ratelimit

import (
	"sync"
	"time"
)

// Limiter allows at most max events per key within window (sliding).
type Limiter struct {
	mu     sync.Mutex
	max    int
	window time.Duration
	hits   map[string][]time.Time
	sweep  time.Time        // next time idle keys are dropped
	now    func() time.Time // injectable for tests
}

// New returns a Limiter allowing max events per window per key.
func New(max int, window time.Duration) *Limiter {
	return &Limiter{
		max:    max,
		window: window,
		hits:   make(map[string][]time.Time),
		now:    time.Now,
	}
}

// Allow records an event for key and reports whether it is within the limit.
// Expired timestamps are pruned on access. At most once per window, keys with
// no event inside the window are dropped, so the map only holds clients seen
// in the last two windows; each event keeps its key alive for at most two
// sweeps, so sweeping stays O(1) amortized per call.
func (l *Limiter) Allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()

	now := l.now()
	cutoff := now.Add(-l.window)

	if !now.Before(l.sweep) {
		for k, ts := range l.hits {
			// Timestamps are appended in order, so the last is the newest.
			if len(ts) == 0 || !ts[len(ts)-1].After(cutoff) {
				delete(l.hits, k)
			}
		}
		l.sweep = now.Add(l.window)
	}

	kept := l.hits[key][:0]
	for _, t := range l.hits[key] {
		if t.After(cutoff) {
			kept = append(kept, t)
		}
	}

	if len(kept) >= l.max {
		l.hits[key] = kept
		return false
	}
	l.hits[key] = append(kept, now)
	return true
}
