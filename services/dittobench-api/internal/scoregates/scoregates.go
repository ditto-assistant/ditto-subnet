// Package scoregates defines the pure, versioned gate evidence used by
// DittoBench v9. It deliberately does not collect telemetry: callers must pass
// trusted, case-level observations after excluding cases the validator could
// not fairly administer.
package scoregates

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strings"
)

const (
	SchemaVersion   = 1
	BenchVersionV9  = 9
	BenchVersionV10 = 10
	BenchVersionV11 = 11
	BenchVersionV12 = 12
	BasisPointScale = 10_000
	MaxCaseCount    = 10_000_000
	MaxUsageCount   = uint64(9_007_199_254_740_991)
)

// SupportedBenchVersion reports whether the gate contract applies to a bench
// version. The v9 contract carries forward unchanged to v10 and v11; the
// evidence records the run's actual version so digests stay version-bound. v12
// adds the causal model-dependence gate on top of the identical v9 stack: the
// extra gate is emitted, canonicalized, and multiplied into the composite only
// for bench_version>=12, so v9..v11 evidence and digests stay byte-identical.
func SupportedBenchVersion(benchVersion int) bool {
	return benchVersion >= BenchVersionV9 && benchVersion <= BenchVersionV12
}

var (
	ErrUnsupportedVersion   = errors.New("unsupported score-gate version")
	ErrTelemetryUnavailable = errors.New("score-gate telemetry unavailable")
	ErrInvalidEvidence      = errors.New("invalid score-gate evidence")
)

type Result string
type RolloutMode string

const (
	ResultPassed               Result = "passed"
	ResultBelowThreshold       Result = "below_threshold"
	ResultZeroInference        Result = "zero_inference"
	ResultInsufficientEvidence Result = "insufficient_evidence"
	ResultNotApplicable        Result = "not_applicable"
)

const (
	RolloutShadow  RolloutMode = "shadow"
	RolloutEnforce RolloutMode = "enforce"
)

var profileIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._:@/-]{0,127}$`)

type ThresholdProfile struct {
	ID             string `json:"id"`
	ManifestSHA256 string `json:"manifest_sha256"`
}

type Thresholds struct {
	Profile                      ThresholdProfile `json:"profile"`
	ModelUseCoverageBPS          int              `json:"model_use_coverage_bps"`
	AuthoritativeToolCoverageBPS int              `json:"authoritative_tool_coverage_bps"`
	// ModelDependenceCoverageBPS is the minimum fraction of the counterfactual
	// slice whose scored answer must CHANGE under a perturbed model completion.
	// It is defined only for bench_version>=12; pre-v12 thresholds must leave it
	// zero so their evidence and digests stay byte-identical.
	ModelDependenceCoverageBPS int `json:"model_dependence_coverage_bps,omitempty"`
}

type ExclusionCounts struct {
	Preflight      int `json:"preflight"`
	Ablation       int `json:"ablation"`
	Undelivered    int `json:"undelivered"`
	ValidatorFault int `json:"validator_fault"`
}

type ModelUseInput struct {
	AdministeredCases        int             `json:"administered_cases"`
	EligibleCases            int             `json:"eligible_cases"`
	SuccessfulInferenceCases int             `json:"successful_inference_cases"`
	ObservedRequests         uint64          `json:"observed_requests"`
	SuccessfulRequests       uint64          `json:"successful_requests"`
	PromptTokens             uint64          `json:"prompt_tokens"`
	CompletionTokens         uint64          `json:"completion_tokens"`
	Excluded                 ExclusionCounts `json:"excluded"`
	TelemetryComplete        bool            `json:"telemetry_complete"`
	// CaseAttributionComplete is true only when trusted runner/broker evidence
	// identifies the distinct administered cases that reached controlled
	// inference. Aggregate request counts cannot set it: repeated requests from
	// one case must never satisfy a multi-case coverage gate.
	CaseAttributionComplete bool `json:"case_attribution_complete"`
}

type AuthoritativeToolInput struct {
	ExpectedExecutions   int  `json:"expected_executions"`
	MatchedExecutions    int  `json:"matched_executions"`
	UnexpectedExecutions int  `json:"unexpected_executions"`
	TelemetryComplete    bool `json:"telemetry_complete"`
}

// ModelDependenceInput carries the trusted counterfactual observations that
// prove a run's scored answers are causally downstream of the model's
// completions (issue #532). On a deterministic seeded slice of scored cases,
// the relay records the scored answer under the clean model completion and
// again under a deterministically perturbed completion; a case is DEPENDENT
// when those two answers differ. A harness that computes answers in
// deterministic code (e.g. a KV-parser) ignores the completion, so its clean
// and counterfactual answers are identical and DependentCases stays zero.
//
// This gate is defined only for bench_version>=12. SliceAttributionComplete is
// the single trust bit the relay/validator must set: it is true only when the
// counterfactual comparison settled for every eligible slice case. Until the
// relay populates real per-case counterfactual evidence it stays false, and the
// gate fails OPEN (insufficient_evidence -> full factor): missing ablation
// telemetry is a validator gap, not proof the answers are model-independent.
type ModelDependenceInput struct {
	AdministeredCases        int  `json:"administered_cases"`
	EligibleCases            int  `json:"eligible_cases"`
	DependentCases           int  `json:"dependent_cases"`
	TelemetryComplete        bool `json:"telemetry_complete"`
	SliceAttributionComplete bool `json:"slice_attribution_complete"`
}

type ModelUseEvidence struct {
	AdministeredCases        int             `json:"administered_cases"`
	EligibleCases            int             `json:"eligible_cases"`
	SuccessfulInferenceCases int             `json:"successful_inference_cases"`
	MissingInferenceCases    int             `json:"missing_inference_cases"`
	ObservedRequests         uint64          `json:"observed_requests"`
	SuccessfulRequests       uint64          `json:"successful_requests"`
	PromptTokens             uint64          `json:"prompt_tokens"`
	CompletionTokens         uint64          `json:"completion_tokens"`
	Excluded                 ExclusionCounts `json:"excluded"`
	CaseAttributionComplete  bool            `json:"case_attribution_complete"`
	// RequestCoverageBPS is a shadow diagnostic derived from aggregate
	// successful requests, capped by the eligible population. It never affects
	// the semantic factor unless distinct-case attribution is complete.
	RequestCoverageBPS int    `json:"request_coverage_bps"`
	CoverageBPS        int    `json:"coverage_bps"`
	ThresholdBPS       int    `json:"threshold_bps"`
	Result             Result `json:"result"`
	FactorBPS          int    `json:"factor_bps"`
}

type AuthoritativeToolEvidence struct {
	ExpectedExecutions   int    `json:"expected_executions"`
	MatchedExecutions    int    `json:"matched_executions"`
	MissingExecutions    int    `json:"missing_executions"`
	UnexpectedExecutions int    `json:"unexpected_executions"`
	ObservedExecutions   int    `json:"observed_executions"`
	CoverageBPS          int    `json:"coverage_bps"`
	ThresholdBPS         int    `json:"threshold_bps"`
	Result               Result `json:"result"`
	FactorBPS            int    `json:"factor_bps"`
}

// ModelDependenceEvidence is the signed, pure evidence for the v12 causal
// model-dependence gate. It mirrors the model_use schema so the public
// projection and validator consume it uniformly. DependenceBPS is the fraction
// of the eligible counterfactual slice whose scored answer changed under a
// perturbed completion. For bench_version<12 every field is the zero value and
// the gate is neither canonicalized nor multiplied into the composite.
type ModelDependenceEvidence struct {
	AdministeredCases        int    `json:"administered_cases"`
	EligibleCases            int    `json:"eligible_cases"`
	DependentCases           int    `json:"dependent_cases"`
	IndependentCases         int    `json:"independent_cases"`
	SliceAttributionComplete bool   `json:"slice_attribution_complete"`
	DependenceBPS            int    `json:"dependence_bps"`
	ThresholdBPS             int    `json:"threshold_bps"`
	Result                   Result `json:"result"`
	FactorBPS                int    `json:"factor_bps"`
}

type Evidence struct {
	SchemaVersion     int                       `json:"schema_version"`
	BenchVersion      int                       `json:"bench_version"`
	RolloutMode       RolloutMode               `json:"rollout_mode"`
	ThresholdProfile  ThresholdProfile          `json:"threshold_profile"`
	ModelUse          ModelUseEvidence          `json:"model_use"`
	AuthoritativeTool AuthoritativeToolEvidence `json:"authoritative_tool"`
	// ModelDependence is emitted only for bench_version>=12; it is the zero
	// value for v9..v11 and is excluded from those versions' canonical bytes.
	ModelDependence ModelDependenceEvidence `json:"model_dependence,omitzero"`
	// InferenceLatency is the v12 inference-latency review gate (see
	// inference_latency.go). It is attached after Build via AttachInferenceLatency
	// for bench_version>=12; it is the zero value ("not administered") for v9..v11
	// and for a v12 Evidence built without the gate, and in both cases it is
	// excluded from the canonical bytes and never affects the combined factor.
	InferenceLatency InferenceLatencyEvidence `json:"inference_latency,omitzero"`
	// AnswerStuffing is the v12 Class-D answer-stuffing detection gate (see
	// answer_stuffing.go). It is attached after Build via AttachAnswerStuffing for
	// bench_version>=12; it is the zero value ("not administered") for v9..v11 and
	// for a v12 Evidence built without the gate, and in both cases it is excluded
	// from the canonical bytes and never affects the combined factor.
	AnswerStuffing AnswerStuffingEvidence `json:"answer_stuffing,omitzero"`
}

type Score struct {
	Composite       float64
	CompositeStderr float64
}

// Build assembles trusted, version-bound gate evidence. The optional dependence
// argument carries the v12 counterfactual observations: it must be absent for
// bench_version<12 and present exactly once for bench_version>=12. Keeping it
// variadic leaves every v9..v11 caller source- and behavior-identical.
func Build(benchVersion int, model ModelUseInput, tool AuthoritativeToolInput, thresholds Thresholds, mode RolloutMode, dependence ...ModelDependenceInput) (Evidence, error) {
	if !SupportedBenchVersion(benchVersion) {
		return Evidence{}, fmt.Errorf("%w: bench=%d", ErrUnsupportedVersion, benchVersion)
	}
	if err := validateThresholds(thresholds, benchVersion); err != nil {
		return Evidence{}, err
	}
	if mode != RolloutShadow && mode != RolloutEnforce {
		return Evidence{}, invalid("rollout mode must be shadow or enforce")
	}
	dependenceEvidence, err := buildModelDependenceForVersion(benchVersion, thresholds.ModelDependenceCoverageBPS, dependence)
	if err != nil {
		return Evidence{}, err
	}
	modelEvidence, err := buildModelUse(model, thresholds.ModelUseCoverageBPS)
	if err != nil {
		return Evidence{}, err
	}
	toolEvidence, err := buildAuthoritativeTool(tool, thresholds.AuthoritativeToolCoverageBPS)
	if err != nil {
		return Evidence{}, err
	}
	return Evidence{
		SchemaVersion: SchemaVersion, BenchVersion: benchVersion,
		RolloutMode: mode, ThresholdProfile: thresholds.Profile,
		ModelUse: modelEvidence, AuthoritativeTool: toolEvidence,
		ModelDependence: dependenceEvidence,
	}, nil
}

// buildModelDependenceForVersion enforces the version contract for the optional
// dependence input and delegates to buildModelDependence for bench_version>=12.
func buildModelDependenceForVersion(benchVersion, threshold int, dependence []ModelDependenceInput) (ModelDependenceEvidence, error) {
	if benchVersion < BenchVersionV12 {
		if len(dependence) != 0 {
			return ModelDependenceEvidence{}, invalid("model-dependence evidence is defined only for bench v%d and later", BenchVersionV12)
		}
		return ModelDependenceEvidence{}, nil
	}
	if len(dependence) != 1 {
		return ModelDependenceEvidence{}, fmt.Errorf("%w: model-dependence", ErrTelemetryUnavailable)
	}
	return buildModelDependence(dependence[0], threshold)
}

// buildModelDependence turns the trusted counterfactual slice observations into
// pure, version-bound evidence. Incomplete telemetry (TelemetryComplete=false)
// is a hard error. A settled slice with no dependent answers (a KV-parser)
// publishes a zero factor. An unsettled slice fails OPEN: insufficient_evidence
// with a full factor, so an honest run is never zeroed because a counterfactual
// re-run timed out or overflowed the ticket-budget guard.
func buildModelDependence(in ModelDependenceInput, threshold int) (ModelDependenceEvidence, error) {
	if !in.TelemetryComplete {
		return ModelDependenceEvidence{}, fmt.Errorf("%w: model-dependence", ErrTelemetryUnavailable)
	}
	caseCounts := []struct {
		name  string
		count int
	}{
		{"dependence.administered_cases", in.AdministeredCases},
		{"dependence.eligible_cases", in.EligibleCases},
		{"dependence.dependent_cases", in.DependentCases},
	}
	for _, item := range caseCounts {
		if err := validateBoundedCases(item.name, item.count); err != nil {
			return ModelDependenceEvidence{}, err
		}
	}
	if in.EligibleCases > in.AdministeredCases {
		return ModelDependenceEvidence{}, invalid("dependence eligible_cases exceed administered_cases")
	}
	if in.DependentCases > in.EligibleCases {
		return ModelDependenceEvidence{}, invalid("dependence dependent_cases exceed eligible_cases")
	}
	e := ModelDependenceEvidence{
		AdministeredCases: in.AdministeredCases, EligibleCases: in.EligibleCases,
		DependentCases: in.DependentCases, IndependentCases: in.EligibleCases - in.DependentCases,
		SliceAttributionComplete: in.SliceAttributionComplete, ThresholdBPS: threshold,
	}
	if !in.SliceAttributionComplete {
		// Fail OPEN: without a settled per-case counterfactual comparison the
		// gate cannot prove independence, and a detection gate must never zero
		// an honest run for missing validator/relay ablation telemetry.
		e.Result, e.FactorBPS, e.DependenceBPS = ResultInsufficientEvidence, BasisPointScale, 0
		return e, nil
	}
	if in.EligibleCases == 0 {
		// The counterfactual slice settled but had no scored model cases to
		// perturb; the model_use gate already governs the zero-inference outcome.
		e.DependenceBPS, e.Result, e.FactorBPS = BasisPointScale, ResultNotApplicable, BasisPointScale
		return e, nil
	}
	e.DependenceBPS = coverageBPS(in.DependentCases, in.EligibleCases)
	if e.DependenceBPS < threshold {
		e.Result, e.FactorBPS = ResultBelowThreshold, 0
	} else {
		e.Result, e.FactorBPS = ResultPassed, BasisPointScale
	}
	return e, nil
}

func buildModelUse(in ModelUseInput, threshold int) (ModelUseEvidence, error) {
	if !in.TelemetryComplete {
		return ModelUseEvidence{}, fmt.Errorf("%w: model-use", ErrTelemetryUnavailable)
	}
	if err := validateCases(in.AdministeredCases, in.EligibleCases, in.SuccessfulInferenceCases, in.Excluded); err != nil {
		return ModelUseEvidence{}, err
	}
	usageCounts := []struct {
		name  string
		count uint64
	}{
		{"observed_requests", in.ObservedRequests},
		{"successful_requests", in.SuccessfulRequests},
		{"prompt_tokens", in.PromptTokens},
		{"completion_tokens", in.CompletionTokens},
	}
	for _, item := range usageCounts {
		if item.count > MaxUsageCount {
			return ModelUseEvidence{}, invalid("%s exceeds %d", item.name, MaxUsageCount)
		}
	}
	if in.SuccessfulRequests > in.ObservedRequests {
		return ModelUseEvidence{}, invalid("successful_requests exceeds observed_requests")
	}
	if uint64(in.SuccessfulInferenceCases) > in.SuccessfulRequests {
		return ModelUseEvidence{}, invalid("successful_inference_cases exceeds successful_requests")
	}
	if in.SuccessfulRequests > 0 && in.PromptTokens == 0 {
		return ModelUseEvidence{}, invalid("successful requests require prompt token accounting")
	}
	if in.SuccessfulRequests == 0 && (in.PromptTokens != 0 || in.CompletionTokens != 0) {
		return ModelUseEvidence{}, invalid("tokens require a successful request")
	}

	e := ModelUseEvidence{
		AdministeredCases: in.AdministeredCases, EligibleCases: in.EligibleCases,
		SuccessfulInferenceCases: in.SuccessfulInferenceCases,
		MissingInferenceCases:    in.EligibleCases - in.SuccessfulInferenceCases,
		ObservedRequests:         in.ObservedRequests, SuccessfulRequests: in.SuccessfulRequests,
		PromptTokens: in.PromptTokens, CompletionTokens: in.CompletionTokens,
		Excluded: in.Excluded, CaseAttributionComplete: in.CaseAttributionComplete,
		ThresholdBPS: threshold,
	}
	if in.EligibleCases == 0 {
		e.CoverageBPS, e.RequestCoverageBPS, e.Result, e.FactorBPS = BasisPointScale, BasisPointScale, ResultNotApplicable, BasisPointScale
		return e, nil
	}
	requestCovered := in.SuccessfulRequests
	if requestCovered > uint64(in.EligibleCases) {
		requestCovered = uint64(in.EligibleCases)
	}
	e.RequestCoverageBPS = coverageBPS(int(requestCovered), in.EligibleCases)
	if in.ObservedRequests == 0 && in.SuccessfulRequests == 0 && in.SuccessfulInferenceCases == 0 && in.PromptTokens == 0 && in.CompletionTokens == 0 {
		e.Result, e.FactorBPS = ResultZeroInference, 0
		return e, nil
	}
	if !in.CaseAttributionComplete {
		if in.SuccessfulInferenceCases != 0 {
			return ModelUseEvidence{}, invalid("successful inference cases require complete distinct-case attribution")
		}
		e.Result, e.FactorBPS = ResultInsufficientEvidence, 0
		e.CoverageBPS = 0
		return e, nil
	}
	e.CoverageBPS = coverageBPS(in.SuccessfulInferenceCases, in.EligibleCases)
	if e.CoverageBPS < threshold {
		e.Result, e.FactorBPS = ResultBelowThreshold, 0
	} else {
		e.Result, e.FactorBPS = ResultPassed, BasisPointScale
	}
	return e, nil
}

func buildAuthoritativeTool(in AuthoritativeToolInput, threshold int) (AuthoritativeToolEvidence, error) {
	if !in.TelemetryComplete {
		return AuthoritativeToolEvidence{}, fmt.Errorf("%w: authoritative-tool", ErrTelemetryUnavailable)
	}
	if err := validateBoundedCases("expected_executions", in.ExpectedExecutions); err != nil {
		return AuthoritativeToolEvidence{}, err
	}
	if err := validateBoundedCases("matched_executions", in.MatchedExecutions); err != nil {
		return AuthoritativeToolEvidence{}, err
	}
	if err := validateBoundedCases("unexpected_executions", in.UnexpectedExecutions); err != nil {
		return AuthoritativeToolEvidence{}, err
	}
	if in.MatchedExecutions > in.ExpectedExecutions {
		return AuthoritativeToolEvidence{}, invalid("matched_executions exceeds expected_executions")
	}
	if in.MatchedExecutions+in.UnexpectedExecutions > MaxCaseCount {
		return AuthoritativeToolEvidence{}, invalid("observed executions exceeds %d", MaxCaseCount)
	}
	e := AuthoritativeToolEvidence{
		ExpectedExecutions: in.ExpectedExecutions, MatchedExecutions: in.MatchedExecutions,
		MissingExecutions:    in.ExpectedExecutions - in.MatchedExecutions,
		UnexpectedExecutions: in.UnexpectedExecutions,
		ObservedExecutions:   in.MatchedExecutions + in.UnexpectedExecutions,
		ThresholdBPS:         threshold,
	}
	if in.ExpectedExecutions == 0 {
		e.CoverageBPS, e.Result, e.FactorBPS = BasisPointScale, ResultNotApplicable, BasisPointScale
		return e, nil
	}
	if in.ExpectedExecutions > 0 {
		e.CoverageBPS = coverageBPS(in.MatchedExecutions, in.ExpectedExecutions)
	}
	if e.CoverageBPS < threshold {
		e.Result, e.FactorBPS = ResultBelowThreshold, 0
	} else {
		e.Result, e.FactorBPS = ResultPassed, BasisPointScale
	}
	return e, nil
}

func (e Evidence) Validate() error {
	if e.SchemaVersion != SchemaVersion || !SupportedBenchVersion(e.BenchVersion) {
		return fmt.Errorf("%w: schema=%d bench=%d", ErrUnsupportedVersion, e.SchemaVersion, e.BenchVersion)
	}
	var dependence []ModelDependenceInput
	if e.BenchVersion >= BenchVersionV12 {
		dependence = []ModelDependenceInput{{
			AdministeredCases:        e.ModelDependence.AdministeredCases,
			EligibleCases:            e.ModelDependence.EligibleCases,
			DependentCases:           e.ModelDependence.DependentCases,
			TelemetryComplete:        true,
			SliceAttributionComplete: e.ModelDependence.SliceAttributionComplete,
		}}
	}
	want, err := Build(
		e.BenchVersion,
		ModelUseInput{
			AdministeredCases: e.ModelUse.AdministeredCases, EligibleCases: e.ModelUse.EligibleCases,
			SuccessfulInferenceCases: e.ModelUse.SuccessfulInferenceCases,
			ObservedRequests:         e.ModelUse.ObservedRequests, SuccessfulRequests: e.ModelUse.SuccessfulRequests,
			PromptTokens: e.ModelUse.PromptTokens, CompletionTokens: e.ModelUse.CompletionTokens,
			Excluded: e.ModelUse.Excluded, TelemetryComplete: true,
			CaseAttributionComplete: e.ModelUse.CaseAttributionComplete,
		},
		AuthoritativeToolInput{
			ExpectedExecutions:   e.AuthoritativeTool.ExpectedExecutions,
			MatchedExecutions:    e.AuthoritativeTool.MatchedExecutions,
			UnexpectedExecutions: e.AuthoritativeTool.UnexpectedExecutions,
			TelemetryComplete:    true,
		},
		Thresholds{
			Profile:                      e.ThresholdProfile,
			ModelUseCoverageBPS:          e.ModelUse.ThresholdBPS,
			AuthoritativeToolCoverageBPS: e.AuthoritativeTool.ThresholdBPS,
			ModelDependenceCoverageBPS:   e.ModelDependence.ThresholdBPS,
		},
		e.RolloutMode,
		dependence...,
	)
	if err != nil {
		return err
	}
	// The inference-latency gate is attached after Build (AttachInferenceLatency),
	// so reconstruct it here from its own echoed inputs and fold it into want
	// before the equality check. A zero-value ("not administered") latency
	// evidence is left untouched, so a v12 Evidence built without the gate still
	// matches want exactly.
	if e.BenchVersion >= BenchVersionV12 && e.InferenceLatency.administered() {
		lat, latErr := buildInferenceLatency(InferenceLatencyInput{
			AdministeredCases:   e.InferenceLatency.AdministeredCases,
			EligibleCases:       e.InferenceLatency.EligibleCases,
			FlaggedCases:        e.InferenceLatency.FlaggedCases,
			FloorMS:             e.InferenceLatency.FloorMS,
			ShareThresholdBPS:   e.InferenceLatency.ThresholdBPS,
			Posture:             e.InferenceLatency.Posture,
			TelemetryComplete:   true,
			AttributionComplete: e.InferenceLatency.AttributionComplete,
		})
		if latErr != nil {
			return latErr
		}
		want.InferenceLatency = lat
	} else if e.InferenceLatency != (InferenceLatencyEvidence{}) {
		return invalid("inference-latency gate is defined only for bench v%d and later", BenchVersionV12)
	}
	// The answer-stuffing gate is likewise attached after Build
	// (AttachAnswerStuffing); reconstruct it from its own echoed inputs and fold it
	// into want before the equality check. A zero-value ("not administered")
	// answer-stuffing evidence is left untouched, so a v12 Evidence built without
	// the gate still matches want exactly.
	if e.BenchVersion >= BenchVersionV12 && e.AnswerStuffing.administered() {
		stuffing, stuffErr := buildAnswerStuffing(AnswerStuffingInput{
			AdministeredCases:       e.AnswerStuffing.AdministeredCases,
			EligibleCases:           e.AnswerStuffing.EligibleCases,
			StuffedCases:            e.AnswerStuffing.StuffedCases,
			MinCases:                e.AnswerStuffing.MinCases,
			ShareThresholdBPS:       e.AnswerStuffing.ThresholdBPS,
			LooseEligibleCases:      e.AnswerStuffing.LooseEligibleCases,
			LooseStuffedCases:       e.AnswerStuffing.LooseStuffedCases,
			ReviewShareThresholdBPS: e.AnswerStuffing.ReviewShareThresholdBPS,
			Posture:                 e.AnswerStuffing.Posture,
			TelemetryComplete:       true,
			AttributionComplete:     e.AnswerStuffing.AttributionComplete,
			ReviewRequired:          e.AnswerStuffing.ReviewRequired,
		})
		if stuffErr != nil {
			return stuffErr
		}
		want.AnswerStuffing = stuffing
	} else if e.AnswerStuffing != (AnswerStuffingEvidence{}) {
		return invalid("answer-stuffing gate is defined only for bench v%d and later", BenchVersionV12)
	}
	if e != want {
		return invalid("derived evidence does not match trusted inputs")
	}
	return nil
}

func (e Evidence) CombinedFactorBPS() (int, error) {
	if err := e.Validate(); err != nil {
		return 0, err
	}
	if e.ModelUse.FactorBPS == 0 || e.AuthoritativeTool.FactorBPS == 0 {
		return 0, nil
	}
	// The causal model-dependence gate multiplies in alongside model_use and
	// authoritative_tool, but only for bench_version>=12 where it is emitted.
	if e.BenchVersion >= BenchVersionV12 && e.ModelDependence.FactorBPS == 0 {
		return 0, nil
	}
	// The inference-latency and answer-stuffing gates MULTIPLY in when administered
	// (bench_version>=12) rather than only zeroing, so a graduated factor reduces the
	// composite proportionally. Latency is binary today (full under review, zero
	// under enforce). Answer-stuffing is graduated under its default PENALIZE posture:
	// a capped, share-proportional factor that REDUCES -- never zeroes -- the
	// composite (zero only under the opt-in enforce posture; full under review). The
	// product preserves the binary gates (a full 10000 factor is identity, a 0 zeroes
	// the run) while letting the penalize factor scale the score.
	combined := BasisPointScale
	if e.BenchVersion >= BenchVersionV12 {
		if e.InferenceLatency.administered() {
			combined = combined * e.InferenceLatency.FactorBPS / BasisPointScale
		}
		if e.AnswerStuffing.administered() {
			combined = combined * e.AnswerStuffing.FactorBPS / BasisPointScale
		}
	}
	return combined, nil
}

func ApplyForVersion(benchVersion int, score Score, evidence *Evidence) (Score, error) {
	if err := validateScore(score); err != nil {
		return Score{}, err
	}
	if benchVersion < BenchVersionV9 {
		if evidence != nil {
			return Score{}, invalid("pre-v9 score must not include v9 gate evidence")
		}
		return score, nil
	}
	if !SupportedBenchVersion(benchVersion) {
		return Score{}, fmt.Errorf("%w: bench=%d", ErrUnsupportedVersion, benchVersion)
	}
	if evidence == nil {
		return Score{}, invalid("v9+ score requires gate evidence")
	}
	if evidence.BenchVersion != benchVersion {
		return Score{}, invalid("gate evidence bench version %d does not match score bench version %d", evidence.BenchVersion, benchVersion)
	}
	factor, err := evidence.CombinedFactorBPS()
	if err != nil {
		return Score{}, err
	}
	if evidence.RolloutMode == RolloutShadow {
		return score, nil
	}
	if factor == 0 {
		return Score{}, nil
	}
	if factor >= BasisPointScale {
		return score, nil
	}
	// Graduated penalty (answer-stuffing penalize posture): scale the composite by
	// the combined factor rather than passing it through unchanged.
	scale := float64(factor) / float64(BasisPointScale)
	return Score{Composite: score.Composite * scale, CompositeStderr: score.CompositeStderr * scale}, nil
}

func (e Evidence) CanonicalBytes() ([]byte, error) {
	if err := e.Validate(); err != nil {
		return nil, err
	}
	var b bytes.Buffer
	fmt.Fprintf(&b, "ditto-score-gates-v1\nschema_version=%d\nbench_version=%d\nrollout_mode=%s\n", e.SchemaVersion, e.BenchVersion, e.RolloutMode)
	fmt.Fprintf(&b, "threshold_profile.id=%s\nthreshold_profile.manifest_sha256=%s\n", e.ThresholdProfile.ID, e.ThresholdProfile.ManifestSHA256)
	fmt.Fprintf(&b, "model.administered_cases=%d\nmodel.eligible_cases=%d\nmodel.successful_inference_cases=%d\nmodel.missing_inference_cases=%d\n", e.ModelUse.AdministeredCases, e.ModelUse.EligibleCases, e.ModelUse.SuccessfulInferenceCases, e.ModelUse.MissingInferenceCases)
	fmt.Fprintf(&b, "model.observed_requests=%d\nmodel.successful_requests=%d\nmodel.prompt_tokens=%d\nmodel.completion_tokens=%d\n", e.ModelUse.ObservedRequests, e.ModelUse.SuccessfulRequests, e.ModelUse.PromptTokens, e.ModelUse.CompletionTokens)
	fmt.Fprintf(&b, "model.excluded.preflight=%d\nmodel.excluded.ablation=%d\nmodel.excluded.undelivered=%d\nmodel.excluded.validator_fault=%d\n", e.ModelUse.Excluded.Preflight, e.ModelUse.Excluded.Ablation, e.ModelUse.Excluded.Undelivered, e.ModelUse.Excluded.ValidatorFault)
	fmt.Fprintf(&b, "model.case_attribution_complete=%t\nmodel.request_coverage_bps=%d\nmodel.coverage_bps=%d\nmodel.threshold_bps=%d\nmodel.result=%s\nmodel.factor_bps=%d\n", e.ModelUse.CaseAttributionComplete, e.ModelUse.RequestCoverageBPS, e.ModelUse.CoverageBPS, e.ModelUse.ThresholdBPS, e.ModelUse.Result, e.ModelUse.FactorBPS)
	fmt.Fprintf(&b, "tool.expected_executions=%d\ntool.matched_executions=%d\ntool.missing_executions=%d\ntool.unexpected_executions=%d\ntool.observed_executions=%d\n", e.AuthoritativeTool.ExpectedExecutions, e.AuthoritativeTool.MatchedExecutions, e.AuthoritativeTool.MissingExecutions, e.AuthoritativeTool.UnexpectedExecutions, e.AuthoritativeTool.ObservedExecutions)
	fmt.Fprintf(&b, "tool.coverage_bps=%d\ntool.threshold_bps=%d\ntool.result=%s\ntool.factor_bps=%d\n", e.AuthoritativeTool.CoverageBPS, e.AuthoritativeTool.ThresholdBPS, e.AuthoritativeTool.Result, e.AuthoritativeTool.FactorBPS)
	// The v12 causal model-dependence gate is appended only for bench_version>=12
	// so v9..v11 canonical bytes, digests, and signatures stay byte-identical.
	if e.BenchVersion >= BenchVersionV12 {
		fmt.Fprintf(&b, "model_dependence.administered_cases=%d\nmodel_dependence.eligible_cases=%d\nmodel_dependence.dependent_cases=%d\nmodel_dependence.independent_cases=%d\n", e.ModelDependence.AdministeredCases, e.ModelDependence.EligibleCases, e.ModelDependence.DependentCases, e.ModelDependence.IndependentCases)
		fmt.Fprintf(&b, "model_dependence.slice_attribution_complete=%t\nmodel_dependence.dependence_bps=%d\nmodel_dependence.threshold_bps=%d\nmodel_dependence.result=%s\nmodel_dependence.factor_bps=%d\n", e.ModelDependence.SliceAttributionComplete, e.ModelDependence.DependenceBPS, e.ModelDependence.ThresholdBPS, e.ModelDependence.Result, e.ModelDependence.FactorBPS)
	}
	// The v12 inference-latency gate is appended only for bench_version>=12 AND
	// only when administered, so v9..v11 canonical bytes -- and a v12 Evidence
	// built without the gate -- stay byte-identical.
	if e.BenchVersion >= BenchVersionV12 && e.InferenceLatency.administered() {
		fmt.Fprintf(&b, "inference_latency.administered_cases=%d\ninference_latency.eligible_cases=%d\ninference_latency.flagged_cases=%d\ninference_latency.unflagged_cases=%d\n", e.InferenceLatency.AdministeredCases, e.InferenceLatency.EligibleCases, e.InferenceLatency.FlaggedCases, e.InferenceLatency.UnflaggedCases)
		fmt.Fprintf(&b, "inference_latency.floor_ms=%d\ninference_latency.attribution_complete=%t\ninference_latency.posture=%s\ninference_latency.sub_floor_bps=%d\ninference_latency.threshold_bps=%d\ninference_latency.result=%s\ninference_latency.factor_bps=%d\n", e.InferenceLatency.FloorMS, e.InferenceLatency.AttributionComplete, e.InferenceLatency.Posture, e.InferenceLatency.SubFloorBPS, e.InferenceLatency.ThresholdBPS, e.InferenceLatency.Result, e.InferenceLatency.FactorBPS)
	}
	// The v12 answer-stuffing gate is appended only for bench_version>=12 AND only
	// when administered, so v9..v11 canonical bytes -- and a v12 Evidence built
	// without the gate -- stay byte-identical.
	if e.BenchVersion >= BenchVersionV12 && e.AnswerStuffing.administered() {
		fmt.Fprintf(&b, "answer_stuffing.administered_cases=%d\nanswer_stuffing.eligible_cases=%d\nanswer_stuffing.stuffed_cases=%d\nanswer_stuffing.clean_cases=%d\n", e.AnswerStuffing.AdministeredCases, e.AnswerStuffing.EligibleCases, e.AnswerStuffing.StuffedCases, e.AnswerStuffing.CleanCases)
		fmt.Fprintf(&b, "answer_stuffing.attribution_complete=%t\nanswer_stuffing.review_required=%t\nanswer_stuffing.posture=%s\nanswer_stuffing.stuffed_bps=%d\nanswer_stuffing.threshold_bps=%d\nanswer_stuffing.min_cases=%d\n", e.AnswerStuffing.AttributionComplete, e.AnswerStuffing.ReviewRequired, e.AnswerStuffing.Posture, e.AnswerStuffing.StuffedBPS, e.AnswerStuffing.ThresholdBPS, e.AnswerStuffing.MinCases)
		fmt.Fprintf(&b, "answer_stuffing.loose_eligible_cases=%d\nanswer_stuffing.loose_stuffed_cases=%d\nanswer_stuffing.loose_stuffed_bps=%d\nanswer_stuffing.review_share_threshold_bps=%d\n", e.AnswerStuffing.LooseEligibleCases, e.AnswerStuffing.LooseStuffedCases, e.AnswerStuffing.LooseStuffedBPS, e.AnswerStuffing.ReviewShareThresholdBPS)
		fmt.Fprintf(&b, "answer_stuffing.result=%s\nanswer_stuffing.factor_bps=%d\n", e.AnswerStuffing.Result, e.AnswerStuffing.FactorBPS)
	}
	return b.Bytes(), nil
}

func (e Evidence) Digest() ([32]byte, error) {
	b, err := e.CanonicalBytes()
	if err != nil {
		return [32]byte{}, err
	}
	return sha256.Sum256(b), nil
}

func (e Evidence) DigestHex() (string, error) {
	d, err := e.Digest()
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(d[:]), nil
}

func (e Evidence) SignatureInput() (string, error) {
	digest, err := e.DigestHex()
	if err != nil {
		return "", err
	}
	return "score-gates-v1:" + digest, nil
}

func validateThresholds(t Thresholds, benchVersion int) error {
	if !profileIDPattern.MatchString(t.Profile.ID) {
		return invalid("threshold profile id must match %s", profileIDPattern)
	}
	if len(t.Profile.ManifestSHA256) != sha256.Size*2 {
		return invalid("threshold profile manifest_sha256 must be 64 lowercase hex characters")
	}
	if _, err := hex.DecodeString(t.Profile.ManifestSHA256); err != nil || t.Profile.ManifestSHA256 != strings.ToLower(t.Profile.ManifestSHA256) {
		return invalid("threshold profile manifest_sha256 must be 64 lowercase hex characters")
	}
	if t.ModelUseCoverageBPS < 1 || t.ModelUseCoverageBPS > BasisPointScale {
		return invalid("model-use threshold must be in [1,%d]", BasisPointScale)
	}
	if t.AuthoritativeToolCoverageBPS < 1 || t.AuthoritativeToolCoverageBPS > BasisPointScale {
		return invalid("authoritative-tool threshold must be in [1,%d]", BasisPointScale)
	}
	if benchVersion >= BenchVersionV12 {
		if t.ModelDependenceCoverageBPS < 1 || t.ModelDependenceCoverageBPS > BasisPointScale {
			return invalid("model-dependence threshold must be in [1,%d]", BasisPointScale)
		}
	} else if t.ModelDependenceCoverageBPS != 0 {
		return invalid("model-dependence threshold is defined only for bench v%d and later", BenchVersionV12)
	}
	return nil
}

func validateCases(administered, eligible, successful int, excluded ExclusionCounts) error {
	caseCounts := []struct {
		name  string
		count int
	}{
		{"administered_cases", administered},
		{"eligible_cases", eligible},
		{"successful_inference_cases", successful},
		{"excluded.preflight", excluded.Preflight},
		{"excluded.ablation", excluded.Ablation},
		{"excluded.undelivered", excluded.Undelivered},
		{"excluded.validator_fault", excluded.ValidatorFault},
	}
	for _, item := range caseCounts {
		if err := validateBoundedCases(item.name, item.count); err != nil {
			return err
		}
	}
	excludedTotal := excluded.Preflight + excluded.Ablation + excluded.Undelivered + excluded.ValidatorFault
	if excludedTotal > MaxCaseCount || eligible+excludedTotal != administered {
		return invalid("eligible cases and exclusions must partition administered cases")
	}
	if successful > eligible {
		return invalid("successful_inference_cases exceeds eligible_cases")
	}
	return nil
}

func validateBoundedCases(name string, count int) error {
	if count < 0 || count > MaxCaseCount {
		return invalid("%s must be in [0,%d]", name, MaxCaseCount)
	}
	return nil
}

func validateScore(score Score) error {
	scoreFields := []struct {
		name  string
		value float64
	}{
		{"composite", score.Composite},
		{"composite_stderr", score.CompositeStderr},
	}
	for _, field := range scoreFields {
		if math.IsNaN(field.value) || math.IsInf(field.value, 0) || field.value < 0 || field.value > 1 {
			return invalid("%s must be finite and in [0,1]", field.name)
		}
	}
	return nil
}

func coverageBPS(numerator, denominator int) int { return numerator * BasisPointScale / denominator }

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalidEvidence, fmt.Sprintf(format, args...))
}
