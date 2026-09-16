package datagen

import (
	"math/rand"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
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
	if _, err := GenerateForVersion(seed, 30, protocol.NewestSupportedBenchVersion()+1); err == nil {
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

// TestV13AppearanceIntentsAreServedByTheInventory: at v13 the mock
// discover_capabilities inventory is seed-specific, so every set_accent /
// set_font RequiredArgs value must be one the fixture built for the same seed
// serves — otherwise the honest inspect-the-options path cannot solve the case.
// The case keeps the v8 capability-resolution shape.
func TestV13AppearanceIntentsAreServedByTheInventory(t *testing.T) {
	const n = 100 // the full-profile tool count
	checked := map[string]int{}
	for seed := int64(1); seed <= 40; seed++ {
		cases, _ := GenerateCasesWithFillersForVersion(rand.New(rand.NewSource(seed)), seed, n, protocol.BenchVersionV13)
		for _, c := range cases {
			if c.Category != "discovery_accent_set" && c.Category != "discovery_font_set" {
				continue
			}
			inventory, ok := toolexec.BuildFixtureForVersion(seed, c, protocol.BenchVersionV13).Result("discover_capabilities", nil)
			if !ok {
				t.Fatalf("seed %d %s: no discover_capabilities result", seed, c.ID)
			}
			if len(c.ExpectedTools) == 0 || c.ExpectedTools[0].Name != "discover_capabilities" {
				t.Fatalf("seed %d %s: expected tools %+v do not start with discover_capabilities", seed, c.ID, c.ExpectedTools)
			}
			var required string
			for _, spec := range c.ExpectedTools {
				for _, value := range spec.RequiredArgs {
					required = value
				}
			}
			if required == "" {
				t.Fatalf("seed %d %s: no RequiredArgs value", seed, c.ID)
			}
			if !strings.Contains(inventory, required) {
				t.Fatalf("seed %d %s: required %q is not served by inventory %q (prompt %q)", seed, c.ID, required, inventory, c.Prompt)
			}
			// The v8 writing-noise pass may typo the prompt, so the "check the
			// options" shape is asserted through its case semantics.
			if !c.FuzzyTrajectory || !c.AllowExtraTools {
				t.Fatalf("seed %d %s: lost the capability-resolution shape: %+v", seed, c.ID, c)
			}
			checked[c.Category]++
		}
	}
	if checked["discovery_accent_set"] < 20 || checked["discovery_font_set"] < 20 {
		t.Fatalf("too few appearance cases checked: %v", checked)
	}
}
