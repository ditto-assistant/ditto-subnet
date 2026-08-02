package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

// This test previously asserted that v7 cases run strictly one at a time. That
// contract existed only because the hosted embedding lane admitted one request
// per ticket, so parallel cases would have manufactured 429s. The lane is no
// longer one wide, so the contract it encoded is gone and the test now pins the
// replacement: v7 runs its cases in parallel, and the historical versions keep
// the concurrency they were calibrated at.
func TestCaseConcurrencyForVersionParallelisesV7AndLeavesHistoryAlone(t *testing.T) {
	originalCase, originalV7 := caseConcurrency, v7CaseConcurrency
	caseConcurrency, v7CaseConcurrency = 4, 4
	t.Cleanup(func() { caseConcurrency, v7CaseConcurrency = originalCase, originalV7 })

	for _, version := range []int{2, 3, 4, 5, 6} {
		if got := caseConcurrencyForVersion(version); got != 4 {
			t.Fatalf("v%d case concurrency = %d, want 4 (unchanged)", version, got)
		}
	}
	if got := caseConcurrencyForVersion(7); got != 4 {
		t.Fatalf("v7 case concurrency = %d, want 4", got)
	}

	// The two are independently tunable: throttling v7 must not disturb the
	// frozen historical concurrency, and vice versa.
	v7CaseConcurrency = 1
	if got := caseConcurrencyForVersion(7); got != 1 {
		t.Fatalf("throttled v7 case concurrency = %d, want 1", got)
	}
	if got := caseConcurrencyForVersion(6); got != 4 {
		t.Fatalf("v6 case concurrency = %d after throttling v7, want 4", got)
	}
}

// The shipped default is the improvement, not an opt-in. A change that made
// DITTOBENCH_V7_CASE_CONCURRENCY default back to 1 would leave every other
// layer widened and deliver nothing, which is precisely the shape of a no-op
// this work exists to avoid.
func TestV7DefaultsShipParallel(t *testing.T) {
	if v7CaseConcurrency < 2 {
		t.Fatalf("default v7 case concurrency = %d, want > 1", v7CaseConcurrency)
	}
	if v7EmbeddingSessionConcurrency < 2 {
		t.Fatalf(
			"default v7 embedding session concurrency = %d, want > 1",
			v7EmbeddingSessionConcurrency,
		)
	}
	// The local Ollama lane must stay pinned: "we will never run v6 again" is an
	// operational intention, not a code guarantee, and the v2-v6 paths still
	// exist to be exercised.
	if maxConcurrentMemoryPhases != 1 {
		t.Fatalf("local memory-phase concurrency = %d, want 1", maxConcurrentMemoryPhases)
	}
}

func TestRunBoundedRunsEveryIndexOnce(t *testing.T) {
	const n = 200
	var seen [n]int32
	runBounded(context.Background(), n, 8, func(i int) {
		atomic.AddInt32(&seen[i], 1)
	})
	for i := 0; i < n; i++ {
		if got := atomic.LoadInt32(&seen[i]); got != 1 {
			t.Fatalf("index %d ran %d times, want exactly 1", i, got)
		}
	}
}

func TestRunBoundedRespectsConcurrencyCap(t *testing.T) {
	const concurrency = 4
	var inFlight, peak int32
	var mu sync.Mutex
	runBounded(context.Background(), 100, concurrency, func(i int) {
		cur := atomic.AddInt32(&inFlight, 1)
		mu.Lock()
		if cur > peak {
			peak = cur
		}
		mu.Unlock()
		for j := 0; j < 1000; j++ { // brief busy work to overlap workers
			_ = j
		}
		atomic.AddInt32(&inFlight, -1)
	})
	if peak > concurrency {
		t.Fatalf("peak in-flight %d exceeded the cap %d", peak, concurrency)
	}
}

func TestRunBoundedSequentialIsUnbounded1(t *testing.T) {
	// concurrency 1 must run strictly one at a time (the original behavior).
	var inFlight int32
	var raced int32
	runBounded(context.Background(), 50, 1, func(i int) {
		if atomic.AddInt32(&inFlight, 1) != 1 {
			atomic.StoreInt32(&raced, 1)
		}
		atomic.AddInt32(&inFlight, -1)
	})
	if atomic.LoadInt32(&raced) != 0 {
		t.Fatal("concurrency=1 must never overlap two calls")
	}
}

func TestRunBoundedStopsSchedulingOnCancel(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var ran int32
	runBounded(ctx, 1000, 2, func(i int) {
		if atomic.AddInt32(&ran, 1) == 1 {
			cancel() // cancel partway; scheduling should stop promptly
		}
	})
	// Not all 1000 should have been scheduled after an early cancel.
	if got := atomic.LoadInt32(&ran); got >= 1000 {
		t.Fatalf("cancel did not stop scheduling: ran %d of 1000", got)
	}
}
