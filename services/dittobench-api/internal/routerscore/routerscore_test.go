package routerscore

import (
	"encoding/json"
	"math"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// validBaseline builds a non-v7 efficiency baseline whose p90 allowance is
// allowanceTotal total tokens (split prompt/completion), matching
// efficiency.validBaseline's requirements.
func testBaseline(promptAllow, completionAllow uint64) *efficiency.Baseline {
	return &efficiency.Baseline{
		ID:                 "router-test-baseline",
		BenchVersion:       protocol.BenchVersionV5,
		RunSize:            "full",
		Provider:           "router",
		Model:              "router-upstream",
		PromptTokens:       promptAllow,
		CompletionTokens:   completionAllow,
		TotalTokens:        promptAllow + completionAllow,
		Samples:            20,
		Aggregation:        "nearest_rank_p90",
		StarterKitRevision: "router-test",
	}
}

// usage builds a complete, arithmetic-valid TokenUsage for one successful run.
func testUsage(prompt, completion uint64) protocol.TokenUsage {
	return protocol.TokenUsage{
		Status:           "complete",
		Successes:        1,
		UsageAvailable:   1,
		UsageUnavailable: 0,
		PromptTokens:     prompt,
		CompletionTokens: completion,
		TotalTokens:      prompt + completion,
	}
}

func TestFloorGateReviewVsEnforce(t *testing.T) {
	review := ShadowFloorGate()
	enforce := EnforceFloorGate()

	// Not operational never passes, in either mode.
	if review.FloorPass(false, true) {
		t.Error("review: non-operational passed")
	}
	if enforce.FloorPass(false, true) {
		t.Error("enforce: non-operational passed")
	}

	// Operational + floor observed clears in both modes.
	if !review.FloorPass(true, true) {
		t.Error("review: operational+floor did not clear")
	}
	if !enforce.FloorPass(true, true) {
		t.Error("enforce: operational+floor did not clear")
	}

	// The distinguishing case: operational but floor observation FAILED.
	// Shadow (REVIEW) observes without blocking -> clears; enforce blocks.
	if !review.FloorPass(true, false) {
		t.Error("review: floor-fail should still clear (observe-only)")
	}
	if enforce.FloorPass(true, false) {
		t.Error("enforce: floor-fail should block")
	}
}

func TestTokenDominantEffBlendCheaperOutscores(t *testing.T) {
	baseline := testBaseline(1000, 1000) // p90 allowance = 2000 total tokens
	weights := DefaultEffAxisWeights

	// Cheaper run: within the p90 budget -> token multiplier 1.
	cheapTok := TokenScore(testUsage(500, 500), baseline)
	// Pricier run: far above budget -> multiplier < 1.
	priceyTok := TokenScore(testUsage(6000, 6000), baseline)

	if cheapTok <= priceyTok {
		t.Fatalf("token score not monotonic: cheap=%v pricey=%v", cheapTok, priceyTok)
	}

	// With identical latency/retrieval axes, the cheaper run's blended eff must
	// strictly beat the pricier run's, and tokens must dominate the blend.
	cheapEff := weights.Blend(cheapTok, 0.5, 0.5)
	priceyEff := weights.Blend(priceyTok, 0.5, 0.5)
	if cheapEff <= priceyEff {
		t.Fatalf("cheaper run did not outscore pricier: cheap=%v pricey=%v", cheapEff, priceyEff)
	}
	for _, e := range []float64{cheapEff, priceyEff} {
		if e <= 0 || e > 1 {
			t.Fatalf("eff %v out of (0,1]", e)
		}
	}

	// Tokens dominant: a swing on the token axis moves eff more than the same
	// swing on latency.
	tokSwing := weights.Blend(1.0, 0.5, 0.5) - weights.Blend(0.0, 0.5, 0.5)
	latSwing := weights.Blend(0.5, 1.0, 0.5) - weights.Blend(0.5, 0.0, 0.5)
	if tokSwing <= latSwing {
		t.Fatalf("token axis not dominant: tokSwing=%v latSwing=%v", tokSwing, latSwing)
	}
}

func TestBlendClampsToUnitInterval(t *testing.T) {
	w := EffAxisWeights{Tokens: 1, Latency: 1, Retrieval: 1}
	if got := w.Blend(1, 1, 1); got != 1 {
		t.Errorf("Blend over-unity = %v, want clamp to 1", got)
	}
	if got := w.Blend(0, 0, 0); got <= 0 || got > 1 {
		t.Errorf("Blend zero = %v, want strictly in (0,1]", got)
	}
	if got := w.Blend(math.Inf(1), 0, 0); got <= 0 || got > 1 {
		t.Errorf("Blend inf = %v, want strictly in (0,1]", got)
	}
}

func TestWeightedSoftCombineGrokForfeitsOnlyItsSlice(t *testing.T) {
	// Every harness clears with a perfect eff of 1.0 so each slice equals its
	// weight; the full combine is therefore 1.0.
	full := []HarnessOutcome{
		{Harness: routerharness.HarnessClaudeCode, Operational: true, Floor: true, Efficiency: 1},
		{Harness: routerharness.HarnessCodex, Operational: true, Floor: true, Efficiency: 1},
		{Harness: routerharness.HarnessOpencode, Operational: true, Floor: true, Efficiency: 1},
		{Harness: routerharness.HarnessGrok, Operational: true, Floor: true, Efficiency: 1},
	}
	if got := Combine(DefaultHarnessWeights, full); math.Abs(got-1.0) > 1e-9 {
		t.Fatalf("full combine = %v, want 1.0", got)
	}

	// Grok fails (not operational). It must forfeit ONLY its 0.10 slice.
	grokDown := append([]HarnessOutcome(nil), full...)
	grokDown[3].Operational = false
	if got := Combine(DefaultHarnessWeights, grokDown); math.Abs(got-0.90) > 1e-9 {
		t.Fatalf("grok-down combine = %v, want 0.90 (only grok's 0.10 lost)", got)
	}

	// A floor failure forfeits the same way as an operational failure.
	codexFloorFail := append([]HarnessOutcome(nil), full...)
	codexFloorFail[1].Floor = false
	if got := Combine(DefaultHarnessWeights, codexFloorFail); math.Abs(got-0.70) > 1e-9 {
		t.Fatalf("codex-floor-fail combine = %v, want 0.70 (codex 0.30 lost)", got)
	}
}

func TestCombineRangeAndBuildEntry(t *testing.T) {
	outcomes := []HarnessOutcome{
		{Harness: routerharness.HarnessClaudeCode, Operational: true, Floor: true, Efficiency: 0.8, UpstreamTokenCostMicros: 1200},
		{Harness: routerharness.HarnessCodex, Operational: true, Floor: true, Efficiency: 0.9, UpstreamTokenCostMicros: 900},
		{Harness: routerharness.HarnessOpencode, Operational: false, Floor: false, Efficiency: 0},
		{Harness: routerharness.HarnessGrok, Operational: true, Floor: false, Efficiency: 0.5},
	}
	score := Combine(DefaultHarnessWeights, outcomes)
	if score < 0 || score > 1 {
		t.Fatalf("combined score %v out of [0,1]", score)
	}
	// Only claude_code (0.40*0.8) and codex (0.30*0.9) count.
	want := 0.40*0.8 + 0.30*0.9
	if math.Abs(score-want) > 1e-9 {
		t.Fatalf("combined score = %v, want %v", score, want)
	}

	first := time.Date(2026, 9, 7, 12, 0, 0, 0, time.UTC)
	entry := BuildEntry("5Fhotkey", "0f7e3d2c-1111-4222-8333-444455556666", DefaultHarnessWeights, false, first, outcomes)
	if RouterContractVersion != 1 {
		t.Fatalf("RouterContractVersion const = %d, want 1", RouterContractVersion)
	}
	if entry.RouterContractVersion != RouterContractVersion {
		t.Fatalf("router_contract_version = %d, want %d", entry.RouterContractVersion, RouterContractVersion)
	}
	if entry.WeightEligible {
		t.Fatal("shadow entry must not be weight-eligible")
	}
	if math.Abs(entry.CombinedScore-want) > 1e-9 {
		t.Fatalf("entry combined score = %v, want %v", entry.CombinedScore, want)
	}
	if len(entry.Harnesses) != 4 {
		t.Fatalf("entry has %d harness results, want 4", len(entry.Harnesses))
	}
}

func TestLedgerJSONTagsMatchPythonKeys(t *testing.T) {
	entry := BuildEntry(
		"5Fhotkey", "0f7e3d2c-1111-4222-8333-444455556666",
		DefaultHarnessWeights, false,
		time.Date(2026, 9, 7, 12, 0, 0, 0, time.UTC),
		[]HarnessOutcome{{Harness: routerharness.HarnessClaudeCode, Operational: true, Floor: true, Efficiency: 1, UpstreamTokenCostMicros: 42}},
	)
	version := RouterContractVersion
	generated := time.Date(2026, 9, 7, 12, 5, 0, 0, time.UTC)
	ledger := Ledger{
		Entries:               []LedgerEntry{entry},
		RouterContractVersion: &version,
		GeneratedAt:           &generated,
		Count:                 1,
	}
	raw, err := json.Marshal(ledger)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"entries", "router_contract_version", "generated_at", "stale", "count"} {
		if _, ok := decoded[key]; !ok {
			t.Errorf("ledger JSON missing Python key %q", key)
		}
	}
	entries := decoded["entries"].([]any)
	e0 := entries[0].(map[string]any)
	for _, key := range []string{"miner_hotkey", "agent_id", "router_contract_version", "weight_eligible", "combined_score", "harnesses", "first_seen"} {
		if _, ok := e0[key]; !ok {
			t.Errorf("entry JSON missing Python key %q", key)
		}
	}
	h0 := e0["harnesses"].([]any)[0].(map[string]any)
	for _, key := range []string{"harness", "operational", "floor_pass", "efficiency", "upstream_token_cost_micros"} {
		if _, ok := h0[key]; !ok {
			t.Errorf("harness result JSON missing Python key %q", key)
		}
	}
	if h0["harness"] != "claude_code" {
		t.Errorf("harness key = %v, want claude_code", h0["harness"])
	}
}
