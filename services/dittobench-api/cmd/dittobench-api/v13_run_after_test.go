package main

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestRunAfterGateHoldsFollowUpUntilMutationReturns is the executor half of
// the v13 "must run AFTER" contract: with the live maximum case concurrency
// (64) and a mutation that is slow to return, the dependent follow-up read is
// not started until the mutation has finished, while every independent case
// runs unblocked.
func TestRunAfterGateHoldsFollowUpUntilMutationReturns(t *testing.T) {
	const n = 100
	cases := make([]protocol.ToolCase, n)
	for i := range cases {
		cases[i] = protocol.ToolCase{ID: "case-" + string(rune('a'+i%26)) + string(rune('a'+i/26))}
	}
	const mutation, followUp = 3, 47
	cases[followUp].RunAfterCaseID = cases[mutation].ID
	gate := newRunAfterGate(cases)
	if gate.pending() != 1 {
		t.Fatalf("pending = %d, want 1", gate.pending())
	}

	var mu sync.Mutex
	started := make([]time.Time, n)
	finished := make([]time.Time, n)
	independentStartedBeforeMutationDone := 0
	runBounded(context.Background(), n, maxBenchmarkCaseConcurrency, func(i int) {
		defer gate.release(i)
		if !gate.wait(context.Background(), i) {
			t.Errorf("case %d: gate reported context end", i)
			return
		}
		mu.Lock()
		started[i] = time.Now()
		mu.Unlock()
		if i == mutation {
			time.Sleep(150 * time.Millisecond)
		} else if i != followUp {
			mu.Lock()
			if finished[mutation].IsZero() {
				independentStartedBeforeMutationDone++
			}
			mu.Unlock()
		}
		mu.Lock()
		finished[i] = time.Now()
		mu.Unlock()
	})
	if !started[followUp].After(finished[mutation]) && !started[followUp].Equal(finished[mutation]) {
		t.Fatalf("follow-up started at %v, before the mutation finished at %v", started[followUp], finished[mutation])
	}
	if independentStartedBeforeMutationDone == 0 {
		t.Fatal("no independent case ran while the mutation was in flight; the gate serialized the run")
	}
	for i := range cases {
		if started[i].IsZero() || finished[i].IsZero() {
			t.Fatalf("case %d never ran", i)
		}
	}
}

// TestRunAfterGateNeverDeadlocks: a dependency at a HIGHER index is not waited
// on (it could never be satisfied at concurrency 1), a context end releases a
// waiter, and release is idempotent.
func TestRunAfterGateNeverDeadlocks(t *testing.T) {
	cases := []protocol.ToolCase{{ID: "later-follow-up", RunAfterCaseID: "mutation"}, {ID: "mutation"}, {ID: "orphan", RunAfterCaseID: "missing"}}
	gate := newRunAfterGate(cases)
	if gate.pending() != 0 {
		t.Fatalf("pending = %d, want 0 (misordered and dangling dependencies are not honoured)", gate.pending())
	}
	ran := 0
	runBounded(context.Background(), len(cases), 1, func(i int) {
		defer gate.release(i)
		gate.wait(context.Background(), i)
		ran++
	})
	if ran != len(cases) {
		t.Fatalf("ran %d of %d at concurrency 1", ran, len(cases))
	}

	ordered := []protocol.ToolCase{{ID: "m"}, {ID: "f", RunAfterCaseID: "m"}}
	gate = newRunAfterGate(ordered)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if gate.wait(ctx, 1) {
		t.Fatal("a cancelled context must release the waiter with false")
	}
	gate.release(0)
	gate.release(0)
	if !gate.wait(context.Background(), 1) {
		t.Fatal("released dependency must satisfy the waiter")
	}
}
