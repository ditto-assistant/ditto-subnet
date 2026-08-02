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
// Expired timestamps are pruned on access; empty keys are garbage-collected.
func (l *Limiter) Allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()

	now := l.now()
	cutoff := now.Add(-l.window)

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
