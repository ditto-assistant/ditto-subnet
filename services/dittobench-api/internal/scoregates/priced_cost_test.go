package scoregates

import (
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

var updatePricedCost = flag.Bool("update-priced-cost", false, "rewrite testdata/priced_cost_vectors.json")

const pricedCostVectorsPath = "testdata/priced_cost_vectors.json"

// PricedCostVector is one shared Go/Python vector: the inputs and the exact
// block BuildPricedCost reports for them. ditto_screening_protocol reads the
// same file (tests/test_priced_cost.py).
type PricedCostVector struct {
	Name         string                      `json:"name"`
	BenchVersion int                         `json:"bench_version"`
	Posture      string                      `json:"posture"`
	Cases        int                         `json:"cases"`
	ChatComplete bool                        `json:"chat_complete"`
	Source       string                      `json:"source"`
	Usage        pricedCostVectorUsage       `json:"usage"`
	Summary      *protocol.PricedCostSummary `json:"summary"`
}

type pricedCostVectorUsage struct {
	PromptTokens         uint64 `json:"prompt_tokens"`
	CompletionTokens     uint64 `json:"completion_tokens"`
	EmbeddingTokens      uint64 `json:"embedding_tokens"`
	DecisionCalls        uint64 `json:"decision_calls"`
	DecisionInputTokens  uint64 `json:"decision_input_tokens"`
	DecisionOutputTokens uint64 `json:"decision_output_tokens"`
}

type PricedCostVectors struct {
	ContractID string             `json:"contract_id"`
	Vectors    []PricedCostVector `json:"vectors"`
	// Factors pins the bare curve at representative ratios.
	Factors []protocol.PricedCostCurvePoint `json:"factors"`
}

func buildPricedCostVectors() PricedCostVectors {
	type in struct {
		name     string
		version  int
		posture  string
		cases    int
		complete bool
		usage    pricedCostVectorUsage
	}
	inputs := []in{
		// v15 fleet shapes (350-case runs), priced at the locked rates.
		{"v15_pruned_catalog_cheapest", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 12_457_068, CompletionTokens: 677_405, EmbeddingTokens: 294_000}},
		{"v15_pruned_catalog", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 14_147_828, CompletionTokens: 1_332_110, EmbeddingTokens: 294_000}},
		{"v15_fleet_mean", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 17_484_979, CompletionTokens: 1_422_919, EmbeddingTokens: 294_086}},
		{"v15_full_catalog", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 22_318_268, CompletionTokens: 1_690_233, EmbeddingTokens: 294_000}},
		{"v15_full_catalog_dearest", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 26_000_000, CompletionTokens: 3_018_488, EmbeddingTokens: 294_000}},
		{"exactly_reference", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 10_000_000, CompletionTokens: 2_000_000}},
		{"one_and_a_half_reference", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 15_000_000, CompletionTokens: 3_000_000}},
		{"twice_reference_floor", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 20_000_000, CompletionTokens: 4_000_000}},
		{"ten_times_reference_capped", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 200_000_000, CompletionTokens: 40_000_000}},
		{"decision_lane_and_embeddings", 16, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 9_000_000, CompletionTokens: 1_000_000, EmbeddingTokens: 500_000, DecisionCalls: 700, DecisionInputTokens: 3_500_000, DecisionOutputTokens: 70_000}},
		{"dueling_small_run_normalized", 16, "enforce", 35, true, pricedCostVectorUsage{PromptTokens: 1_600_000, CompletionTokens: 140_000, EmbeddingTokens: 30_000}},
		{"odd_case_count_floor_division", 16, "enforce", 349, true, pricedCostVectorUsage{PromptTokens: 17_000_001, CompletionTokens: 1_400_003, EmbeddingTokens: 7}},
		{"shadow_reports_not_applies", 16, "shadow", 350, true, pricedCostVectorUsage{PromptTokens: 22_318_268, CompletionTokens: 1_690_233, EmbeddingTokens: 294_000}},
		{"incomplete_chat_accounting", 16, "enforce", 350, false, pricedCostVectorUsage{PromptTokens: 22_318_268, CompletionTokens: 1_690_233}},
		{"zero_cases", 16, "enforce", 0, true, pricedCostVectorUsage{}},
		{"zero_inference", 16, "enforce", 350, true, pricedCostVectorUsage{}},
		{"saturating_counts", 16, "enforce", 1, true, pricedCostVectorUsage{PromptTokens: MaxUsageCount, CompletionTokens: MaxUsageCount, EmbeddingTokens: MaxUsageCount, DecisionInputTokens: MaxUsageCount}},
		{"v17_inherits", 17, "enforce", 350, true, pricedCostVectorUsage{PromptTokens: 14_000_000, CompletionTokens: 1_300_000}},
	}
	out := PricedCostVectors{ContractID: PricedCostContractID}
	for _, v := range inputs {
		summary := BuildPricedCost(v.version, ParsePricedCostPosture(v.posture), v.cases, PricedCostUsage{
			ChatComplete: v.complete, Source: PricedCostSourceRelay,
			PromptTokens: v.usage.PromptTokens, CompletionTokens: v.usage.CompletionTokens,
			EmbeddingTokens: v.usage.EmbeddingTokens, DecisionCalls: v.usage.DecisionCalls,
			DecisionInputTokens: v.usage.DecisionInputTokens, DecisionOutputTokens: v.usage.DecisionOutputTokens,
		})
		out.Vectors = append(out.Vectors, PricedCostVector{
			Name: v.name, BenchVersion: v.version, Posture: v.posture, Cases: v.cases,
			ChatComplete: v.complete, Source: PricedCostSourceRelay, Usage: v.usage, Summary: summary,
		})
	}
	for _, r := range []int{0, 5000, 10_000, 10_001, 11_000, 12_345, 15_000, 17_500, 19_999, 20_000, 30_000, PricedCostMaxRatioBPS} {
		out.Factors = append(out.Factors, protocol.PricedCostCurvePoint{RatioBPS: r, FactorBPS: PricedCostFactorBPS(r)})
	}
	return out
}

func TestPricedCostVectorsGolden(t *testing.T) {
	got := buildPricedCostVectors()
	raw, err := json.MarshalIndent(got, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	raw = append(raw, '\n')
	if *updatePricedCost {
		if err := os.WriteFile(pricedCostVectorsPath, raw, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(pricedCostVectorsPath)
	if err != nil {
		t.Fatalf("%v (run go test ./internal/scoregates -run TestPricedCostVectorsGolden -update-priced-cost)", err)
	}
	if !bytes.Equal(want, raw) {
		t.Fatal("priced cost vectors drifted; re-run with -update-priced-cost, review the diff and move the Python mirror with it")
	}
}

func TestPricedCostIsVersionGated(t *testing.T) {
	usage := PricedCostUsage{ChatComplete: true, PromptTokens: 30_000_000, CompletionTokens: 3_000_000}
	for v := BenchVersionV9; v < protocol.PricedCostMinBenchVersion; v++ {
		if got := BuildPricedCost(v, PricedCostEnforce, 350, usage); got != nil {
			t.Fatalf("bench %d priced its run: %+v", v, got)
		}
	}
	if got := PricedCostMultiplier(nil); got != 1 {
		t.Fatalf("nil multiplier = %v", got)
	}
}

func TestPricedCostCurveShape(t *testing.T) {
	cases := []struct{ ratio, want int }{
		{0, 10_000}, {10_000, 10_000}, {12_500, 9_000}, {15_000, 8_000},
		{17_500, 7_000}, {20_000, 6_000}, {25_000, 6_000}, {PricedCostMaxRatioBPS, 6_000},
	}
	for _, tc := range cases {
		if got := PricedCostFactorBPS(tc.ratio); got != tc.want {
			t.Fatalf("ratio %d: factor %d, want %d", tc.ratio, got, tc.want)
		}
	}
	prev := BasisPointScale
	for r := 0; r <= 25_000; r += 7 {
		f := PricedCostFactorBPS(r)
		if f > prev || f < PricedCostFloorBPS {
			t.Fatalf("curve not monotone non-increasing within [floor, 1] at ratio %d: %d after %d", r, f, prev)
		}
		prev = f
	}
}

func TestPricedCostPostures(t *testing.T) {
	usage := PricedCostUsage{ChatComplete: true, Source: PricedCostSourceRelay, PromptTokens: 22_000_000, CompletionTokens: 1_700_000}
	enforced := BuildPricedCost(16, PricedCostEnforce, 350, usage)
	shadow := BuildPricedCost(16, PricedCostShadow, 350, usage)
	if !enforced.Applied || enforced.AppliedFactorBPS != enforced.FactorBPS || enforced.FactorBPS >= BasisPointScale {
		t.Fatalf("enforce: %+v", enforced)
	}
	if shadow.Applied || shadow.AppliedFactorBPS != BasisPointScale || shadow.FactorBPS != enforced.FactorBPS {
		t.Fatalf("shadow: %+v", shadow)
	}
	for _, raw := range []string{"", "enforce", "ENFORCE", "bogus"} {
		if ParsePricedCostPosture(raw) != PricedCostEnforce {
			t.Fatalf("posture %q is not the enforce default", raw)
		}
	}
	if ParsePricedCostPosture(" Shadow ") != PricedCostShadow {
		t.Fatal("shadow not parsed")
	}
	incomplete := BuildPricedCost(16, PricedCostEnforce, 350, PricedCostUsage{PromptTokens: 99_000_000})
	if incomplete.Result != PricedCostResultInsufficientEvidence || incomplete.AppliedFactorBPS != BasisPointScale || incomplete.Applied {
		t.Fatalf("incomplete accounting must apply the full factor: %+v", incomplete)
	}
}

func TestValidatePricedCostRoundTrip(t *testing.T) {
	for _, v := range buildPricedCostVectors().Vectors {
		if v.Summary == nil {
			continue
		}
		if err := ValidatePricedCost(v.BenchVersion, *v.Summary); err != nil {
			t.Fatalf("%s: %v", v.Name, err)
		}
		tampered := *v.Summary
		tampered.AppliedFactorBPS = BasisPointScale + 1
		if err := ValidatePricedCost(v.BenchVersion, tampered); err == nil {
			t.Fatalf("%s: tampered factor accepted", v.Name)
		}
	}
}
