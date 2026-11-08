package main

import (
	"context"
	"math/rand"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestRunBudgetAdmitsUntilThePricedCostReachesTheBudget(t *testing.T) {
	var prompt uint64
	b := &runBudget{benchVersion: protocol.BenchVersionV18, cases: 4,
		meter: func() (scoregates.PricedCostUsage, bool) {
			return scoregates.PricedCostUsage{ChatComplete: true, PromptTokens: prompt}, true
		}}
	b.budget, _ = scoregates.RunBudgetNanoUSD(protocol.BenchVersionV18, 4)
	perCaseTokens := b.budget / 70 / 3 // three cases spend the whole budget
	admitted := 0
	for i := 0; i < 4; i++ {
		if b.admit() {
			admitted++
			prompt += perCaseTokens + 1
		} else {
			b.refuse(protocol.KindMemory)
		}
	}
	got := b.summary()
	if admitted != 3 || !got.Exhausted || got.AdmittedCases != 3 || got.UnrunCases != 1 || got.UnrunMemoryCases != 1 ||
		got.Reason != protocol.RunBudgetExhaustedReason || got.SpentNanoUSDAtCutoff < b.budget {
		t.Fatalf("admitted %d, summary %+v", admitted, got)
	}
	// Once exhausted it stays exhausted, whatever the meter says.
	prompt = 0
	if b.admit() {
		t.Fatal("budget re-opened")
	}
	var none *runBudget
	if !none.admit() || none.summary() != nil {
		t.Fatal("a nil budget must admit everything and report nothing")
	}
}

func TestBudgetExhaustedCaseIsNeverAnInfrastructureFailure(t *testing.T) {
	cs := budgetExhaustedCase("c1", "world-trip-current", protocol.KindMemory)
	if !cs.BudgetExhausted || cs.Undelivered || cs.ValidatorFault || cs.Score != 0 {
		t.Fatalf("%+v", cs)
	}
}

func TestNoRunBudgetBelowV18OrWithoutASession(t *testing.T) {
	s := &server{}
	if s.newRunBudget(protocol.BenchVersionV17, "session", 350) != nil || s.newRunBudget(protocol.BenchVersionV18, "", 350) != nil {
		t.Fatal("budget without a contract or a session")
	}
}

// spendingBudget is a budget whose meter is a spend counter the test drives:
// limit units exhaust it.
func spendingBudget(limit uint64, spent *atomic.Uint64) *runBudget {
	b := &runBudget{benchVersion: protocol.BenchVersionV18, cases: 1}
	b.budget, _ = scoregates.RunBudgetNanoUSD(protocol.BenchVersionV18, 1)
	unit := b.budget / 70 / limit
	b.meter = func() (scoregates.PricedCostUsage, bool) {
		return scoregates.PricedCostUsage{ChatComplete: true, PromptTokens: spent.Load() * (unit + 1)}, true
	}
	return b
}

// Omar's reproduction (PR #3421 review): concurrency 2, case 0 delayed until
// case 1 has spent the whole budget. Admission inside the worker admitted
// case 1 and refused case 0 ([false,true]). At dispatch both are admitted
// before either runs, and case 2, dispatched once case 1's slot frees, is
// refused: [true,true,false], a prefix.
func TestRunBudgetAdmissionIsIndexOrdered(t *testing.T) {
	var spent atomic.Uint64
	b := spendingBudget(1, &spent)
	caseOneSpent := make(chan struct{})
	admitted := make([]bool, 3)
	runBoundedAdmitted(context.Background(), 3, 2, b.admitter(), func(i int, ok bool) {
		admitted[i] = ok
		if !ok {
			return
		}
		switch i {
		case 0:
			<-caseOneSpent
		case 1:
			spent.Add(1)
			close(caseOneSpent)
		}
	})
	if !admitted[0] || !admitted[1] || admitted[2] {
		t.Fatalf("admitted by index %v, want [true true false]", admitted)
	}
}

// Under reversed and random worker scheduling the admitted cases are always a
// prefix of the dispatch order, every admitted case runs to completion, the
// concurrency bound holds, and a nil budget (v17) admits everything.
func TestRunBudgetAdmittedCasesAreAPrefixUnderAnySchedule(t *testing.T) {
	r := rand.New(rand.NewSource(18))
	for trial := 0; trial < 60; trial++ {
		const n, concurrency = 12, 3
		var spent atomic.Uint64
		b := spendingBudget(uint64(2+r.Intn(6)), &spent)
		delays := make([]time.Duration, n)
		for i := range delays {
			// Later cases tend to finish first: reversed scheduling.
			delays[i] = time.Duration(n-i+r.Intn(3)) * 200 * time.Microsecond
		}
		var mu sync.Mutex
		admitted, ran := make([]bool, n), make([]bool, n)
		var inFlight, peak atomic.Int32
		runBoundedAdmitted(context.Background(), n, concurrency, b.admitter(), func(i int, ok bool) {
			now := inFlight.Add(1)
			defer inFlight.Add(-1)
			for p := peak.Load(); now > p && !peak.CompareAndSwap(p, now); p = peak.Load() {
			}
			mu.Lock()
			admitted[i] = ok
			mu.Unlock()
			if !ok {
				return
			}
			time.Sleep(delays[i])
			spent.Add(1)
			mu.Lock()
			ran[i] = true
			mu.Unlock()
		})
		refusedSeen := false
		for i := 0; i < n; i++ {
			if !admitted[i] {
				refusedSeen = true
			} else if refusedSeen {
				t.Fatalf("trial %d: admitted %v is not a prefix", trial, admitted)
			}
			if admitted[i] != ran[i] {
				t.Fatalf("trial %d: case %d admitted=%v ran=%v", trial, i, admitted[i], ran[i])
			}
		}
		if peak.Load() > concurrency {
			t.Fatalf("trial %d: %d workers in flight, bound %d", trial, peak.Load(), concurrency)
		}
	}
	var none *runBudget
	count := 0
	runBoundedAdmitted(context.Background(), 5, 2, none.admitter(), func(_ int, ok bool) {
		if ok {
			count++
		}
	})
	if count != 5 {
		t.Fatalf("a nil budget admitted %d of 5", count)
	}
}

// A broker session that vanishes mid-run fails closed instead of pricing zero.
func TestRunBudgetFailsClosedWhenTheSessionVanishes(t *testing.T) {
	gone := false
	b := &runBudget{benchVersion: protocol.BenchVersionV18, cases: 4,
		meter: func() (scoregates.PricedCostUsage, bool) {
			return scoregates.PricedCostUsage{ChatComplete: true}, !gone
		}}
	b.budget, _ = scoregates.RunBudgetNanoUSD(protocol.BenchVersionV18, 4)
	if !b.admit() {
		t.Fatal("first case refused")
	}
	gone = true
	if b.admit() || b.admit() {
		t.Fatal("admitted after the session vanished")
	}
	if got := b.summary(); !got.Exhausted || got.AdmittedCases != 1 {
		t.Fatalf("summary %+v", got)
	}
}

// Budget-refused cases leave the signed gate inputs (and their transcripts)
// but nothing else; below v18 the inputs are untouched.
func TestV18GatedEvidenceDropsOnlyRefusedCases(t *testing.T) {
	perCase := []protocol.CaseScore{{CaseID: "a", Score: 1}, budgetExhaustedCase("b", "x", protocol.KindMemory), {CaseID: "c"}}
	transcripts := []transcriptCase{{CaseID: "a"}, {CaseID: "b"}, {CaseID: "c"}}
	gotCases, gotTranscripts := v18GatedEvidence(protocol.BenchVersionV18, perCase, transcripts)
	if len(gotCases) != 2 || len(gotTranscripts) != 2 || gotCases[1].CaseID != "c" || gotTranscripts[1].CaseID != "c" {
		t.Fatalf("cases %+v transcripts %+v", gotCases, gotTranscripts)
	}
	plain := []protocol.CaseScore{{CaseID: "a"}, {CaseID: "c"}}
	if c, tr := v18GatedEvidence(protocol.BenchVersionV17, plain, transcripts); len(c) != 2 || len(tr) != 3 {
		t.Fatal("v17 evidence changed")
	}
}
