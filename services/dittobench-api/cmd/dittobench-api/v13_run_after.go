package main

import (
	"context"
	"log"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// runAfterGate enforces ToolCase.RunAfterCaseID inside the bounded tool loop
// (issue #1845). The harness projection only ORDERS a follow-up read after the
// mutation it verifies (gen.V13ArrangeToolOrder, gap >= 40); runBounded then
// starts index j whenever any slot frees, so with case_concurrency >= 2 the
// follow-up /run could reach the harness while the mutation's /run is still in
// flight, and an honest harness would be zeroed on the end-state read. The gate
// makes the dependency real: a dependent case blocks until its mutation's /run
// has returned (or the run context ends), and every case releases its channel
// when its work is done — on panic too — so a waiter can never hang on a case
// that never finished.
//
// A dependency is honoured only when it sits at a LOWER index: runBounded
// launches in index order, so a lower-index dependency has always been started
// before its dependent and the wait cannot deadlock a slot pool of any size. A
// dependency at a higher index (a projection that could not fit the gap) is
// logged and not waited on rather than risking a deadlock at concurrency 1.
type runAfterGate struct {
	done      []chan struct{}
	dependsOn map[int]int
}

func newRunAfterGate(cases []protocol.ToolCase) *runAfterGate {
	g := &runAfterGate{done: make([]chan struct{}, len(cases)), dependsOn: map[int]int{}}
	index := make(map[string]int, len(cases))
	for i, c := range cases {
		g.done[i] = make(chan struct{})
		index[c.ID] = i
	}
	for i, c := range cases {
		if c.RunAfterCaseID == "" {
			continue
		}
		dep, ok := index[c.RunAfterCaseID]
		if !ok {
			continue
		}
		if dep >= i {
			log.Printf("v13 run-after: case %s depends on %s at a later position (%d >= %d); not waited on", c.ID, c.RunAfterCaseID, dep, i)
			continue
		}
		g.dependsOn[i] = dep
	}
	return g
}

// wait blocks until case i's dependency (if any) has released, or ctx ends.
// It reports false when the context ended first.
func (g *runAfterGate) wait(ctx context.Context, i int) bool {
	dep, ok := g.dependsOn[i]
	if !ok {
		return true
	}
	select {
	case <-g.done[dep]:
		return true
	case <-ctx.Done():
		return false
	}
}

// release marks case i finished. Idempotent.
func (g *runAfterGate) release(i int) {
	select {
	case <-g.done[i]:
	default:
		close(g.done[i])
	}
}

// pending reports how many cases carry an honoured dependency.
func (g *runAfterGate) pending() int { return len(g.dependsOn) }
