package privatesurface

import (
	"context"
	"errors"
	"math"
	"sync"
	"sync/atomic"
	"testing"
)

func TestSpendBudgetConcurrentAdmissionAndUnknownCharges(t *testing.T) {
	b := &spendBudget{state: BudgetSnapshot{LimitUSD: 10}, checkpoint: func(BudgetSnapshot) error { return nil }}
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, _ = b.reserve(2000) }()
	}
	wg.Wait()
	if b.state.Requests != 6 || b.state.ChargedUSD > 10 {
		t.Fatalf("over-admission: %+v", b.state)
	}
	// No settlement is a failed/unknown call: all six reservations still count.
	if b.state.ReportedUSD != 0 || b.state.ChargedUSD < 8 {
		t.Fatalf("lost unknown charges: %+v", b.state)
	}
}

func TestFactVerifierRouteReservesFullReasoningBound(t *testing.T) {
	p := Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "openai/gpt-5.4-mini", ValidatorProvider: "azure", ValidatorReasoning: "medium"}
	c, err := NewClient(p, "test")
	if err != nil {
		t.Fatal(err)
	}
	if err := c.EnableBudget(4, func(BudgetSnapshot) error { return nil }); err != nil {
		t.Fatal(err)
	}
	reserved, err := c.budget.reserve(1000)
	if err != nil || reserved < 2.56 {
		t.Fatal("reasoning output bound not reserved")
	}
	if _, err := c.budget.reserve(1000); err == nil {
		t.Fatal("concurrent reservation exceeds cap")
	}
	if !exactFactIdentity(CompletionReceipt{Model: "openai/gpt-5.4-mini", Provider: "Azure"}, p.ValidatorModel) || exactFactIdentity(CompletionReceipt{Model: "openai/gpt-5.4-mini", Provider: "OpenAI"}, p.ValidatorModel) {
		t.Fatal("verifier identity not exact")
	}
}

func TestSpendBudgetKnownChargesAndFailClosedAccounting(t *testing.T) {
	for _, cost := range []*float64{nil, ptrCost(-1), ptrCost(math.NaN()), ptrCost(math.Inf(1)), ptrCost(99)} {
		b := &spendBudget{state: BudgetSnapshot{LimitUSD: 10}, checkpoint: func(BudgetSnapshot) error { return nil }}
		reserved, _ := b.reserve(2000)
		if err := b.settle(reserved, cost); err == nil {
			t.Fatal("invalid receipt accepted")
		}
		if _, err := b.reserve(1); err == nil {
			t.Fatal("continued after invalid billing")
		}
		if (cost == nil || math.IsNaN(*cost) || math.IsInf(*cost, 0) || *cost < 0) && b.state.ChargedUSD != reserved {
			t.Fatal("refunded unknown billing")
		}
	}
	b := &spendBudget{state: BudgetSnapshot{LimitUSD: 10}, checkpoint: func(BudgetSnapshot) error { return nil }}
	r, _ := b.reserve(2000)
	if err := b.settle(r, ptrCost(.001)); err != nil {
		t.Fatal(err)
	}
	if math.Abs(b.state.ChargedUSD-.001) > 1e-9 || b.state.ReportedUSD != .001 {
		t.Fatalf("bad settlement: %+v", b.state)
	}
}

func ptrCost(n float64) *float64 { return &n }

func TestBudgetPersistsBeforeCallingAndCapsProviderPrices(t *testing.T) {
	var snapshot BudgetSnapshot
	var mu sync.Mutex
	var calls atomic.Int32
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		n := calls.Add(1)
		mu.Lock()
		s := snapshot
		mu.Unlock()
		if s.Requests != int(n) || s.ChargedUSD < 1 {
			t.Fatal("request preceded reservation")
		}
		prices := request["provider"].(map[string]any)["max_price"].(map[string]any)
		if prices["prompt"] != float64(20) || prices["completion"] != float64(20) {
			t.Fatal("missing provider price ceilings")
		}
		return 200, completion(`{"text":null}`)
	})
	c.profile = Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	if err := c.EnableBudget(10, func(s BudgetSnapshot) error { mu.Lock(); snapshot = s; mu.Unlock(); return nil }); err != nil {
		t.Fatal(err)
	}
	_, _, err := c.complete(context.Background(), c.profile.RewriteModel, c.profile.RewriteProvider, "system", "input", "text", "string", 0)
	if err != nil || calls.Load() != 1 || snapshot.ReportedUSD != .001 {
		t.Fatalf("completion: %v %+v", err, snapshot)
	}
	c.budget.checkpoint = func(BudgetSnapshot) error { return errors.New("disk full") }
	_, _, err = c.complete(context.Background(), c.profile.RewriteModel, c.profile.RewriteProvider, "system", "input", "text", "string", 0)
	if err == nil || calls.Load() != 1 {
		t.Fatal("called provider after checkpoint failure")
	}
}

func TestBudgetRejectsUnreviewedRoutesAndInvalidLimits(t *testing.T) {
	c, _ := NewClient(testProfile(), "key")
	if err := c.EnableBudget(10, func(BudgetSnapshot) error { return nil }); err == nil {
		t.Fatal("unbounded route allowed")
	}
	for _, limit := range []float64{0, -1, 1001, math.NaN(), math.Inf(1)} {
		if err := c.EnableBudget(limit, func(BudgetSnapshot) error { return nil }); err == nil {
			t.Fatal("invalid limit allowed")
		}
	}
}
