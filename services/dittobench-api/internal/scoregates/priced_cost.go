package scoregates

import (
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v16 priced inference cost factor (details.priced_cost).
//
// The v13 inference_cost gate counts answer OUTPUT tokens per case and stays
// in shadow. On the locked model about 85% of what a run costs is PROMPT
// tokens (v15: ~17.5M prompt vs ~1.4M completion tokens per 350-case run), so
// a harness that resends a 235-tool catalog on every call paid nothing for it.
// From bench 16 the scorer prices the whole run at the contract's locked
// public per-token rates and multiplies the composite by a steep,
// floored-above-zero factor of the priced cost per case:
//
//	cost       = 70 x prompt + 150 x completion + 4 x embedding
//	             + 42 x decision_input + 0 x decision_output      (nano-USD)
//	normalized = floor(cost x 350 / cases)          (a 350-case run equivalent)
//	ratio_bps  = min(floor(normalized x 10000 / reference), 100000)
//	factor_bps = piecewise-linear(curve, ratio_bps)
//
// with reference = $1.00 (1e9 nano-USD) and the curve 10000 at ratio <= 1.0x,
// 8000 at 1.5x and the 6000 floor from 2.0x. All arithmetic is unsigned
// integer with floor division, so Go and Python (ditto_screening_protocol.
// priced_cost) agree bit for bit (testdata/priced_cost_vectors.json).
//
// Prices are CONTRACT CONSTANTS, not live bills: the scorer, every validator
// and any reader recompute the same factor from the token counts in the
// report. Changing a price, the reference or the curve after the version
// activates is a new bench version.
//
// Measurement window: the WHOLE run. A dueling run's datagen half and its
// generator slice share one harness and one ticket, and both spend the
// miner's inference; pricing only the datagen half would leave the 175 slice
// cases free. The factor therefore scales the whole composite (it rides in
// details.dueling.datagen_gate, which multiplies both halves) and never the
// column statistics, so a generator's column means are unaffected by the
// agent's spend. Because it is floored at 0.6 it is a multiplier, not a
// zeroing gate: dueling-bench.md section 4 ("generator cases never feed a
// zeroing gate") holds.

// PricedCostV2MinBenchVersion is the first bench version that uses the
// priced-cost-v2 contract (steeper curve, $0.50 reference, 0.3 floor).
const PricedCostV2MinBenchVersion = BenchVersionV17

const (
	PricedCostContractID   = "priced-cost-v1"
	PricedCostV2ContractID = "priced-cost-v2"

	// Locked public per-token rates, nano-USD per token (1 nano-USD per token
	// = $1 per 1B tokens = $0.001 per 1M tokens).
	//
	// Chat: openai/gpt-oss-20b on Amazon Bedrock, $0.07 / 1M input and
	// $0.15 / 1M output. completion_tokens includes reasoning tokens (the
	// provider bills them as output), so the full count is priced.
	PricedCostChatModel                 = "openai/gpt-oss-20b"
	PricedCostNanoUSDPerPromptToken     = uint64(70)
	PricedCostNanoUSDPerCompletionToken = uint64(150)
	// Embeddings: perplexity/pplx-embed-v1-0.6b, $0.004 / 1M input tokens
	// (the Platform's catalog charge).
	PricedCostEmbeddingModel           = "perplexity/pplx-embed-v1-0.6b"
	PricedCostNanoUSDPerEmbeddingToken = uint64(4)
	// Decision lane: typesafe/jev-1.14, $0.042 / 1M input tokens; the relay
	// meters input only, so output is priced at 0.
	PricedCostDecisionModel                 = "typesafe/jev-1.14"
	PricedCostNanoUSDPerDecisionInputToken  = uint64(42)
	PricedCostNanoUSDPerDecisionOutputToken = uint64(0)

	// PricedCostNormalizationCases is the run size costs are reported at: the
	// v15/v16 full run (350 cases).
	PricedCostNormalizationCases = 350
	// PricedCostReferenceNanoUSD is the full-factor reference for v16: $1.00
	// per 350-case run equivalent.
	PricedCostReferenceNanoUSD = uint64(1_000_000_000)
	// PricedCostFloorBPS is the deepest the v16 factor falls (0.6).
	PricedCostFloorBPS = 6000

	// PricedCostV2ReferenceNanoUSD is the full-factor reference for v17+:
	// $0.50 per 350-case run equivalent (halved from v1 to incentivize cost
	// reduction further). The reference halving doubles the effective sensitivity
	// of the curve: an agent that costs $0.50 per run scores full factor, while
	// the same $1.00 cost that was full-factor under v1 is now 2x the reference.
	PricedCostV2ReferenceNanoUSD = uint64(500_000_000)
	// PricedCostV2FloorBPS is the deepest the v17+ factor falls (0.3), down
	// from 0.6 in v1. Combined with the lower reference this creates a much
	// steeper penalty for high-cost harnesses.
	PricedCostV2FloorBPS = 3000

	// PricedCostMaxRatioBPS caps the reported ratio (10x the reference); the
	// factor is already at the floor long before it.
	PricedCostMaxRatioBPS = 100_000

	PricedCostResultPriced               = "priced"
	PricedCostResultInsufficientEvidence = "insufficient_evidence"
	PricedCostWindowWholeRun             = "whole_run"
	PricedCostSourceRelay                = "relay"
	PricedCostSourceBrokerSession        = "broker_session"
)

// PricedCostCurve is the published v1 curve, ratio ascending. A copy is
// returned so callers cannot mutate the contract.
func PricedCostCurve() []protocol.PricedCostCurvePoint {
	return []protocol.PricedCostCurvePoint{
		{RatioBPS: 10_000, FactorBPS: 10_000},
		{RatioBPS: 15_000, FactorBPS: 8_000},
		{RatioBPS: 20_000, FactorBPS: PricedCostFloorBPS},
	}
}

// PricedCostV2Curve is the published v2 curve, ratio ascending. A copy is
// returned so callers cannot mutate the contract. The v2 curve is steeper:
// at 2x the reference ($1.00 for a 350-case run) the factor hits 0.3.
func PricedCostV2Curve() []protocol.PricedCostCurvePoint {
	return []protocol.PricedCostCurvePoint{
		{RatioBPS: 10_000, FactorBPS: 10_000},
		{RatioBPS: 15_000, FactorBPS: 6_500},
		{RatioBPS: 20_000, FactorBPS: PricedCostV2FloorBPS},
	}
}

// pricedCostCurveForVersion returns the curve for the given bench version.
func pricedCostCurveForVersion(benchVersion int) []protocol.PricedCostCurvePoint {
	if benchVersion >= PricedCostV2MinBenchVersion {
		return PricedCostV2Curve()
	}
	return PricedCostCurve()
}

// pricedCostContractIDForVersion returns the contract id for the given bench version.
func pricedCostContractIDForVersion(benchVersion int) string {
	if benchVersion >= PricedCostV2MinBenchVersion {
		return PricedCostV2ContractID
	}
	return PricedCostContractID
}

// pricedCostReferenceForVersion returns the reference nano-USD for the given bench version.
func pricedCostReferenceForVersion(benchVersion int) uint64 {
	if benchVersion >= PricedCostV2MinBenchVersion {
		return PricedCostV2ReferenceNanoUSD
	}
	return PricedCostReferenceNanoUSD
}

// pricedCostFloorForVersion returns the floor BPS for the given bench version.
func pricedCostFloorForVersion(benchVersion int) int {
	if benchVersion >= PricedCostV2MinBenchVersion {
		return PricedCostV2FloorBPS
	}
	return PricedCostFloorBPS
}

// PricedCostPrices returns the locked rates as published in the report.
func PricedCostPrices() protocol.PricedCostPrices {
	return protocol.PricedCostPrices{
		ChatModel:                   PricedCostChatModel,
		PromptToken:                 PricedCostNanoUSDPerPromptToken,
		CompletionToken:             PricedCostNanoUSDPerCompletionToken,
		EmbeddingModel:              PricedCostEmbeddingModel,
		EmbeddingToken:              PricedCostNanoUSDPerEmbeddingToken,
		DecisionModel:               PricedCostDecisionModel,
		DecisionInputToken:          PricedCostNanoUSDPerDecisionInputToken,
		DecisionOutputToken:         PricedCostNanoUSDPerDecisionOutputToken,
		CompletionIncludesReasoning: true,
	}
}

// PricedCostPosture selects whether the factor multiplies the composite.
type PricedCostPosture string

const (
	// PricedCostEnforce multiplies the composite by the factor (the v16
	// contract default).
	PricedCostEnforce PricedCostPosture = "enforce"
	// PricedCostShadow reports the factor and applies 10000.
	PricedCostShadow PricedCostPosture = "shadow"
)

// ParsePricedCostPosture maps an operator string to a posture. Empty or any
// unknown value is the contract default, enforce: a typo must not silently
// switch a contract term off.
func ParsePricedCostPosture(raw string) PricedCostPosture {
	if strings.EqualFold(strings.TrimSpace(raw), string(PricedCostShadow)) {
		return PricedCostShadow
	}
	return PricedCostEnforce
}

// PricedCostUsage is the run's metered inference.
type PricedCostUsage struct {
	// ChatComplete is true when every successful chat completion and every
	// successful decision-lane call carried usage; otherwise the counts are a
	// lower bound and the factor is not computed.
	ChatComplete         bool
	Source               string
	PromptTokens         uint64
	CompletionTokens     uint64
	EmbeddingTokens      uint64
	DecisionCalls        uint64
	DecisionInputTokens  uint64
	DecisionOutputTokens uint64
}

// pricedCostMaxTokens bounds every count so the nano-USD products cannot
// overflow uint64 (2^53 tokens x 150 < 2^64).
const pricedCostMaxTokens = MaxUsageCount

func clampTokens(v uint64) uint64 {
	if v > pricedCostMaxTokens {
		return pricedCostMaxTokens
	}
	return v
}

func satAdd(a, b uint64) uint64 {
	if a > ^uint64(0)-b {
		return ^uint64(0)
	}
	return a + b
}

func satMul(a, b uint64) uint64 {
	if a != 0 && b > ^uint64(0)/a {
		return ^uint64(0)
	}
	return a * b
}

// PricedCostFactorBPS maps a ratio (normalized cost over the reference, in
// basis points) through the v1 published curve. For version-aware callers use
// PricedCostFactorBPSForCurve with the curve from pricedCostCurveForVersion.
func PricedCostFactorBPS(ratioBPS int) int {
	return PricedCostFactorBPSForCurve(ratioBPS, PricedCostCurve())
}

// PricedCostFactorBPSForCurve maps a ratio through any published curve.
func PricedCostFactorBPSForCurve(ratioBPS int, curve []protocol.PricedCostCurvePoint) int {
	if ratioBPS <= curve[0].RatioBPS {
		return curve[0].FactorBPS
	}
	for i := 1; i < len(curve); i++ {
		lo, hi := curve[i-1], curve[i]
		if ratioBPS > hi.RatioBPS {
			continue
		}
		// Linear between lo and hi; the drop is floored so the factor never
		// rounds below the published line.
		drop := (lo.FactorBPS - hi.FactorBPS) * (ratioBPS - lo.RatioBPS) / (hi.RatioBPS - lo.RatioBPS)
		return lo.FactorBPS - drop
	}
	return curve[len(curve)-1].FactorBPS
}

// BuildPricedCost prices one run. It returns nil below
// PricedCostMinBenchVersion so earlier details keep their exact shape.
// bench_version >= PricedCostV2MinBenchVersion (v17+) uses priced-cost-v2:
// $0.50 reference, 0.3 floor, steeper curve.
func BuildPricedCost(benchVersion int, posture PricedCostPosture, cases int, usage PricedCostUsage) *protocol.PricedCostSummary {
	if !protocol.PricedCostBenchVersion(benchVersion) {
		return nil
	}
	if posture != PricedCostShadow {
		posture = PricedCostEnforce
	}
	prices := PricedCostPrices()
	contractID := pricedCostContractIDForVersion(benchVersion)
	referenceNanoUSD := pricedCostReferenceForVersion(benchVersion)
	floorBPS := pricedCostFloorForVersion(benchVersion)
	curve := pricedCostCurveForVersion(benchVersion)
	summary := &protocol.PricedCostSummary{
		ContractID:           contractID,
		Posture:              string(posture),
		Window:               PricedCostWindowWholeRun,
		Source:               usage.Source,
		Cases:                cases,
		PromptTokens:         clampTokens(usage.PromptTokens),
		CompletionTokens:     clampTokens(usage.CompletionTokens),
		EmbeddingTokens:      clampTokens(usage.EmbeddingTokens),
		DecisionCalls:        usage.DecisionCalls,
		DecisionInputTokens:  clampTokens(usage.DecisionInputTokens),
		DecisionOutputTokens: clampTokens(usage.DecisionOutputTokens),
		Prices:               prices,
		NormalizationCases:   PricedCostNormalizationCases,
		ReferenceNanoUSD:     referenceNanoUSD,
		Curve:                curve,
		FloorBPS:             floorBPS,
		FactorBPS:            BasisPointScale,
		AppliedFactorBPS:     BasisPointScale,
	}
	summary.PromptCostNanoUSD = satMul(summary.PromptTokens, prices.PromptToken)
	summary.CompletionCostNanoUSD = satMul(summary.CompletionTokens, prices.CompletionToken)
	summary.EmbeddingCostNanoUSD = satMul(summary.EmbeddingTokens, prices.EmbeddingToken)
	summary.DecisionCostNanoUSD = satAdd(
		satMul(summary.DecisionInputTokens, prices.DecisionInputToken),
		satMul(summary.DecisionOutputTokens, prices.DecisionOutputToken),
	)
	summary.CostNanoUSD = satAdd(satAdd(summary.PromptCostNanoUSD, summary.CompletionCostNanoUSD),
		satAdd(summary.EmbeddingCostNanoUSD, summary.DecisionCostNanoUSD))
	if !usage.ChatComplete || cases <= 0 {
		summary.Result = PricedCostResultInsufficientEvidence
		return summary
	}
	summary.Result = PricedCostResultPriced
	summary.NormalizedCostNanoUSD = satMul(summary.CostNanoUSD, PricedCostNormalizationCases) / uint64(cases)
	ratio := satMul(summary.NormalizedCostNanoUSD, BasisPointScale) / referenceNanoUSD
	if ratio > PricedCostMaxRatioBPS {
		ratio = PricedCostMaxRatioBPS
	}
	summary.RatioBPS = int(ratio)
	summary.FactorBPS = PricedCostFactorBPSForCurve(summary.RatioBPS, curve)
	if posture == PricedCostEnforce {
		summary.Applied = true
		summary.AppliedFactorBPS = summary.FactorBPS
	}
	return summary
}

// PricedCostMultiplier is the factor the composite carries: 1 for a nil
// summary (bench < 16) and AppliedFactorBPS / 10000 otherwise.
func PricedCostMultiplier(summary *protocol.PricedCostSummary) float64 {
	if summary == nil {
		return 1
	}
	return float64(summary.AppliedFactorBPS) / float64(BasisPointScale)
}

// ValidatePricedCost recomputes a reported block from its own token counts
// and the published constants (a report-side invariant; the composite is
// what is signed).
func ValidatePricedCost(benchVersion int, s protocol.PricedCostSummary) error {
	usage := PricedCostUsage{
		ChatComplete: s.Result == PricedCostResultPriced, Source: s.Source,
		PromptTokens: s.PromptTokens, CompletionTokens: s.CompletionTokens,
		EmbeddingTokens: s.EmbeddingTokens, DecisionCalls: s.DecisionCalls,
		DecisionInputTokens: s.DecisionInputTokens, DecisionOutputTokens: s.DecisionOutputTokens,
	}
	want := BuildPricedCost(benchVersion, PricedCostPosture(s.Posture), s.Cases, usage)
	if want == nil {
		return invalid("priced cost reported below bench %d", protocol.PricedCostMinBenchVersion)
	}
	curveMatches := len(s.Curve) == len(want.Curve)
	for i := 0; curveMatches && i < len(s.Curve); i++ {
		curveMatches = s.Curve[i] == want.Curve[i]
	}
	if !curveMatches || s.ContractID != want.ContractID || s.Result != want.Result || s.Posture != want.Posture ||
		s.PromptTokens != want.PromptTokens || s.CompletionTokens != want.CompletionTokens ||
		s.EmbeddingTokens != want.EmbeddingTokens || s.DecisionInputTokens != want.DecisionInputTokens ||
		s.DecisionOutputTokens != want.DecisionOutputTokens ||
		s.Window != want.Window || s.Prices != want.Prices || s.NormalizationCases != want.NormalizationCases ||
		s.ReferenceNanoUSD != want.ReferenceNanoUSD || s.FloorBPS != want.FloorBPS ||
		s.PromptCostNanoUSD != want.PromptCostNanoUSD || s.CompletionCostNanoUSD != want.CompletionCostNanoUSD ||
		s.EmbeddingCostNanoUSD != want.EmbeddingCostNanoUSD || s.DecisionCostNanoUSD != want.DecisionCostNanoUSD ||
		s.FactorBPS != want.FactorBPS || s.AppliedFactorBPS != want.AppliedFactorBPS ||
		s.CostNanoUSD != want.CostNanoUSD || s.NormalizedCostNanoUSD != want.NormalizedCostNanoUSD ||
		s.RatioBPS != want.RatioBPS || s.Applied != want.Applied {
		return invalid("priced cost block does not match the published rule")
	}
	return nil
}
