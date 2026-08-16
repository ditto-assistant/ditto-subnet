// Package efficiency implements the versioned DittoBench v5 token transform.
// It consumes only validator-observed model-proxy telemetry and keeps the raw
// quality score separate from the adjusted score.
package efficiency

import (
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"sort"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	FormulaVersion   = "v5-relay-token-waste-p90-v1"
	BudgetPercentile = 0.90
	MaximumPenalty   = 0.10
	MinMultiplier    = 1 - MaximumPenalty
)

// v7 quality-only contract. While the v7 difficulty suite is still moving,
// repeatedly recalibrating an ABSOLUTE token budget is the wrong contract, so
// bench_version 7 removes the token penalty from the composite entirely:
//
//   - The v7 composite is a pure function of answer/trajectory QUALITY. No
//     token multiplier is applied, so a deterministic validator produces the
//     same score for the same artifact regardless of when it runs.
//   - Audited usage stays FIRST-CLASS: validators keep metering and reporting
//     trusted chat + embedding usage (details.token_usage and the broker
//     accounting record) alongside the quality score. ApplyForVersion emits a
//     neutral TokenEfficiency record carrying the observed totals and the
//     explicit decision reason below, so every v7 report shows the usage and
//     shows that it did not move the composite.
//   - Efficiency incentives move to the PLATFORM layer as a bounded,
//     epoch-frozen RELATIVE bonus among quality-qualified submissions — see
//     docs/relative-efficiency-bonus-spec.md. The validator's only job is to
//     expose the audited inputs that make that computable.
//
// The reviewed-measured v7 manifest is retained only as the compatibility
// identity already advertised by deployed validators. Its historical budgets
// are never activated or refreshed. V8 uses a smaller quality-only contract
// with no calibration grid; the platform-side relative bonus is the successor
// to absolute starter budgets.
const (
	V7QualityOnlyFormula = "v7-quality-only-v1"
	V7QualityOnlyReason  = "v7_quality_only_contract"
)

type Baseline struct {
	ID                           string `json:"id"`
	BenchVersion                 int    `json:"bench_version"`
	RunSize                      string `json:"run_size"`
	Provider                     string `json:"provider"`
	ProfileRevision              string `json:"profile_revision"`
	Model                        string `json:"model"`
	PromptTokens                 uint64 `json:"prompt_tokens,omitempty"`
	CompletionTokens             uint64 `json:"completion_tokens,omitempty"`
	TotalTokens                  uint64 `json:"total_tokens,omitempty"`
	RawReferencePromptTokens     uint64 `json:"raw_reference_prompt_tokens,omitempty"`
	RawReferenceCompletionTokens uint64 `json:"raw_reference_completion_tokens,omitempty"`
	RawReferenceTotalTokens      uint64 `json:"raw_reference_total_tokens,omitempty"`
	AllowanceMultiplierBPS       uint64 `json:"allowance_multiplier_bps,omitempty"`
	AllowancePromptTokens        uint64 `json:"allowance_prompt_tokens,omitempty"`
	AllowanceCompletionTokens    uint64 `json:"allowance_completion_tokens,omitempty"`
	AllowanceTotalTokens         uint64 `json:"allowance_total_tokens,omitempty"`
	AllowancePolicy              string `json:"allowance_policy,omitempty"`
	Samples                      int    `json:"samples"`
	Aggregation                  string `json:"aggregation"`
	StarterKitRevision           string `json:"starter_kit_revision"`
}

type CalibrationDataset struct {
	RunSize       string `json:"run_size"`
	Seed          int64  `json:"seed"`
	DatasetSHA256 string `json:"dataset_sha256"`
}

type EmbeddingContract struct {
	Provider      string `json:"provider"`
	Model         string `json:"model"`
	Profile       string `json:"profile"`
	Dimensions    int    `json:"dimensions"`
	CatalogSHA256 string `json:"catalog_sha256"`
}

// QualityOnlyContract is the v8 scorer-readiness contract. It deliberately has
// no starter baseline or calibration grid: the scorer records trusted usage but
// never applies an absolute token transform. Relative efficiency is computed
// by ditto-platform from an epoch-frozen live cohort after model-use integrity
// checks have qualified the run.
type QualityOnlyContract struct {
	SchemaVersion       int                      `json:"schema_version"`
	BenchVersion        int                      `json:"bench_version"`
	DatasetKnownVector  string                   `json:"dataset_known_vector"`
	ScorerTokenPolicy   string                   `json:"scorer_token_policy"`
	EfficiencyAuthority string                   `json:"efficiency_authority"`
	Harness             CalibrationRouteIdentity `json:"harness"`
	Embedding           EmbeddingContract        `json:"embedding"`
}

type Manifest struct {
	SchemaVersion      int                  `json:"schema_version"`
	FormulaVersion     string               `json:"formula_version"`
	BenchVersion       int                  `json:"bench_version"`
	ScoringEnabled     bool                 `json:"scoring_enabled"`
	DatasetKnownVector string               `json:"dataset_known_vector"`
	StarterKitRevision string               `json:"starter_kit_revision"`
	Embedding          *EmbeddingContract   `json:"embedding,omitempty"`
	Calibration        []CalibrationDataset `json:"calibration_datasets"`
	Baselines          []Baseline           `json:"baselines"`
	// Derived is retained only so historical/research manifests remain
	// parseable and are rejected fail-closed. The transfer tool that produced
	// this provenance is retired; no scoring or readiness gate consumes it.
	Derived *Derivation `json:"derived,omitempty"`
}

// Derivation records how a derived manifest was produced: the model identity,
// the fit evidence behind it, the per-size transfer factors applied, and the
// smoke-validation outcome (nil until a smoke pass runs).
type Derivation struct {
	Method               string                    `json:"method"`
	ModelVersion         string                    `json:"model_version"`
	SourceManifestSHA256 string                    `json:"source_manifest_sha256"`
	SourceBenchVersion   int                       `json:"source_bench_version"`
	FitCorpusSHA256      string                    `json:"fit_corpus_sha256"`
	FitRuns              int                       `json:"fit_runs"`
	FitResidualMeanPct   map[string]float64        `json:"fit_residual_mean_pct"`
	FitResidualMaxPct    map[string]float64        `json:"fit_residual_max_pct"`
	TransferP90CVMaxPct  map[string]float64        `json:"transfer_p90_cv_max_pct"`
	TransferFactors      map[string]TransferFactor `json:"transfer_factors"`
	Smoke                *SmokeRecord              `json:"smoke,omitempty"`
}

// TransferFactor is the per-run-size multiplicative correction applied to the
// model's raw predictions (the measured-vs-predicted anchor at the p90 rank
// of the fit corpus).
type TransferFactor struct {
	Prompt     float64 `json:"prompt"`
	Completion float64 `json:"completion"`
}

// SmokeRecord is the outcome of validating a derived manifest against K live
// runs. Passed is true only when every smoke run's measured chat total landed
// inside the acceptance band (tolerance shrunk by the border zone — a
// near-boundary result is rejected as inconclusive, fail-closed).
type SmokeRecord struct {
	Runs           []SmokeRun         `json:"runs"`
	TolerancePct   map[string]float64 `json:"tolerance_pct"`
	BorderZoneFrac float64            `json:"border_zone_frac"`
	Passed         bool               `json:"passed"`
}

// SmokeRun is one live run's measured-vs-predicted comparison.
type SmokeRun struct {
	RunSize              string  `json:"run_size"`
	Seed                 int64   `json:"seed"`
	MeasuredTotalTokens  uint64  `json:"measured_total_tokens"`
	PredictedTotalTokens uint64  `json:"predicted_total_tokens"`
	ErrorPct             float64 `json:"error_pct"`
}

// Manifest provenance classes. ReviewedMeasured is the only class any gate
// accepts today.
const (
	ProvenanceReviewedMeasured     = "reviewed-measured"
	ProvenanceReviewedContract     = "reviewed-contract"
	ProvenanceDerivedSmokeVerified = "derived-smoke-validated"
	ProvenanceDerivedUnvalidated   = "derived-unvalidated"
)

// ManifestProvenance classifies a manifest's calibration provenance. The
// platform can use this to apply per-class rollout policy; validator-side
// gates currently accept only ProvenanceReviewedMeasured.
func ManifestProvenance(m Manifest) string {
	if m.Derived == nil {
		return ProvenanceReviewedMeasured
	}
	if m.Derived.Smoke != nil && m.Derived.Smoke.Passed {
		return ProvenanceDerivedSmokeVerified
	}
	return ProvenanceDerivedUnvalidated
}

// CalibrationRouteIdentity is the exact provider route contract covered by a
// reviewed token-baseline manifest. Consumers must intersect all three fields;
// a provider name alone is not a calibrated route identity.
type CalibrationRouteIdentity struct {
	Provider        string `json:"provider"`
	ProfileRevision string `json:"profile_revision"`
	Model           string `json:"model"`
}

// CalibrationReadiness is the public identity of a reviewed baseline
// manifest. An empty digest and route set means no reviewed v7 manifest is
// embedded and v7 must remain unavailable.
type CalibrationReadiness struct {
	ManifestSHA256  string                     `json:"manifest_sha256"`
	SupportedRoutes []CalibrationRouteIdentity `json:"supported_routes"`
	// Provenance is the manifest's calibration provenance class (additive;
	// see ManifestProvenance). The embedded production manifest is always
	// reviewed-measured today.
	Provenance string `json:"provenance,omitempty"`
}

//go:embed baselines_v5.json
var manifestJSON []byte

//go:embed baselines_v7.json
var manifestV7JSON []byte

//go:embed contract_v8.json
var contractV8JSON []byte

var productionManifest = mustManifest(manifestJSON, protocol.BenchVersionV5)
var productionV7Manifest = mustManifest(manifestV7JSON, protocol.BenchVersionV7)
var productionV8Contract = mustQualityOnlyContract(contractV8JSON)

func mustManifest(body []byte, benchVersion int) Manifest {
	var manifest Manifest
	if err := json.Unmarshal(body, &manifest); err != nil {
		panic(fmt.Sprintf("invalid embedded token baseline manifest: %v", err))
	}
	if manifest.SchemaVersion != 2 || manifest.FormulaVersion != FormulaVersion || manifest.BenchVersion != benchVersion {
		panic("embedded token baseline manifest has incompatible contract metadata")
	}
	return manifest
}

func mustQualityOnlyContract(body []byte) QualityOnlyContract {
	var contract QualityOnlyContract
	if err := json.Unmarshal(body, &contract); err != nil {
		panic(fmt.Sprintf("invalid embedded quality-only contract: %v", err))
	}
	return contract
}

func ManifestSnapshot() Manifest {
	copy := productionManifest
	copy.Calibration = append([]CalibrationDataset(nil), productionManifest.Calibration...)
	copy.Baselines = append([]Baseline(nil), productionManifest.Baselines...)
	return copy
}

func V7ManifestSnapshot() Manifest {
	copy := productionV7Manifest
	copy.Calibration = append([]CalibrationDataset(nil), productionV7Manifest.Calibration...)
	copy.Baselines = append([]Baseline(nil), productionV7Manifest.Baselines...)
	if productionV7Manifest.Embedding != nil {
		embedding := *productionV7Manifest.Embedding
		copy.Embedding = &embedding
	}
	return copy
}

func V8ContractSnapshot() QualityOnlyContract { return productionV8Contract }

// ProductionReady is true only after reviewed baselines exist for every run
// size on both certified provider profiles. Until then v5 remains hidden from
// capability negotiation and any direct v5 report fails neutral.
func ProductionReady() bool {
	return ReadyForProduction(productionManifest)
}

// ProductionReadyForVersion reports TECHNICAL readiness, which gates capability
// advertisement. v7 becomes ready once its reviewed GPT-OSS quality-only
// manifest is embedded (scoring_enabled=false, permanent — usage is audited but
// never scored); v5/v6 retain the existing Qwen calibration. Whether a ready
// version is actually dispatched/scored is the platform's benchmark rollout
// decision (backroom-controlled active bench); there is deliberately no
// validator-side activation flag. V9 and V10 retain the reviewed v8
// quality-only authority. V9 adds its own score-gate contract; V9 and later
// share the agent-selected reasoning route contract.
func ProductionReadyForVersion(benchVersion int) bool {
	switch benchVersion {
	case protocol.BenchVersionV5, protocol.BenchVersionV6:
		return ProductionReady()
	case protocol.BenchVersionV7:
		return ReadyForV7QualityOnly(productionV7Manifest)
	case protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10,
		protocol.BenchVersionV11:
		return ReadyForV8QualityOnly(productionV8Contract)
	default:
		return false
	}
}

// V8Readiness carries the immutable dataset and route identity for the v8
// quality-only contract. No token baseline is implied or consumed.
func V8Readiness() CalibrationReadiness {
	readiness := CalibrationReadiness{SupportedRoutes: []CalibrationRouteIdentity{}}
	if !ReadyForV8QualityOnly(productionV8Contract) {
		return readiness
	}
	readiness.SupportedRoutes = []CalibrationRouteIdentity{{
		Provider: v7AggregateProvider, ProfileRevision: v7AggregateProfile,
		Model: llm.V7HarnessModel,
	}}
	sum := sha256.Sum256(contractV8JSON)
	readiness.ManifestSHA256 = fmt.Sprintf("%x", sum[:])
	readiness.Provenance = ProvenanceReviewedContract
	return readiness
}

func ValidV8Readiness(readiness CalibrationReadiness) bool {
	return canonicalSHA256(readiness.ManifestSHA256) &&
		len(readiness.SupportedRoutes) == 1 &&
		readiness.SupportedRoutes[0] == (CalibrationRouteIdentity{
			Provider: v7AggregateProvider, ProfileRevision: v7AggregateProfile,
			Model: llm.V7HarnessModel,
		})
}

// V7CalibrationReadiness returns only identities proven by the embedded,
// reviewed v7 manifest. There is deliberately no candidate/smoke fallback.
func V7CalibrationReadiness() CalibrationReadiness {
	return v7CalibrationReadiness(productionV7Manifest, manifestV7JSON)
}

func v7CalibrationReadiness(manifest Manifest, rawManifest []byte) CalibrationReadiness {
	readiness := CalibrationReadiness{SupportedRoutes: []CalibrationRouteIdentity{}}
	if len(rawManifest) == 0 || !ReadyForV7QualityOnly(manifest) {
		return readiness
	}
	seen := map[CalibrationRouteIdentity]bool{}
	for _, baseline := range manifest.Baselines {
		seen[CalibrationRouteIdentity{
			Provider:        baseline.Provider,
			ProfileRevision: baseline.ProfileRevision,
			Model:           baseline.Model,
		}] = true
	}
	for route := range seen {
		readiness.SupportedRoutes = append(readiness.SupportedRoutes, route)
	}
	sort.Slice(readiness.SupportedRoutes, func(i, j int) bool {
		a, b := readiness.SupportedRoutes[i], readiness.SupportedRoutes[j]
		if a.Provider != b.Provider {
			return a.Provider < b.Provider
		}
		if a.ProfileRevision != b.ProfileRevision {
			return a.ProfileRevision < b.ProfileRevision
		}
		return a.Model < b.Model
	})
	sum := sha256.Sum256(rawManifest)
	readiness.ManifestSHA256 = fmt.Sprintf("%x", sum[:])
	readiness.Provenance = ManifestProvenance(manifest)
	return readiness
}

func ValidV7CalibrationReadiness(readiness CalibrationReadiness) bool {
	if !canonicalSHA256(readiness.ManifestSHA256) || len(readiness.SupportedRoutes) != 1 {
		return false
	}
	return readiness.SupportedRoutes[0] == (CalibrationRouteIdentity{
		Provider: v7AggregateProvider, ProfileRevision: v7AggregateProfile,
		Model: llm.V7HarnessModel,
	})
}

// ReadyForProduction validates a candidate manifest using the same gate as the
// embedded production manifest. Calibration tooling uses this before it can
// emit an explicitly enabled phase-B candidate.
func ReadyForProduction(manifest Manifest) bool {
	if !manifest.ScoringEnabled {
		return false
	}
	required := []struct {
		provider string
		revision string
		model    string
	}{
		{"chutes", llm.ChutesRelayProfileRevision, llm.LockedUpstreamModel},
		{"openrouter", llm.OpenRouterRelayProfileRevision, llm.LockedHarnessModel},
	}
	for _, contract := range required {
		for _, runSize := range []string{"small", "medium", "full"} {
			found := false
			for _, b := range manifest.Baselines {
				if b.BenchVersion == protocol.BenchVersionV5 && b.Provider == contract.provider &&
					b.ProfileRevision == contract.revision && b.Model == contract.model && b.RunSize == runSize &&
					b.StarterKitRevision == manifest.StarterKitRevision && validBaseline(b) {
					found = true
					break
				}
			}
			if !found {
				return false
			}
		}
	}
	return true
}

const (
	v7AggregateProvider        = "openrouter"
	v7AggregateProfile         = "openrouter-route-a471cd87ae7df5b9-v1"
	v7StarterKitRevision       = "62223b028acceb38ad0db98790402f1e2361dd18"
	v7DatasetKnownVector       = "1cfc6e3b9f3f4c04afe04b058a6851f9357f6463170b879867e2cf4588f58fcf"
	v7CalibrationDatasetDigest = "11067ee51dd87582af939f26631cfc075588a13f708a726a3ebae1199dcabfa8"
	v7EmbeddingProvider        = "Perplexity"
	v7EmbeddingModel           = "perplexity/pplx-embed-v1-0.6b"
	v7EmbeddingProfile         = "dittobench-v7-openrouter-pplx-embed-v1-0.6b-768-v1"
	v7EmbeddingDimensions      = 768
	v7EmbeddingCatalogSHA256   = "a6fff568e4f8c9d17e54739e6da2cdd76006d2a486124c5c43b12e94099d3348"
	v7AllowanceMultiplierBPS   = 7500
	v7AllowancePolicy          = "starter_raw_p90_floor_75_percent_v1"
)

const (
	v8DatasetKnownVector  = "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98"
	v8ScorerTokenPolicy   = "quality-only"
	v8EfficiencyAuthority = "ditto-platform-relative-cohort-v1"
)

// v7ContractShapeValid validates every element of the reviewed aggregate
// GPT-OSS v7 contract EXCEPT the scoring_enabled flag: the locked harness model
// identity, the aggregate route + hosted embedding profile, the starter-kit
// revision, the dataset known-vector and calibration-digest verification, and
// the three reference baseline groups (one per run size). Both the
// scoring-enabled contract (ReadyForV7Production) and the quality-only
// production contract (ReadyForV7QualityOnly) build on this shared, fail-closed
// identity gate. Derived manifests (calibration-transfer provenance) are
// rejected here: the derived+smoke-validated class is reserved for a later
// explicit platform policy, and today only a reviewed-measured campaign may
// establish v7 identity/readiness.
func v7ContractShapeValid(manifest Manifest) bool {
	if manifest.Derived != nil {
		return false
	}
	if manifest.BenchVersion != protocol.BenchVersionV7 ||
		manifest.StarterKitRevision != v7StarterKitRevision ||
		manifest.DatasetKnownVector != v7DatasetKnownVector ||
		calibrationDatasetDigest(manifest.Calibration) != v7CalibrationDatasetDigest ||
		!validV7CalibrationDatasets(manifest.Calibration) ||
		manifest.Embedding == nil || *manifest.Embedding != (EmbeddingContract{
		Provider: v7EmbeddingProvider, Model: v7EmbeddingModel,
		Profile: v7EmbeddingProfile, Dimensions: v7EmbeddingDimensions,
		CatalogSHA256: v7EmbeddingCatalogSHA256,
	}) {
		return false
	}
	groups := map[string]int{}
	profiles := map[string]bool{}
	for _, b := range manifest.Baselines {
		if b.BenchVersion != protocol.BenchVersionV7 || b.Provider != v7AggregateProvider ||
			b.Model != llm.V7HarnessModel || b.StarterKitRevision != manifest.StarterKitRevision ||
			b.ProfileRevision != v7AggregateProfile || b.Samples != 20 || !validBaseline(b) ||
			b.ID != baselineID(protocol.BenchVersionV7, b) {
			return false
		}
		profile := b.Provider + ":" + b.ProfileRevision
		profiles[profile] = true
		groups[profile+":"+b.RunSize]++
	}
	if len(profiles) != 1 || len(manifest.Baselines) != 3 {
		return false
	}
	for profile := range profiles {
		for _, runSize := range []string{"small", "medium", "full"} {
			if groups[profile+":"+runSize] != 1 {
				return false
			}
		}
	}
	return true
}

// ReadyForV7Production validates the SCORING-ENABLED aggregate GPT-OSS contract:
// the full identity gate plus scoring_enabled=true. This is the completeness
// gate the calibration tooling uses before it may emit an explicitly
// scoring-enabled candidate. The embedded production manifest ships
// scoring_enabled=false (the permanent v7 quality-only contract), so this
// returns false for it by design — production readiness flows through
// ReadyForV7QualityOnly.
func ReadyForV7Production(manifest Manifest) bool {
	return manifest.ScoringEnabled && v7ContractShapeValid(manifest)
}

// ReadyForV7QualityOnly validates the v7 QUALITY-ONLY production contract: the
// full model/route/embedding/dataset-hash identity gate is intact and enforced,
// but scoring_enabled MUST be false. Under quality-only, token usage never
// moves the composite (ApplyForVersion emits a neutral multiplier-1 record), so
// no scoring-enabled token baseline is required to admit or score a v7 run —
// yet the locked model identity (openai/gpt-oss-20b), the route/embedding
// profile, and dataset verification remain fail-closed. This is the predicate
// behind ProductionReadyForVersion(V7) and the v7 capability readiness.
func ReadyForV7QualityOnly(manifest Manifest) bool {
	return !manifest.ScoringEnabled && v7ContractShapeValid(manifest)
}

// ReadyForV8QualityOnly binds the immutable dataset and provider identities
// needed to run v8. It intentionally rejects any scorer-side token policy other
// than quality-only: model-use integrity and the dynamic relative efficiency
// bonus are platform-owned, so v8 never waits for or consumes a 60-run starter
// calibration campaign.
func ReadyForV8QualityOnly(contract QualityOnlyContract) bool {
	return contract.SchemaVersion == 1 &&
		contract.BenchVersion == protocol.BenchVersionV8 &&
		contract.DatasetKnownVector == v8DatasetKnownVector &&
		contract.ScorerTokenPolicy == v8ScorerTokenPolicy &&
		contract.EfficiencyAuthority == v8EfficiencyAuthority &&
		contract.Harness == (CalibrationRouteIdentity{
			Provider:        v7AggregateProvider,
			ProfileRevision: v7AggregateProfile,
			Model:           llm.V7HarnessModel,
		}) &&
		contract.Embedding == (EmbeddingContract{
			Provider: v7EmbeddingProvider, Model: v7EmbeddingModel,
			Profile: v7EmbeddingProfile, Dimensions: v7EmbeddingDimensions,
			CatalogSHA256: v7EmbeddingCatalogSHA256,
		})
}

func canonicalSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	for _, r := range value {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

func calibrationDatasetDigest(datasets []CalibrationDataset) string {
	h := sha256.New()
	for _, dataset := range datasets {
		_, _ = fmt.Fprintf(h, "%s:%d:%s\n", dataset.RunSize, dataset.Seed, dataset.DatasetSHA256)
	}
	return fmt.Sprintf("%x", h.Sum(nil))
}

func baselineID(benchVersion int, b Baseline) string {
	if benchVersion == protocol.BenchVersionV7 {
		canonical := fmt.Sprintf("%s:%s:%s:%s:%s:%d:%d:%d:%d:%d:%d:%d:%s:%s", FormulaVersion, b.RunSize, b.Provider,
			b.ProfileRevision, b.Model, b.RawReferencePromptTokens, b.RawReferenceCompletionTokens,
			b.RawReferenceTotalTokens, b.AllowanceMultiplierBPS, b.AllowancePromptTokens,
			b.AllowanceCompletionTokens, b.AllowanceTotalTokens, b.AllowancePolicy, b.StarterKitRevision)
		sum := sha256.Sum256([]byte(canonical))
		return fmt.Sprintf("v%d-starter-p90-%x", benchVersion, sum[:8])
	}
	canonical := fmt.Sprintf("%s:%s:%s:%s:%s:%d:%d:%d:%s", FormulaVersion, b.RunSize, b.Provider,
		b.ProfileRevision, b.Model, b.PromptTokens, b.CompletionTokens, b.TotalTokens, b.StarterKitRevision)
	sum := sha256.Sum256([]byte(canonical))
	return fmt.Sprintf("v%d-starter-p90-%x", benchVersion, sum[:8])
}

// BaselineID is the canonical content-derived baseline id (exported for the
// calibration-transfer tooling, which must mint ids the same way the measured
// pipeline does).
func BaselineID(benchVersion int, b Baseline) string { return baselineID(benchVersion, b) }

func validV7CalibrationDatasets(datasets []CalibrationDataset) bool {
	if len(datasets) != 60 {
		return false
	}
	counts := map[string]int{}
	seenSeeds := map[string]bool{}
	for _, dataset := range datasets {
		if dataset.RunSize != "small" && dataset.RunSize != "medium" && dataset.RunSize != "full" {
			return false
		}
		if len(dataset.DatasetSHA256) != sha256.Size*2 {
			return false
		}
		for _, r := range dataset.DatasetSHA256 {
			if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
				return false
			}
		}
		seedKey := fmt.Sprintf("%s:%d", dataset.RunSize, dataset.Seed)
		if seenSeeds[seedKey] {
			return false
		}
		seenSeeds[seedKey] = true
		counts[dataset.RunSize]++
	}
	return counts["small"] == 20 && counts["medium"] == 20 && counts["full"] == 20
}

func Lookup(runSize string, usage protocol.TokenUsage) (Baseline, bool) {
	return LookupForVersion(protocol.BenchVersionV5, runSize, usage)
}

// LookupForVersion selects only the reviewed manifest for the requested model
// epoch. v6 intentionally retains the historical v5 Qwen calibration.
func LookupForVersion(benchVersion int, runSize string, usage protocol.TokenUsage) (Baseline, bool) {
	manifest := productionManifest
	baselineVersion := benchVersion
	switch benchVersion {
	case protocol.BenchVersionV5:
	case protocol.BenchVersionV6:
		baselineVersion = protocol.BenchVersionV5
	case protocol.BenchVersionV7:
		manifest = productionV7Manifest
	default:
		return Baseline{}, false
	}
	if !ProductionReadyForVersion(benchVersion) {
		return Baseline{}, false
	}
	for _, b := range manifest.Baselines {
		if b.BenchVersion == baselineVersion && b.RunSize == runSize &&
			b.Provider == usage.Provider && b.ProfileRevision == usage.ProfileRevision &&
			b.Model == usage.Model && b.StarterKitRevision == manifest.StarterKitRevision && validBaseline(b) {
			return b, true
		}
	}
	return Baseline{}, false
}

func validBaseline(b Baseline) bool {
	if b.BenchVersion == protocol.BenchVersionV7 {
		return validV7Allowance(b)
	}
	return b.ID != "" && b.Samples >= 20 && b.Aggregation == "nearest_rank_p90" &&
		b.PromptTokens > 0 && b.CompletionTokens > 0 &&
		b.TotalTokens == b.PromptTokens+b.CompletionTokens && b.StarterKitRevision != ""
}

func validV7Allowance(b Baseline) bool {
	if b.ID == "" || b.Samples != 20 || b.Aggregation != "nearest_rank_p90" ||
		b.StarterKitRevision == "" || b.PromptTokens != 0 || b.CompletionTokens != 0 || b.TotalTokens != 0 ||
		b.RawReferencePromptTokens == 0 || b.RawReferenceCompletionTokens == 0 ||
		b.RawReferenceTotalTokens != b.RawReferencePromptTokens+b.RawReferenceCompletionTokens ||
		b.AllowanceMultiplierBPS != v7AllowanceMultiplierBPS || b.AllowancePolicy != v7AllowancePolicy {
		return false
	}
	if b.RawReferenceTotalTokens > ^uint64(0)/v7AllowanceMultiplierBPS ||
		b.RawReferencePromptTokens > ^uint64(0)/v7AllowanceMultiplierBPS {
		return false
	}
	wantTotal := b.RawReferenceTotalTokens * v7AllowanceMultiplierBPS / 10_000
	wantPrompt := b.RawReferencePromptTokens * v7AllowanceMultiplierBPS / 10_000
	return b.AllowanceTotalTokens == wantTotal && b.AllowancePromptTokens == wantPrompt &&
		b.AllowanceCompletionTokens == wantTotal-wantPrompt &&
		b.AllowanceTotalTokens == b.AllowancePromptTokens+b.AllowanceCompletionTokens
}

func allowanceTokens(b Baseline) (uint64, uint64, uint64) {
	if b.BenchVersion == protocol.BenchVersionV7 {
		return b.AllowancePromptTokens, b.AllowanceCompletionTokens, b.AllowanceTotalTokens
	}
	return b.PromptTokens, b.CompletionTokens, b.TotalTokens
}

// Apply returns the complete audit record and mutates no report. Callers assign
// AdjustedComposite to ScoreReport.Composite only for bench v5.
func Apply(raw protocol.ScoreReport, usage protocol.TokenUsage, baseline *Baseline) protocol.TokenEfficiency {
	result := protocol.TokenEfficiency{
		FormulaVersion:           FormulaVersion,
		BudgetPercentile:         BudgetPercentile,
		ObservedPromptTokens:     usage.PromptTokens,
		ObservedCompletionTokens: usage.CompletionTokens,
		ObservedTotalTokens:      usage.TotalTokens,
		MaximumPenalty:           MaximumPenalty,
		MinimumMultiplier:        MinMultiplier,
		Multiplier:               1,
		RawComposite:             raw.Composite,
		AdjustedComposite:        raw.Composite,
		DecisionReason:           "within_budget",
	}

	if !ValidUsage(usage) {
		result.DecisionReason = "token_telemetry_unavailable"
		return result
	}
	if baseline == nil || !validBaseline(*baseline) {
		result.DecisionReason = "baseline_unavailable"
		return result
	}
	result.BaselineID = baseline.ID
	allowancePrompt, allowanceCompletion, allowanceTotal := allowanceTokens(*baseline)
	result.BaselinePromptTokens = allowancePrompt
	result.BaselineCompletionTokens = allowanceCompletion
	result.BaselineTotalTokens = allowanceTotal
	if usage.TotalTokens <= allowanceTotal {
		return result
	}

	ratio := float64(usage.TotalTokens) / float64(allowanceTotal)
	result.ExcessRatio = ratio - 1
	// A one-sided rational curve is neutral through the generous p90 budget,
	// then approaches (but never crosses) the 0.90 floor as waste grows.
	result.Multiplier = 1 - MaximumPenalty*(result.ExcessRatio/(1+result.ExcessRatio))
	if math.IsNaN(result.Multiplier) || math.IsInf(result.Multiplier, 0) {
		result.Multiplier = 1
		result.ExcessRatio = 0
		result.DecisionReason = "token_transform_unavailable"
		return result
	}
	result.Multiplier = math.Max(MinMultiplier, result.Multiplier)
	result.PenaltyApplied = true
	result.DecisionReason = "above_budget"
	result.AdjustedComposite = round6(raw.Composite * result.Multiplier)
	return result
}

// ApplyForVersion selects the token contract for a bench version. v5/v6 are
// byte-identical to Apply. v7 is the QUALITY-ONLY contract: the returned
// record is always neutral (multiplier 1, composite untouched) and exists
// purely to carry the audited observed usage and the explicit decision reason
// into the report — token usage is recorded, never scored, for v7. The
// reviewed manifest's budgets/allowances are reference identity only.
func ApplyForVersion(benchVersion int, raw protocol.ScoreReport, usage protocol.TokenUsage, baseline *Baseline) protocol.TokenEfficiency {
	if benchVersion >= protocol.BenchVersionV7 {
		return protocol.TokenEfficiency{
			FormulaVersion:           V7QualityOnlyFormula,
			ObservedPromptTokens:     usage.PromptTokens,
			ObservedCompletionTokens: usage.CompletionTokens,
			ObservedTotalTokens:      usage.TotalTokens,
			MinimumMultiplier:        1,
			Multiplier:               1,
			RawComposite:             raw.Composite,
			AdjustedComposite:        raw.Composite,
			DecisionReason:           V7QualityOnlyReason,
		}
	}
	return Apply(raw, usage, baseline)
}

// ValidUsage applies the same completeness and arithmetic checks to scoring
// and starter-kit calibration inputs.
func ValidUsage(usage protocol.TokenUsage) bool {
	if usage.Status != "complete" || usage.Successes == 0 ||
		usage.UsageAvailable != usage.Successes || usage.UsageUnavailable != 0 ||
		usage.TotalTokens == 0 || usage.PromptTokens == 0 ||
		usage.PromptTokens > ^uint64(0)-usage.CompletionTokens {
		return false
	}
	return usage.TotalTokens == usage.PromptTokens+usage.CompletionTokens
}

func round6(value float64) float64 { return math.Round(value*1e6) / 1e6 }
