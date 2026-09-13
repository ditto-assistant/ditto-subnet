package scoregates

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestCostBudgetsArePublishedPerClass(t *testing.T) {
	budgets := CostBudgets()
	want := map[string]int{"memory": 3, "single_tool": 3, "tool_chain": 5}
	if len(budgets) != len(want) {
		t.Fatalf("budgets = %+v", budgets)
	}
	for _, b := range budgets {
		if want[b.Class] != b.Completions {
			t.Fatalf("class %s budget %d completions, want %d", b.Class, b.Completions, want[b.Class])
		}
		if b.OutputTokens != uint64(b.Completions)*CostCompletionEquivalentTokens {
			t.Fatalf("class %s token budget %d is not completions x equivalent", b.Class, b.OutputTokens)
		}
	}
	// The acceptance floors: >= 3 single-tool and >= 5 chain completions.
	if CostBudgetCompletions(CostClassSingleTool) < 3 || CostBudgetCompletions(CostClassToolChain) < 5 {
		t.Fatal("published budgets fall below the issue #1850 floors")
	}
}

func TestCostCaseClassFor(t *testing.T) {
	if got := CostCaseClassFor(protocol.KindMemory, 0); got != CostClassMemory {
		t.Fatalf("memory = %s", got)
	}
	if got := CostCaseClassFor(protocol.KindTool, 1); got != CostClassSingleTool {
		t.Fatalf("single tool = %s", got)
	}
	if got := CostCaseClassFor(protocol.KindTool, 0); got != CostClassSingleTool {
		t.Fatalf("restraint tool case = %s", got)
	}
	if got := CostCaseClassFor(protocol.KindTool, 3); got != CostClassToolChain {
		t.Fatalf("chain = %s", got)
	}
}

func TestCostFactorRule(t *testing.T) {
	budget := CostBudgetTokens(CostClassSingleTool)
	cases := []struct {
		tokens uint64
		want   int
		excess uint64
	}{
		{0, BasisPointScale, 0},
		{budget, BasisPointScale, 0},
		{budget + budget/4, 9000, budget / 4},
		{budget + budget/2, 8000, budget / 2},
		{2 * budget, CostFactorFloorBPS, budget},
		{10 * budget, CostFactorFloorBPS, 9 * budget},
	}
	for _, tc := range cases {
		got, excess := CostFactorBPS(CostClassSingleTool, tc.tokens)
		if got != tc.want || excess != tc.excess {
			t.Fatalf("tokens %d: factor=%d excess=%d, want %d/%d", tc.tokens, got, excess, tc.want, tc.excess)
		}
	}
	// A chain has more room than a single-tool case at the same spend.
	single, _ := CostFactorBPS(CostClassSingleTool, 2000)
	chain, _ := CostFactorBPS(CostClassToolChain, 2000)
	if !(chain > single) {
		t.Fatalf("chain factor %d not above single-tool factor %d", chain, single)
	}
}

func TestBuildInferenceCostIsVersionGatedAndShadow(t *testing.T) {
	record := &InferenceCostRecord{Completions: 4, ChoicesTotal: 12, OutputTokens: 4000, Attribution: CostAttributionSerialRunCase}
	for _, version := range []int{BenchVersionV9, BenchVersionV10, BenchVersionV11, BenchVersionV12} {
		if got := BuildInferenceCost(version, CostClassMemory, record); got != nil {
			t.Fatalf("v%d produced cost evidence: %+v", version, got)
		}
		if got := SummarizeInferenceCost(version, nil, InferenceCostRecord{}); got != nil {
			t.Fatalf("v%d produced a cost summary: %+v", version, got)
		}
	}
	got := BuildInferenceCost(BenchVersionV13, CostClassMemory, record)
	if got == nil || !got.Attributed || got.Attribution != CostAttributionSerialRunCase {
		t.Fatalf("attributed record = %+v", got)
	}
	if got.Completions != 4 || got.ChoicesTotal != 12 || got.OutputTokens != 4000 || got.BudgetTokens != CostBudgetTokens(CostClassMemory) {
		t.Fatalf("record not carried: %+v", got)
	}
	if got.FactorBPS != CostFactorFloorBPS || got.ExcessTokens != 4000-CostBudgetTokens(CostClassMemory) {
		t.Fatalf("n=3 sampling over budget did not floor: %+v", got)
	}
	if err := ValidateInferenceCost(*got); err != nil {
		t.Fatal(err)
	}
	unattributed := BuildInferenceCost(BenchVersionV13, CostClassToolChain, &InferenceCostRecord{Completions: 9, OutputTokens: 99999})
	if unattributed.Attributed || unattributed.FactorBPS != BasisPointScale || unattributed.Completions != 0 || unattributed.Attribution != CostAttributionUnavailable {
		t.Fatalf("unattributed bookings leaked onto the case: %+v", unattributed)
	}
	if err := ValidateInferenceCost(*unattributed); err != nil {
		t.Fatal(err)
	}
	if nilRecord := BuildInferenceCost(BenchVersionV13, CostClassMemory, nil); nilRecord == nil || nilRecord.Attributed || nilRecord.FactorBPS != BasisPointScale {
		t.Fatalf("nil record = %+v", nilRecord)
	}
}

func TestSummarizeInferenceCostIsShadowAndAggregates(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "a", InferenceCost: &protocol.InferenceCostEvidence{Attributed: true, Attribution: CostAttributionSerialRunCase, Completions: 2, ChoicesTotal: 2, OutputTokens: 300, FactorBPS: BasisPointScale}},
		{CaseID: "b", InferenceCost: &protocol.InferenceCostEvidence{Attributed: true, Attribution: CostAttributionCaseCapability, Completions: 5, ChoicesTotal: 25, OutputTokens: 9000, FactorBPS: CostFactorFloorBPS}},
		{CaseID: "c", InferenceCost: &protocol.InferenceCostEvidence{Attribution: CostAttributionUnavailable, FactorBPS: BasisPointScale}},
		{CaseID: "d"},
	}
	got := SummarizeInferenceCost(BenchVersionV13, perCase, InferenceCostRecord{Completions: 7, ChoicesTotal: 7, OutputTokens: 1234})
	if got.Posture != "shadow" || got.Applied {
		t.Fatalf("summary is not shadow: %+v", got)
	}
	if got.Cases != 3 || got.AttributedCases != 2 || got.CasesBelowFullFactor != 1 {
		t.Fatalf("counts = %+v", got)
	}
	if got.Completions != 7 || got.ChoicesTotal != 27 || got.OutputTokens != 9300 {
		t.Fatalf("attributed totals = %+v", got)
	}
	if got.UnattributedCompletions != 7 || got.UnattributedOutputTokens != 1234 {
		t.Fatalf("unattributed totals = %+v", got)
	}
	if got.MeanFactorBPS != (BasisPointScale+CostFactorFloorBPS+BasisPointScale)/3 {
		t.Fatalf("mean factor = %d", got.MeanFactorBPS)
	}
	if len(got.Budgets) != 3 || got.FloorBPS != CostFactorFloorBPS || got.CompletionEquivalentTokens != CostCompletionEquivalentTokens {
		t.Fatalf("published constants missing: %+v", got)
	}
	empty := SummarizeInferenceCost(BenchVersionV13, nil, InferenceCostRecord{})
	if empty.Cases != 0 || empty.MeanFactorBPS != BasisPointScale {
		t.Fatalf("empty summary = %+v", empty)
	}
}

func TestValidateInferenceCostRejectsInconsistentRecords(t *testing.T) {
	bad := []protocol.InferenceCostEvidence{
		{Class: "memory", FactorBPS: 5000},
		{Class: "memory", FactorBPS: BasisPointScale, Completions: 1},
		{Class: "memory", Attributed: true, Attribution: CostAttributionSerialRunCase, Completions: 3, ChoicesTotal: 1, FactorBPS: BasisPointScale},
		{Class: "memory", Attributed: true, Attribution: CostAttributionSerialRunCase, Completions: 1, ChoicesTotal: 1, OutputTokens: 99999, FactorBPS: BasisPointScale},
	}
	for i, e := range bad {
		if err := ValidateInferenceCost(e); err == nil {
			t.Fatalf("record %d accepted: %+v", i, e)
		}
	}
}
