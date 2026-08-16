package datagen

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestGenerateForVersionUsesExplicitV9Stream(t *testing.T) {
	const seed = int64(424242)
	v8, err := GenerateForVersion(seed, 30, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	v9, err := GenerateForVersion(seed, 30, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	v9Again, err := GenerateForVersion(seed, 30, protocol.BenchVersionV9)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(v9, v9Again) {
		t.Fatal("v9 generation is not deterministic")
	}
	if reflect.DeepEqual(v8, v9) {
		t.Fatal("v8 and v9 generation streams unexpectedly match")
	}
	v9Epoch, _ := protocol.DatasetEpochForVersion(protocol.BenchVersionV9)
	if v9.GeneratedAt != v9Epoch.Format("2006-01-02T15:04:05Z07:00") {
		t.Fatalf("v9 epoch = %q, want %q", v9.GeneratedAt, v9Epoch)
	}
	if _, err := GenerateForVersion(seed, 30, protocol.BenchVersionV11+1); err == nil {
		t.Fatal("unsupported benchmark version accepted")
	}
}

// TestDeterministicPerSeed: same seed yields byte-identical datasets. With
// seed-derived time the GeneratedAt envelope is the pinned dataset epoch, not
// a wall clock, so it is part of the reproducible dataset too.
func TestDeterministicPerSeed(t *testing.T) {
	a := Generate(42, 30)
	b := Generate(42, 30)

	if a.GeneratedAt != b.GeneratedAt || a.GeneratedAt != protocol.DatasetEpochRFC3339 {
		t.Fatalf("GeneratedAt must be the pinned epoch, deterministic per seed: a=%q b=%q want=%q",
			a.GeneratedAt, b.GeneratedAt, protocol.DatasetEpochRFC3339)
	}
	if len(a.ToolCases) != len(b.ToolCases) {
		t.Fatalf("len mismatch: %d vs %d", len(a.ToolCases), len(b.ToolCases))
	}
	for i := range a.ToolCases {
		ca, cb := a.ToolCases[i], b.ToolCases[i]
		if ca.ID != cb.ID || ca.Category != cb.Category || ca.Prompt != cb.Prompt {
			t.Fatalf("case %d differs for same seed:\n  a=%+v\n  b=%+v", i, ca, cb)
		}
		if len(ca.ExpectedTools) != len(cb.ExpectedTools) {
			t.Fatalf("case %d expected-tools differ", i)
		}
		for j := range ca.ExpectedTools {
			if ca.ExpectedTools[j].Name != cb.ExpectedTools[j].Name {
				t.Fatalf("case %d tool %d differs", i, j)
			}
		}
	}
}

// TestVarietyAcrossSeeds: different seeds produce different datasets.
func TestVarietyAcrossSeeds(t *testing.T) {
	a := Generate(1, 30)
	b := Generate(2, 30)

	same := 0
	for i := range a.ToolCases {
		if a.ToolCases[i].Prompt == b.ToolCases[i].Prompt &&
			a.ToolCases[i].Category == b.ToolCases[i].Category {
			same++
		}
	}
	if same == len(a.ToolCases) {
		t.Fatalf("seeds 1 and 2 produced identical datasets")
	}
}

// TestClampAndShape: n is clamped and cases are well-formed.
func TestClampAndShape(t *testing.T) {
	if got := len(Generate(7, 0).ToolCases); got != 1 {
		t.Fatalf("n=0 should clamp to 1, got %d", got)
	}
	if got := len(Generate(7, 500).ToolCases); got != 200 {
		t.Fatalf("n=500 should clamp to 200, got %d", got)
	}

	ds := Generate(99, 60)
	for _, c := range ds.ToolCases {
		if c.ID == "" || c.Category == "" || c.Prompt == "" {
			t.Fatalf("malformed case: %+v", c)
		}
		// no-tool categories (chit-chat, abstention, missing-arg) must expect no tools
		noTool := c.Category == "no_tool" || c.Category == "abstention" || c.Category == "arg_hallucination"
		if noTool && len(c.ExpectedTools) != 0 {
			t.Fatalf("category %s should expect no tools: %+v", c.Category, c)
		}
		// tool categories expect >=1 tool, and MaxToolCalls tracks the sequence
		// length (1 for single-hop, >1 for multi-hop trajectories).
		if !noTool {
			if len(c.ExpectedTools) < 1 {
				t.Fatalf("category %s should expect a tool: %+v", c.Category, c)
			}
			if c.MaxToolCalls != len(c.ExpectedTools) {
				t.Fatalf("category %s: MaxToolCalls %d != len(ExpectedTools) %d", c.Category, c.MaxToolCalls, len(c.ExpectedTools))
			}
		}
	}
}

// TestArgHallucinationIsNoTool: the missing-argument trap emits no-expected-tool
// cases whose expected behavior is to ask rather than fabricate, so the
// no-tool scoring path (any tool call ⇒ 0) probes hallucinated arguments.
func TestArgHallucinationIsNoTool(t *testing.T) {
	ds := Generate(42, len(categories)*3)
	seen := 0
	for _, c := range ds.ToolCases {
		if c.Category != "arg_hallucination" {
			continue
		}
		seen++
		if len(c.ExpectedTools) != 0 || c.MaxToolCalls != 0 {
			t.Fatalf("arg_hallucination must expect no tools: %+v", c)
		}
		if !strings.Contains(c.ExpectedBehavior, "fabricated argument") {
			t.Fatalf("arg_hallucination behavior should warn against fabrication: %q", c.ExpectedBehavior)
		}
	}
	if seen == 0 {
		t.Fatal("expected arg_hallucination cases to appear")
	}
}

// TestParallelToolsIsUnordered: the parallel category emits an independent
// two-tool set flagged Unordered so call order is not graded.
func TestParallelToolsIsUnordered(t *testing.T) {
	ds := Generate(11, len(categories)*3)
	seen := 0
	for _, c := range ds.ToolCases {
		if c.Category != "parallel_web_image" {
			continue
		}
		seen++
		if !c.Unordered || len(c.ExpectedTools) != 2 || c.MaxToolCalls != 2 {
			t.Fatalf("parallel_web_image must be an unordered 2-tool case: %+v", c)
		}
	}
	if seen == 0 {
		t.Fatal("expected parallel_web_image cases to appear")
	}
}

// TestCoversCategories: across a decent n, multiple categories appear.
func TestCoversCategories(t *testing.T) {
	ds := Generate(123, 120)
	seen := map[string]bool{}
	for _, c := range ds.ToolCases {
		seen[c.Category] = true
	}
	if len(seen) < 5 {
		t.Fatalf("expected variety of categories, only saw %d: %v", len(seen), seen)
	}
}

// TestGenerateHasMultiHopAndArgCases: multi-hop trajectories and exact-value
// argument ground truth both must actually appear in a dataset.
func TestGenerateHasMultiHopAndArgCases(t *testing.T) {
	ds := Generate(7, 120)
	multiHop, argScored := 0, 0
	for _, c := range ds.ToolCases {
		if len(c.ExpectedTools) > 1 {
			multiHop++
		}
		for _, ts := range c.ExpectedTools {
			if len(ts.RequiredArgs) > 0 {
				argScored++
			}
		}
	}
	if multiHop == 0 {
		t.Fatal("expected some multi-hop (sequence) tool cases")
	}
	if argScored == 0 {
		t.Fatal("expected some cases with required-arg ground truth")
	}
}

// TestStratifiedBalance pins the stratification invariant: with a fixed mix, the
// per-category counts of any dataset differ by at most one (floor/ceil of n/C),
// regardless of seed. This is what removes the multinomial category-draw variance.
func TestStratifiedBalance(t *testing.T) {
	for _, seed := range []int64{1, 2, 999, 123456} {
		ds := Generate(seed, len(categories)*4) // multiple of C → perfectly balanced
		counts := map[string]int{}
		for _, c := range ds.ToolCases {
			counts[c.Category]++
		}
		lo, hi := 1<<30, 0
		for _, n := range counts {
			if n < lo {
				lo = n
			}
			if n > hi {
				hi = n
			}
		}
		if hi-lo > 1 {
			t.Fatalf("seed %d: category counts unbalanced (min=%d max=%d): %v", seed, lo, hi, counts)
		}
	}
}
