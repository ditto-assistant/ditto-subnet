// Package routerreplay is the SN118 router track's OFFLINE deterministic replay
// scorer. v1 shadow mode never calls a live provider: instead the trusted scorer
// replays a fixed corpus of recorded tap/relay upstream-usage pairs — one per
// big-four harness — through the exact routerscore token-dominant blend it would
// use live. That yields a real, non-zero, prefix-stable shadow composite for an
// included router with zero secrets and zero network egress, matching the v1
// offline-conformance / prefix-replay design.
//
// The corpus is embedded so the deployed dittobench-api binary can replay it with
// no external testdata dependency. Everything here is SHADOW and never weight-
// eligible; the measured number is carried as routerscore.LedgerEntry.ShadowComposite
// (the dashboard's router_shadow_composite), never folded into weights.
package routerreplay

import (
	"embed"
	"encoding/json"
	"fmt"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

//go:embed fixtures/*.json
var fixturesFS embed.FS

// DefaultCorpusPath is the embedded reference corpus the LocalBackend replays by
// default.
const DefaultCorpusPath = "fixtures/default.json"

// Record is one harness's recorded replay row: the router's upstream provider
// usage after its lever, the reference p90 allowance the no-router cost would
// have used, and the non-token axis scores. It is a pure function input — the
// same record always yields the same outcome.
type Record struct {
	Harness       routerharness.Harness `json:"harness"`
	Operational   bool                  `json:"operational"`
	FloorObserved bool                  `json:"floor_observed"`
	// Reference p90 allowance (the no-router cost the token axis grades against).
	BaselinePromptAllow     uint64 `json:"baseline_prompt_allow"`
	BaselineCompletionAllow uint64 `json:"baseline_completion_allow"`
	// The router's UPSTREAM provider usage for this harness's replayed tasks.
	UpstreamPromptTokens     uint64 `json:"upstream_prompt_tokens"`
	UpstreamCompletionTokens uint64 `json:"upstream_completion_tokens"`
	// Non-token efficiency axes in [0, 1] (tokens dominate the blend).
	LatencyScore   float64 `json:"latency_score"`
	RetrievalScore float64 `json:"retrieval_score"`
	// Informational upstream cost, carried onto the ledger slice.
	UpstreamTokenCostMicros uint64 `json:"upstream_token_cost_micros"`
}

// Corpus is a loaded, ordered replay corpus.
type Corpus struct {
	CorpusID    string   `json:"corpus_id"`
	Description string   `json:"description"`
	Harnesses   []Record `json:"harnesses"`
}

// Default loads and validates the embedded reference corpus.
func Default() (Corpus, error) {
	return Load(DefaultCorpusPath)
}

// Load parses one embedded corpus by path and validates it.
func Load(path string) (Corpus, error) {
	raw, err := fixturesFS.ReadFile(path)
	if err != nil {
		return Corpus{}, fmt.Errorf("routerreplay: read %s: %w", path, err)
	}
	var c Corpus
	if err := json.Unmarshal(raw, &c); err != nil {
		return Corpus{}, fmt.Errorf("routerreplay: parse %s: %w", path, err)
	}
	if err := c.validate(); err != nil {
		return Corpus{}, fmt.Errorf("routerreplay: %s: %w", path, err)
	}
	return c, nil
}

// validate rejects a malformed corpus so a bad fixture fails closed at load
// (caught in CI) rather than silently scoring wrong.
func (c Corpus) validate() error {
	if len(c.Harnesses) == 0 {
		return fmt.Errorf("empty corpus")
	}
	seen := make(map[routerharness.Harness]bool, len(c.Harnesses))
	for _, r := range c.Harnesses {
		if !r.Harness.Valid() {
			return fmt.Errorf("unknown harness %q", r.Harness)
		}
		if seen[r.Harness] {
			return fmt.Errorf("duplicate harness %q", r.Harness)
		}
		seen[r.Harness] = true
		if r.LatencyScore < 0 || r.LatencyScore > 1 || r.RetrievalScore < 0 || r.RetrievalScore > 1 {
			return fmt.Errorf("harness %q axis scores out of [0,1]", r.Harness)
		}
	}
	return nil
}

// baseline builds a valid v5 efficiency baseline whose p90 allowance is the
// record's reference prompt/completion split (mirrors the routerscore test
// baseline so TokenScore grades against a real allowance).
func (r Record) baseline() *efficiency.Baseline {
	return &efficiency.Baseline{
		ID:                 "router-replay-" + string(r.Harness),
		BenchVersion:       protocol.BenchVersionV5,
		RunSize:            "full",
		Provider:           "router",
		Model:              "router-upstream",
		PromptTokens:       r.BaselinePromptAllow,
		CompletionTokens:   r.BaselineCompletionAllow,
		TotalTokens:        r.BaselinePromptAllow + r.BaselineCompletionAllow,
		Samples:            20,
		Aggregation:        "nearest_rank_p90",
		StarterKitRevision: "router-replay",
	}
}

// usage builds the complete, arithmetic-valid upstream TokenUsage for the record.
func (r Record) usage() protocol.TokenUsage {
	return protocol.TokenUsage{
		Status:           "complete",
		Successes:        1,
		UsageAvailable:   1,
		UsageUnavailable: 0,
		PromptTokens:     r.UpstreamPromptTokens,
		CompletionTokens: r.UpstreamCompletionTokens,
		TotalTokens:      r.UpstreamPromptTokens + r.UpstreamCompletionTokens,
	}
}

// Outcome scores one record into a routerscore.HarnessOutcome: the token axis is
// the dominant TokenScore over the replayed upstream usage vs the reference
// allowance, blended with the fixed latency/retrieval axes, then floor-gated. A
// non-operational record forfeits its slice (Combine ignores it).
func (r Record) Outcome(axis routerscore.EffAxisWeights, gate routerscore.FloorGate) routerscore.HarnessOutcome {
	floor := gate.FloorPass(r.Operational, r.FloorObserved)
	if !r.Operational {
		return routerscore.HarnessOutcome{
			Harness:                 r.Harness,
			Operational:             false,
			Floor:                   floor,
			UpstreamTokenCostMicros: r.UpstreamTokenCostMicros,
		}
	}
	tok := routerscore.TokenScore(r.usage(), r.baseline())
	eff := axis.Blend(tok, r.LatencyScore, r.RetrievalScore)
	return routerscore.HarnessOutcome{
		Harness:                 r.Harness,
		Operational:             true,
		Floor:                   floor,
		Efficiency:              eff,
		UpstreamTokenCostMicros: r.UpstreamTokenCostMicros,
	}
}

// Outcomes scores every record in stable corpus order into per-harness outcomes
// ready for routerscore.Combine / BuildEntry.
func (c Corpus) Outcomes(axis routerscore.EffAxisWeights, gate routerscore.FloorGate) []routerscore.HarnessOutcome {
	outcomes := make([]routerscore.HarnessOutcome, 0, len(c.Harnesses))
	for _, r := range c.Harnesses {
		outcomes = append(outcomes, r.Outcome(axis, gate))
	}
	return outcomes
}
