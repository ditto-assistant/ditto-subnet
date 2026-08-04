package main

import (
	"math"
	"testing"
)

func approx(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// A benchmark whose entire spread comes from which seed you drew (both
// categories move together, run to run) must be flagged seed-dominated.
func TestSeedDominated(t *testing.T) {
	runs := []run{
		{Seed: 1, PerCase: []perCase{{Category: "a", Score: 0}, {Category: "b", Score: 0}}},
		{Seed: 2, PerCase: []perCase{{Category: "a", Score: 1}, {Category: "b", Score: 1}}},
	}
	rep := analyze(runs)
	if rep.DominantFacet != "seed" {
		t.Fatalf("dominant facet = %q, want seed", rep.DominantFacet)
	}
	if rep.Variance.Item != 0 {
		t.Fatalf("item component = %v, want 0 (categories never differ within a seed)", rep.Variance.Item)
	}
}

// A benchmark whose spread comes from which category you're in (easy always
// passes, hard always fails, regardless of seed) must be flagged item-dominated.
func TestItemDominated(t *testing.T) {
	runs := []run{
		{Seed: 1, PerCase: []perCase{{Category: "easy", Score: 1}, {Category: "hard", Score: 0}}},
		{Seed: 2, PerCase: []perCase{{Category: "easy", Score: 1}, {Category: "hard", Score: 0}}},
	}
	rep := analyze(runs)
	if rep.DominantFacet != "item" {
		t.Fatalf("dominant facet = %q, want item", rep.DominantFacet)
	}
	if rep.Variance.Seed != 0 {
		t.Fatalf("seed component = %v, want 0 (seeds never differ)", rep.Variance.Seed)
	}
}

// Saturated (everyone passes) and floor (everyone fails) categories carry no
// discrimination and must be flagged so they can be retired.
func TestSaturationFlags(t *testing.T) {
	runs := []run{
		{Seed: 1, PerCase: []perCase{{Category: "trivial", Score: 1}, {Category: "impossible", Score: 0}, {Category: "mixed", Score: 1}}},
		{Seed: 2, PerCase: []perCase{{Category: "trivial", Score: 1}, {Category: "impossible", Score: 0}, {Category: "mixed", Score: 0}}},
	}
	rep := analyze(runs)
	flags := map[string]string{}
	for _, c := range rep.Categories {
		flags[c.Category] = c.Flag
	}
	if flags["trivial"] != "saturated" {
		t.Errorf("trivial flag = %q, want saturated", flags["trivial"])
	}
	if flags["impossible"] != "floor" {
		t.Errorf("impossible flag = %q, want floor", flags["impossible"])
	}
	if flags["mixed"] != "" {
		t.Errorf("mixed flag = %q, want empty", flags["mixed"])
	}
}

func TestBetweenGroupZeroWhenNoSpread(t *testing.T) {
	groups := [][]float64{{0.5, 0.5}, {0.5, 0.5}}
	if got := betweenGroup(groups, 0.5); !approx(got, 0) {
		t.Fatalf("betweenGroup = %v, want 0", got)
	}
}

// TestParserSignatureGap: a parser-like harness aces plain recall and collapses
// on synthesis/trap families, producing a large trap-gap and the parser-like
// flag; a reasoner that degrades gracefully does not.
func TestParserSignatureGap(t *testing.T) {
	runs := []run{
		// parser: 1.0 on recall, ~0 on synthesis
		{Seed: 1, Harness: "parser", PerCase: []perCase{
			{Category: "single-session-recall", Score: 1}, {Category: "preference", Score: 1},
			{Category: "aggregation-count", Score: 0}, {Category: "temporal-reasoning", Score: 0},
		}},
		{Seed: 2, Harness: "parser", PerCase: []perCase{
			{Category: "single-session-recall", Score: 1}, {Category: "preference", Score: 1},
			{Category: "computed-answer", Score: 0}, {Category: "abstention", Score: 0},
		}},
		// reasoner: high but graceful across both
		{Seed: 1, Harness: "reasoner", PerCase: []perCase{
			{Category: "single-session-recall", Score: 0.9}, {Category: "preference", Score: 0.9},
			{Category: "aggregation-count", Score: 0.8}, {Category: "temporal-reasoning", Score: 0.7},
		}},
	}
	rep := analyze(runs)
	sig := map[string]HarnessSignal{}
	for _, h := range rep.ParserSignals.Harnesses {
		sig[h.Harness] = h
	}
	p, ok := sig["parser"]
	if !ok || p.Flag != "parser-like" {
		t.Fatalf("parser should be flagged parser-like, got %+v", p)
	}
	if p.TrapGap < 0.9 {
		t.Fatalf("parser trap-gap should be ~1.0, got %v", p.TrapGap)
	}
	r, ok := sig["reasoner"]
	if !ok || r.Flag == "parser-like" {
		t.Fatalf("reasoner should NOT be flagged, got %+v", r)
	}
}
