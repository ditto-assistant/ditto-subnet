// Package routerscore scores the SN118 router track: a correctness FLOOR (a
// gate) followed by a token-cost-DOMINANT efficiency rank, aggregated across the
// four harnesses with a soft per-harness weighting, and published as a ledger
// the Python validator folds.
//
// The scoring shape mirrors ditto/api_models/router_ledger.py exactly:
//
//	combined_score = Σ_h weight_h × (eff_h if operational_h && floor_h else 0)
//
// A failed harness forfeits ONLY its slice (soft aggregation). eff_h is a blend
// of token/latency/retrieval axes with TOKENS dominant; its token axis reuses
// internal/efficiency (the p90 token-waste primitive) over the router's UPSTREAM
// provider tokens per task. The floor gate rides the internal/scoregates
// REVIEW→ENFORCE rollout ladder and starts in shadow (REVIEW): observed but not
// blocking, and never weight-eligible.
//
// Nothing here is wired into a live scoring path or the release graph; it is
// shadow scaffolding plus tests.
package routerscore

import (
	"math"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// RouterContractVersion is the router-track contract this scorer emits, matching
// the Literal[1] on the Python RouterLedgerEntry.router_contract_version.
const RouterContractVersion = 1

// effEpsilon keeps a counting harness's efficiency strictly inside (0, 1]; only
// a forfeited slice (not operational or floor failure) contributes exactly 0.
const effEpsilon = 1e-6

// FloorGate wraps the scoregates REVIEW→ENFORCE rollout ladder for the router
// track's deterministic correctness floor.
//
// NOTE ON THE LADDER NAMES: the brief calls the ladder REVIEW→ENFORCE, but
// internal/scoregates exposes the two modes as RolloutShadow and RolloutEnforce
// (there is no distinct "review" constant). This gate maps REVIEW to
// scoregates.RolloutShadow — the observe-only mode — and ENFORCE to
// scoregates.RolloutEnforce. In shadow the floor observation is recorded but
// never blocks (an operational run always clears); in enforce the observation
// gates the slice.
type FloorGate struct {
	Mode scoregates.RolloutMode
}

// ShadowFloorGate is the shadow-start gate (REVIEW): floor observed, not blocking.
func ShadowFloorGate() FloorGate { return FloorGate{Mode: scoregates.RolloutShadow} }

// EnforceFloorGate is the promoted gate (ENFORCE): the floor observation blocks.
func EnforceFloorGate() FloorGate { return FloorGate{Mode: scoregates.RolloutEnforce} }

// FloorPass returns the effective, mode-aware floor decision for one harness. A
// non-operational run never passes. In enforce mode the raw deterministic floor
// observation gates; in shadow (REVIEW) mode the floor is observed but does not
// block, so an operational run always clears.
func (g FloorGate) FloorPass(operational, floorObserved bool) bool {
	if !operational {
		return false
	}
	if g.Mode == scoregates.RolloutEnforce {
		return floorObserved
	}
	return true
}

// EffAxisWeights weights the three efficiency axes. Tokens is dominant.
type EffAxisWeights struct {
	Tokens    float64 `json:"tokens"`
	Latency   float64 `json:"latency"`
	Retrieval float64 `json:"retrieval"`
}

// DefaultEffAxisWeights makes tokens dominant (0.7/0.2/0.1).
var DefaultEffAxisWeights = EffAxisWeights{Tokens: 0.7, Latency: 0.2, Retrieval: 0.1}

// Blend computes eff_h = wTok*tokScore + wLat*latScore + wRet*retScore, clamped
// to (0, 1]. Each axis score is expected in [0, 1].
func (w EffAxisWeights) Blend(tokScore, latScore, retScore float64) float64 {
	eff := w.Tokens*tokScore + w.Latency*latScore + w.Retrieval*retScore
	if math.IsNaN(eff) || math.IsInf(eff, 0) {
		return effEpsilon
	}
	if eff > 1 {
		return 1
	}
	if eff < effEpsilon {
		return effEpsilon
	}
	return eff
}

// TokenScore is the DOMINANT token axis: it reuses internal/efficiency's p90
// token-waste transform over the router's UPSTREAM provider usage for a
// harness's tasks. The returned multiplier is 1 within the p90 budget and decays
// toward efficiency.MinMultiplier as upstream waste grows, so a cheaper
// floor-clearing run scores no lower than a pricier one. When usage telemetry or
// the baseline is unavailable the transform yields a neutral 1.
func TokenScore(usage protocol.TokenUsage, baseline *efficiency.Baseline) float64 {
	eff := efficiency.Apply(protocol.ScoreReport{Composite: 1}, usage, baseline)
	return eff.Multiplier
}

// HarnessWeights assigns each harness its slice of the combined score.
type HarnessWeights map[routerharness.Harness]float64

// DefaultHarnessWeights weights claude_code:0.40, codex:0.30, opencode:0.20,
// grok:0.10.
var DefaultHarnessWeights = HarnessWeights{
	routerharness.HarnessClaudeCode: 0.40,
	routerharness.HarnessCodex:      0.30,
	routerharness.HarnessOpencode:   0.20,
	routerharness.HarnessGrok:       0.10,
}

// HarnessOutcome is one harness's scored result feeding the soft combine. Floor
// is the effective, mode-aware decision (FloorGate.FloorPass), not the raw
// observation; Efficiency is eff_h in (0, 1].
type HarnessOutcome struct {
	Harness                 routerharness.Harness
	Operational             bool
	Floor                   bool
	Efficiency              float64
	UpstreamTokenCostMicros uint64
}

// Combine folds the per-harness slices: Σ_h weight_h × (eff_h if operational_h
// && floor_h else 0). A failed harness forfeits only its own slice. The result
// is clamped to [0, 1].
func Combine(weights HarnessWeights, outcomes []HarnessOutcome) float64 {
	var total float64
	for _, o := range outcomes {
		if !o.Operational || !o.Floor {
			continue
		}
		total += weights[o.Harness] * o.Efficiency
	}
	if total < 0 {
		return 0
	}
	if total > 1 {
		return 1
	}
	return total
}

// HarnessResult mirrors the Python RouterHarnessResult. JSON tags are the Python
// snake_case keys so the validator parses this byte-for-byte.
type HarnessResult struct {
	Harness                 routerharness.Harness `json:"harness"`
	Operational             bool                  `json:"operational"`
	FloorPass               bool                  `json:"floor_pass"`
	Efficiency              float64               `json:"efficiency"`
	UpstreamTokenCostMicros uint64                `json:"upstream_token_cost_micros"`
}

// LedgerEntry mirrors the Python RouterLedgerEntry. AgentID is the UUID string.
type LedgerEntry struct {
	MinerHotkey           string          `json:"miner_hotkey"`
	AgentID               string          `json:"agent_id"`
	RouterContractVersion int             `json:"router_contract_version"`
	WeightEligible        bool            `json:"weight_eligible"`
	CombinedScore         float64         `json:"combined_score"`
	Harnesses             []HarnessResult `json:"harnesses"`
	FirstSeen             time.Time       `json:"first_seen"`
}

// Ledger mirrors the Python RouterLedgerResponse. Entries are ordered highest
// combined score first by the scorer before publication.
type Ledger struct {
	Entries               []LedgerEntry `json:"entries"`
	RouterContractVersion *int          `json:"router_contract_version,omitempty"`
	GeneratedAt           *time.Time    `json:"generated_at,omitempty"`
	Stale                 bool          `json:"stale"`
	Count                 int           `json:"count"`
}

// BuildEntry assembles a ledger entry from per-harness outcomes: it records each
// slice, computes the soft weighted combine, and stamps the contract version. In
// shadow the entry is never weight-eligible; the validator's track state remains
// the authority and weightEligible here is a defensive echo.
func BuildEntry(
	minerHotkey, agentID string,
	weights HarnessWeights,
	weightEligible bool,
	firstSeen time.Time,
	outcomes []HarnessOutcome,
) LedgerEntry {
	results := make([]HarnessResult, 0, len(outcomes))
	for _, o := range outcomes {
		results = append(results, HarnessResult{
			Harness:                 o.Harness,
			Operational:             o.Operational,
			FloorPass:               o.Floor,
			Efficiency:              o.Efficiency,
			UpstreamTokenCostMicros: o.UpstreamTokenCostMicros,
		})
	}
	return LedgerEntry{
		MinerHotkey:           minerHotkey,
		AgentID:               agentID,
		RouterContractVersion: RouterContractVersion,
		WeightEligible:        weightEligible,
		CombinedScore:         Combine(weights, outcomes),
		Harnesses:             results,
		FirstSeen:             firstSeen,
	}
}
