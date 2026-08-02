package scorer

import (
	"math"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestFinishTool(t *testing.T) {
	cs := protocol.CaseScore{ToolScore: 0.8, Kind: protocol.KindTool}
	cs = FinishTool(cs)
	// Judge-free: the deterministic trajectory accuracy is the score.
	if cs.Score != 0.8 {
		t.Fatalf("expected score 0.8, got %v", cs.Score)
	}
}

func TestAggregate(t *testing.T) {
	perCase := []protocol.CaseScore{
		{CaseID: "t1", Category: "web_search", Kind: protocol.KindTool, Score: 1.0, LatencyMs: 10},
		{CaseID: "t2", Category: "web_search", Kind: protocol.KindTool, Score: 0.0, LatencyMs: 30},
		{CaseID: "m1", Category: "multi-session", Kind: protocol.KindMemory, Score: 1.0, LatencyMs: 20},
		{CaseID: "m2", Category: "multi-session", Kind: protocol.KindMemory, Score: 0.0, LatencyMs: 40},
	}
	r := Aggregate("run", perCase)

	if r.ToolMean != 0.5 {
		t.Fatalf("tool_mean expected 0.5, got %v", r.ToolMean)
	}
	if r.MemoryMean != 0.5 {
		t.Fatalf("memory_mean expected 0.5, got %v", r.MemoryMean)
	}
	if r.Composite != 0.5 {
		t.Fatalf("composite expected 0.5, got %v", r.Composite)
	}
	if r.N != 4 {
		t.Fatalf("n expected 4, got %d", r.N)
	}
	if r.MedianMs != 25 { // median of 10,30,20,40 = (20+30)/2 = 25
		t.Fatalf("median expected 25, got %d", r.MedianMs)
	}
	if len(r.PerCategory) != 2 {
		t.Fatalf("expected 2 categories, got %d", len(r.PerCategory))
	}
	for _, c := range r.PerCategory {
		if c.Count != 2 || c.Mean != 0.5 {
			t.Fatalf("category %s: count=%d mean=%v", c.Category, c.Count, c.Mean)
		}
	}
}

func TestAggregateCompositeWeighting(t *testing.T) {
	// tool_mean=1.0, memory_mean=0.0 → 0.5*1.0 + 0.5*0.0 = 0.5, pinning the v2
	// (bench_version 2) 0.5/0.5 rebalance: memory is the core product value and
	// the raw-pairs tier makes memory_mean the harder axis.
	r := Aggregate("run", []protocol.CaseScore{
		{CaseID: "t1", Category: "web_search", Kind: protocol.KindTool, Score: 1.0},
		{CaseID: "m1", Category: "multi-session", Kind: protocol.KindMemory, Score: 0.0},
	})
	if r.ToolMean != 1.0 || r.MemoryMean != 0.0 {
		t.Fatalf("means: tool=%v mem=%v", r.ToolMean, r.MemoryMean)
	}
	if r.Composite != 0.5 {
		t.Fatalf("composite expected 0.5 (0.5*1 + 0.5*0), got %v", r.Composite)
	}
}

func TestAggregateMemoryOnly(t *testing.T) {
	r := Aggregate("run", []protocol.CaseScore{
		{Kind: protocol.KindMemory, Score: 1.0, Category: "x"},
	})
	if r.ToolMean != 0 || r.MemoryMean != 1.0 || r.Composite != 1.0 {
		t.Fatalf("memory-only: tool=%v mem=%v comp=%v", r.ToolMean, r.MemoryMean, r.Composite)
	}
}

func TestAggregateToolOnly(t *testing.T) {
	r := Aggregate("run", []protocol.CaseScore{
		{Kind: protocol.KindTool, Score: 1.0, Category: "x"},
	})
	if r.MemoryMean != 0 || r.ToolMean != 1.0 || r.Composite != 1.0 {
		t.Fatalf("tool-only: tool=%v mem=%v comp=%v", r.ToolMean, r.MemoryMean, r.Composite)
	}
}

// TestPerCategoryStdErr: a category with spread reports a positive standard error
// of the mean; a single-case category reports 0; a zero-variance category is 0.
func TestPerCategoryStdErr(t *testing.T) {
	cases := []protocol.CaseScore{
		{Category: "spread", Kind: protocol.KindTool, Score: 0},
		{Category: "spread", Kind: protocol.KindTool, Score: 1},
		{Category: "spread", Kind: protocol.KindTool, Score: 0},
		{Category: "spread", Kind: protocol.KindTool, Score: 1},
		{Category: "flat", Kind: protocol.KindTool, Score: 0.5},
		{Category: "flat", Kind: protocol.KindTool, Score: 0.5},
		{Category: "single", Kind: protocol.KindTool, Score: 1},
	}
	rep := Aggregate("r", cases)
	got := map[string]protocol.CategoryStat{}
	for _, c := range rep.PerCategory {
		got[c.Category] = c
	}
	// mean 0.5 over {0,1,0,1}: sample sd = 0.5773.., se = sd/sqrt(4) = 0.2887..
	if se := got["spread"].StdErr; se < 0.28 || se > 0.29 {
		t.Fatalf("spread std_err expected ~0.2887, got %v", se)
	}
	if se := got["flat"].StdErr; se != 0 {
		t.Fatalf("zero-variance category std_err must be 0, got %v", se)
	}
	if se := got["single"].StdErr; se != 0 {
		t.Fatalf("single-case category std_err must be 0, got %v", se)
	}
}

func TestCompositeStderrCombinesHalves(t *testing.T) {
	perCase := []protocol.CaseScore{
		{Kind: protocol.KindTool, Category: "web_search", Score: 1.0},
		{Kind: protocol.KindTool, Category: "web_search", Score: 0.0},
		{Kind: protocol.KindTool, Category: "web_search", Score: 0.5},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.9},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.2},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.6},
	}
	rep := Aggregate("run", perCase)
	if rep.CompositeStderr <= 0 {
		t.Fatalf("expected a positive composite stderr, got %v", rep.CompositeStderr)
	}
	flat := []protocol.CaseScore{
		{Kind: protocol.KindTool, Category: "web_search", Score: 0.5},
		{Kind: protocol.KindTool, Category: "web_search", Score: 0.5},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.5},
		{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.5},
	}
	if se := Aggregate("run", flat).CompositeStderr; se != 0 {
		t.Fatalf("zero-spread run should have 0 stderr, got %v", se)
	}
}

// The reported composite stderr must be the SE of the GATED composite: a gate
// factor below 1 scales the stderr by the same factor it scales the composite,
// so the KOTH indifference band is not widened by the SE of the ungated mean.
// Two runs with identical per-case scores that differ only in the canary note
// (a clean run, gate ×1.0, vs a leak, gate ×0.5) isolate the gate: their ungated
// SE is the same, so each run's stderr must equal that gate factor times the
// ungated SE, and the leaked run's stderr must be lower. (An honest canary miss
// no longer applies a gate factor after the de-inversion, so a clean run is the
// ×1.0 comparator.)
func TestCompositeStderrScaledByGateFactor(t *testing.T) {
	withNote := func(note string) []protocol.CaseScore {
		return []protocol.CaseScore{
			{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.9},
			{Kind: protocol.KindMemory, Category: "single-session-recall", Score: 0.6},
			{Kind: protocol.KindMemory, Category: "memory-canary", Score: 0.2,
				Notes: func() []string {
					if note == "" {
						return nil
					}
					return []string{note}
				}()},
		}
	}
	clean := withNote("")            // honest canary miss, no gate: factor 1.0
	leak := withNote(canaryLeakNote) // canary leak: factor 0.50
	fClean := CanaryIntegrityFactor(clean)
	fLeak := CanaryIntegrityFactor(leak)
	if fClean != 1.0 || fLeak != canaryLeakPenalty {
		t.Fatalf("canary factors: clean=%v (want 1.0), leak=%v (want %v)",
			fClean, fLeak, canaryLeakPenalty)
	}
	// Pinned to the frozen v5 contract: Aggregate follows CurrentBenchVersion,
	// and v7 deepens the leak penalty to 0.25 (asserted in v7_test.go); this
	// test isolates the SE-shares-the-gate identity under the historical 0.5.
	repClean := AggregateForVersion("run", clean, protocol.BenchVersionV5)
	repLeak := AggregateForVersion("run", leak, protocol.BenchVersionV5)
	if repClean.CompositeStderr <= 0 || repLeak.CompositeStderr <= 0 {
		t.Fatalf("expected positive stderrs, got clean=%v leak=%v",
			repClean.CompositeStderr, repLeak.CompositeStderr)
	}
	// Recover the ungated SE from each (stderr = gate * ungated) and require they
	// agree, then require the leaked run's stderr is strictly the smaller.
	ungatedClean := repClean.CompositeStderr / fClean
	ungatedLeak := repLeak.CompositeStderr / fLeak
	// Tolerance accommodates round6 on the stored stderr divided by the 0.5 gate.
	if math.Abs(ungatedClean-ungatedLeak) > 5e-6 {
		t.Fatalf("ungated SE should match across identical scores: %v vs %v",
			ungatedClean, ungatedLeak)
	}
	if repLeak.CompositeStderr >= repClean.CompositeStderr {
		t.Fatalf("leak stderr %v must be below clean stderr %v (harder gate)",
			repLeak.CompositeStderr, repClean.CompositeStderr)
	}
}
