package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

func TestCaseConcurrencyForRunUsesRuntimeAndCaps(t *testing.T) {
	if got := caseConcurrencyForRun(nil); got != activeCaseConcurrency() {
		t.Fatalf("nil runtime = %d, want env default %d", got, activeCaseConcurrency())
	}
	if got := caseConcurrencyForRun(&benchmarkRuntimeSettings{CaseConcurrency: 4}); got != 4 {
		t.Fatalf("runtime 4 = %d", got)
	}
	if got := caseConcurrencyForRun(&benchmarkRuntimeSettings{CaseConcurrency: 64}); got != 64 {
		t.Fatalf("runtime 64 = %d", got)
	}
	if got := caseConcurrencyForRun(&benchmarkRuntimeSettings{CaseConcurrency: 65}); got != maxBenchmarkCaseConcurrency {
		t.Fatalf("runtime 65 = %d, want cap %d", got, maxBenchmarkCaseConcurrency)
	}
}

func TestV8DefaultsShipParallel(t *testing.T) {
	if activeCaseConcurrency() < 2 {
		t.Fatalf("default v8 case concurrency = %d, want > 1", activeCaseConcurrency())
	}
	if v8EmbeddingSessionConcurrency < 2 {
		t.Fatalf(
			"default v8 embedding session concurrency = %d, want > 1",
			v8EmbeddingSessionConcurrency,
		)
	}
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
