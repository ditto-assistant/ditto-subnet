package scoregates

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"sync"
	"testing"
)

func validModelInput() ModelUseInput {
	return ModelUseInput{
		AdministeredCases:        12,
		EligibleCases:            10,
		SuccessfulInferenceCases: 9,
		ObservedRequests:         13,
		SuccessfulRequests:       11,
		PromptTokens:             1_100,
		CompletionTokens:         220,
		Excluded: ExclusionCounts{
			Preflight:   1,
			Undelivered: 1,
		},
		TelemetryComplete:       true,
		CaseAttributionComplete: true,
	}
}

func validToolInput() AuthoritativeToolInput {
	return AuthoritativeToolInput{
		ExpectedExecutions: 10,
		MatchedExecutions:  9,
		TelemetryComplete:  true,
	}
}

func validThresholds() Thresholds {
	return Thresholds{
		Profile: ThresholdProfile{
			ID:             "v9-honest-v8-calibration-2026-08-08",
			ManifestSHA256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		},
		ModelUseCoverageBPS:          9_000,
		AuthoritativeToolCoverageBPS: 9_000,
	}
}

func mustEvidence(t *testing.T) Evidence {
	t.Helper()
	e, err := Build(BenchVersionV9, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	return e
}

func TestBuildPassingEvidence(t *testing.T) {
	e := mustEvidence(t)

	if e.SchemaVersion != SchemaVersion {
		t.Fatalf("SchemaVersion = %d, want %d", e.SchemaVersion, SchemaVersion)
	}
	if e.BenchVersion != BenchVersionV9 {
		t.Fatalf("BenchVersion = %d, want %d", e.BenchVersion, BenchVersionV9)
	}
	if e.RolloutMode != RolloutEnforce {
		t.Fatalf("RolloutMode = %s, want %s", e.RolloutMode, RolloutEnforce)
	}
	if e.ThresholdProfile != validThresholds().Profile {
		t.Fatalf("ThresholdProfile = %+v, want %+v", e.ThresholdProfile, validThresholds().Profile)
	}
	if e.ModelUse.MissingInferenceCases != 1 {
		t.Fatalf("MissingInferenceCases = %d, want 1", e.ModelUse.MissingInferenceCases)
	}
	if e.ModelUse.CoverageBPS != 9_000 {
		t.Fatalf("model coverage = %d, want 9000", e.ModelUse.CoverageBPS)
	}
	if e.ModelUse.Result != ResultPassed || e.ModelUse.FactorBPS != BasisPointScale {
		t.Fatalf("model result/factor = %s/%d", e.ModelUse.Result, e.ModelUse.FactorBPS)
	}
	if e.AuthoritativeTool.MissingExecutions != 1 {
		t.Fatalf("MissingExecutions = %d, want 1", e.AuthoritativeTool.MissingExecutions)
	}
	if e.AuthoritativeTool.ObservedExecutions != 9 {
		t.Fatalf("ObservedExecutions = %d, want 9", e.AuthoritativeTool.ObservedExecutions)
	}
	if e.AuthoritativeTool.CoverageBPS != 9_000 {
		t.Fatalf("tool coverage = %d, want 9000", e.AuthoritativeTool.CoverageBPS)
	}
	if e.AuthoritativeTool.Result != ResultPassed || e.AuthoritativeTool.FactorBPS != BasisPointScale {
		t.Fatalf("tool result/factor = %s/%d", e.AuthoritativeTool.Result, e.AuthoritativeTool.FactorBPS)
	}
	if err := e.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 10000, nil", factor, err)
	}
}

func TestModelUseCoverageUsesDistinctCasesNotRequests(t *testing.T) {
	in := validModelInput()
	in.SuccessfulInferenceCases = 8
	in.ObservedRequests = 10_000
	in.SuccessfulRequests = 9_999
	in.PromptTokens = 1_000_000

	e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if got := e.ModelUse.CoverageBPS; got != 8_000 {
		t.Fatalf("CoverageBPS = %d, want 8000; request retries must not inflate coverage", got)
	}
	if got := e.ModelUse.Result; got != ResultBelowThreshold {
		t.Fatalf("Result = %s, want %s", got, ResultBelowThreshold)
	}
	if got := e.ModelUse.FactorBPS; got != 0 {
		t.Fatalf("FactorBPS = %d, want 0", got)
	}
}

func TestAggregateRequestsCannotMasqueradeAsDistinctCaseCoverage(t *testing.T) {
	in := validModelInput()
	in.CaseAttributionComplete = false
	in.SuccessfulInferenceCases = 0
	in.EligibleCases = 10
	in.ObservedRequests = 10_000
	in.SuccessfulRequests = 9_999
	in.PromptTokens = 1_000_000

	e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutShadow)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.ModelUse.RequestCoverageBPS != BasisPointScale {
		t.Fatalf("request coverage = %d, want diagnostic 10000", e.ModelUse.RequestCoverageBPS)
	}
	if e.ModelUse.CoverageBPS != 0 || e.ModelUse.Result != ResultInsufficientEvidence || e.ModelUse.FactorBPS != 0 {
		t.Fatalf("aggregate requests satisfied semantic gate: %+v", e.ModelUse)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 0, nil", factor, err)
	}
}

func TestUnattributedAggregateCannotClaimSuccessfulCases(t *testing.T) {
	in := validModelInput()
	in.CaseAttributionComplete = false
	_, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutShadow)
	if !errors.Is(err, ErrInvalidEvidence) || !strings.Contains(err.Error(), "distinct-case attribution") {
		t.Fatalf("Build() error = %v, want distinct-case attribution rejection", err)
	}
}

func TestModelUseThresholdBoundaryIsInclusive(t *testing.T) {
	tests := []struct {
		name       string
		successful int
		threshold  int
		wantResult Result
		wantFactor int
	}{
		{name: "equal", successful: 9, threshold: 9_000, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "one basis point above computed coverage", successful: 9, threshold: 9_001, wantResult: ResultBelowThreshold, wantFactor: 0},
		{name: "minimum threshold", successful: 1, threshold: 1, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "perfect required", successful: 10, threshold: 10_000, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "perfect required but incomplete", successful: 9, threshold: 10_000, wantResult: ResultBelowThreshold, wantFactor: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			in := validModelInput()
			in.SuccessfulInferenceCases = tt.successful
			thresholds := validThresholds()
			thresholds.ModelUseCoverageBPS = tt.threshold
			e, err := Build(BenchVersionV9, in, validToolInput(), thresholds, RolloutEnforce)
			if err != nil {
				t.Fatalf("Build() error = %v", err)
			}
			if e.ModelUse.Result != tt.wantResult {
				t.Fatalf("Result = %s, want %s", e.ModelUse.Result, tt.wantResult)
			}
			if e.ModelUse.FactorBPS != tt.wantFactor {
				t.Fatalf("FactorBPS = %d, want %d", e.ModelUse.FactorBPS, tt.wantFactor)
			}
		})
	}
}

func TestZeroInferenceIsCompletedEvidenceWithZeroFactor(t *testing.T) {
	in := validModelInput()
	in.SuccessfulInferenceCases = 0
	in.ObservedRequests = 0
	in.SuccessfulRequests = 0
	in.PromptTokens = 0
	in.CompletionTokens = 0

	e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.ModelUse.Result != ResultZeroInference {
		t.Fatalf("Result = %s, want %s", e.ModelUse.Result, ResultZeroInference)
	}
	if e.ModelUse.FactorBPS != 0 {
		t.Fatalf("FactorBPS = %d, want 0", e.ModelUse.FactorBPS)
	}
	if e.ModelUse.MissingInferenceCases != e.ModelUse.EligibleCases {
		t.Fatalf("missing = %d, eligible = %d", e.ModelUse.MissingInferenceCases, e.ModelUse.EligibleCases)
	}
	if err := e.Validate(); err != nil {
		t.Fatalf("zero-inference evidence must remain valid and signable: %v", err)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 0, nil", factor, err)
	}
	score, err := ApplyForVersion(BenchVersionV9, Score{Composite: 0.75, CompositeStderr: 0.1}, &e)
	if err != nil {
		t.Fatalf("ApplyForVersion() error = %v", err)
	}
	if score != (Score{}) {
		t.Fatalf("zero-inference score = %+v, want zero score", score)
	}
}

func TestAttemptedButUnsuccessfulInferenceIsBelowThreshold(t *testing.T) {
	in := validModelInput()
	in.SuccessfulInferenceCases = 0
	in.ObservedRequests = 4
	in.SuccessfulRequests = 0
	in.PromptTokens = 0
	in.CompletionTokens = 0

	e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.ModelUse.Result != ResultBelowThreshold {
		t.Fatalf("Result = %s, want %s", e.ModelUse.Result, ResultBelowThreshold)
	}
	if e.ModelUse.FactorBPS != 0 {
		t.Fatalf("FactorBPS = %d, want 0", e.ModelUse.FactorBPS)
	}
}

func TestNoEligibleModelCasesIsNotApplicable(t *testing.T) {
	in := ModelUseInput{
		AdministeredCases: 4,
		Excluded: ExclusionCounts{
			Preflight:      1,
			Ablation:       1,
			Undelivered:    1,
			ValidatorFault: 1,
		},
		TelemetryComplete: true,
	}

	e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.ModelUse.Result != ResultNotApplicable {
		t.Fatalf("Result = %s, want %s", e.ModelUse.Result, ResultNotApplicable)
	}
	if e.ModelUse.CoverageBPS != BasisPointScale || e.ModelUse.FactorBPS != BasisPointScale {
		t.Fatalf("coverage/factor = %d/%d, want neutral", e.ModelUse.CoverageBPS, e.ModelUse.FactorBPS)
	}
}

func TestEveryExclusionCategoryParticipatesInPartition(t *testing.T) {
	tests := []struct {
		name     string
		excluded ExclusionCounts
	}{
		{name: "preflight", excluded: ExclusionCounts{Preflight: 2}},
		{name: "ablation", excluded: ExclusionCounts{Ablation: 2}},
		{name: "undelivered", excluded: ExclusionCounts{Undelivered: 2}},
		{name: "validator fault", excluded: ExclusionCounts{ValidatorFault: 2}},
		{name: "mixed", excluded: ExclusionCounts{Preflight: 1, Ablation: 1, Undelivered: 1, ValidatorFault: 1}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			in := validModelInput()
			in.Excluded = tt.excluded
			total := tt.excluded.Preflight + tt.excluded.Ablation + tt.excluded.Undelivered + tt.excluded.ValidatorFault
			in.AdministeredCases = in.EligibleCases + total
			e, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
			if err != nil {
				t.Fatalf("Build() error = %v", err)
			}
			if e.ModelUse.Excluded != tt.excluded {
				t.Fatalf("Excluded = %+v, want %+v", e.ModelUse.Excluded, tt.excluded)
			}
		})
	}
}

func TestRejectsInvalidModelInputs(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*ModelUseInput)
		is     error
		text   string
	}{
		{name: "telemetry incomplete", mutate: func(in *ModelUseInput) { in.TelemetryComplete = false }, is: ErrTelemetryUnavailable, text: "model-use"},
		{name: "negative administered", mutate: func(in *ModelUseInput) { in.AdministeredCases = -1 }, is: ErrInvalidEvidence, text: "administered_cases"},
		{name: "administered too large", mutate: func(in *ModelUseInput) { in.AdministeredCases = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "administered_cases"},
		{name: "negative eligible", mutate: func(in *ModelUseInput) { in.EligibleCases = -1 }, is: ErrInvalidEvidence, text: "eligible_cases"},
		{name: "eligible too large", mutate: func(in *ModelUseInput) { in.EligibleCases = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "eligible_cases"},
		{name: "negative successful cases", mutate: func(in *ModelUseInput) { in.SuccessfulInferenceCases = -1 }, is: ErrInvalidEvidence, text: "successful_inference_cases"},
		{name: "successful cases too large", mutate: func(in *ModelUseInput) { in.SuccessfulInferenceCases = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "successful_inference_cases"},
		{name: "negative preflight", mutate: func(in *ModelUseInput) { in.Excluded.Preflight = -1 }, is: ErrInvalidEvidence, text: "excluded.preflight"},
		{name: "negative ablation", mutate: func(in *ModelUseInput) { in.Excluded.Ablation = -1 }, is: ErrInvalidEvidence, text: "excluded.ablation"},
		{name: "negative undelivered", mutate: func(in *ModelUseInput) { in.Excluded.Undelivered = -1 }, is: ErrInvalidEvidence, text: "excluded.undelivered"},
		{name: "negative validator fault", mutate: func(in *ModelUseInput) { in.Excluded.ValidatorFault = -1 }, is: ErrInvalidEvidence, text: "excluded.validator_fault"},
		{name: "partition short", mutate: func(in *ModelUseInput) { in.AdministeredCases++ }, is: ErrInvalidEvidence, text: "partition"},
		{name: "partition long", mutate: func(in *ModelUseInput) { in.AdministeredCases-- }, is: ErrInvalidEvidence, text: "partition"},
		{name: "success exceeds eligible", mutate: func(in *ModelUseInput) { in.SuccessfulInferenceCases = in.EligibleCases + 1 }, is: ErrInvalidEvidence, text: "eligible_cases"},
		{name: "success cases exceeds requests", mutate: func(in *ModelUseInput) { in.SuccessfulRequests = 8 }, is: ErrInvalidEvidence, text: "successful_requests"},
		{name: "success requests exceeds observed", mutate: func(in *ModelUseInput) { in.SuccessfulRequests = 14 }, is: ErrInvalidEvidence, text: "observed_requests"},
		{name: "observed requests too large", mutate: func(in *ModelUseInput) { in.ObservedRequests = MaxUsageCount + 1 }, is: ErrInvalidEvidence, text: "observed_requests"},
		{name: "successful requests too large", mutate: func(in *ModelUseInput) { in.SuccessfulRequests = MaxUsageCount + 1 }, is: ErrInvalidEvidence, text: "successful_requests"},
		{name: "prompt tokens too large", mutate: func(in *ModelUseInput) { in.PromptTokens = MaxUsageCount + 1 }, is: ErrInvalidEvidence, text: "prompt_tokens"},
		{name: "completion tokens too large", mutate: func(in *ModelUseInput) { in.CompletionTokens = MaxUsageCount + 1 }, is: ErrInvalidEvidence, text: "completion_tokens"},
		{name: "successful request without prompt tokens", mutate: func(in *ModelUseInput) { in.PromptTokens = 0 }, is: ErrInvalidEvidence, text: "prompt token"},
		{name: "tokens without successful request", mutate: func(in *ModelUseInput) { in.SuccessfulInferenceCases = 0; in.SuccessfulRequests = 0 }, is: ErrInvalidEvidence, text: "tokens require"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			in := validModelInput()
			tt.mutate(&in)
			_, err := Build(BenchVersionV9, in, validToolInput(), validThresholds(), RolloutEnforce)
			if !errors.Is(err, tt.is) {
				t.Fatalf("Build() error = %v, want errors.Is(%v)", err, tt.is)
			}
			if !strings.Contains(err.Error(), tt.text) {
				t.Fatalf("Build() error = %q, want substring %q", err, tt.text)
			}
		})
	}
}

func TestRejectsInvalidThresholds(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Thresholds)
		text   string
	}{
		{name: "zero model", mutate: func(v *Thresholds) { v.ModelUseCoverageBPS = 0 }, text: "model-use"},
		{name: "negative model", mutate: func(v *Thresholds) { v.ModelUseCoverageBPS = -1 }, text: "model-use"},
		{name: "model above scale", mutate: func(v *Thresholds) { v.ModelUseCoverageBPS = BasisPointScale + 1 }, text: "model-use"},
		{name: "zero tool", mutate: func(v *Thresholds) { v.AuthoritativeToolCoverageBPS = 0 }, text: "authoritative-tool"},
		{name: "negative tool", mutate: func(v *Thresholds) { v.AuthoritativeToolCoverageBPS = -1 }, text: "authoritative-tool"},
		{name: "tool above scale", mutate: func(v *Thresholds) { v.AuthoritativeToolCoverageBPS = BasisPointScale + 1 }, text: "authoritative-tool"},
		{name: "missing profile id", mutate: func(v *Thresholds) { v.Profile.ID = "" }, text: "profile id"},
		{name: "profile id newline", mutate: func(v *Thresholds) { v.Profile.ID = "v9\nforged" }, text: "profile id"},
		{name: "profile id uppercase", mutate: func(v *Thresholds) { v.Profile.ID = "V9-profile" }, text: "profile id"},
		{name: "profile id too long", mutate: func(v *Thresholds) { v.Profile.ID = strings.Repeat("a", 129) }, text: "profile id"},
		{name: "missing manifest digest", mutate: func(v *Thresholds) { v.Profile.ManifestSHA256 = "" }, text: "manifest_sha256"},
		{name: "short manifest digest", mutate: func(v *Thresholds) { v.Profile.ManifestSHA256 = strings.Repeat("a", 63) }, text: "manifest_sha256"},
		{name: "long manifest digest", mutate: func(v *Thresholds) { v.Profile.ManifestSHA256 = strings.Repeat("a", 65) }, text: "manifest_sha256"},
		{name: "non-hex manifest digest", mutate: func(v *Thresholds) { v.Profile.ManifestSHA256 = strings.Repeat("g", 64) }, text: "manifest_sha256"},
		{name: "uppercase manifest digest", mutate: func(v *Thresholds) { v.Profile.ManifestSHA256 = strings.Repeat("A", 64) }, text: "manifest_sha256"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			thresholds := validThresholds()
			tt.mutate(&thresholds)
			_, err := Build(BenchVersionV9, validModelInput(), validToolInput(), thresholds, RolloutEnforce)
			if !errors.Is(err, ErrInvalidEvidence) {
				t.Fatalf("Build() error = %v, want ErrInvalidEvidence", err)
			}
			if !strings.Contains(err.Error(), tt.text) {
				t.Fatalf("Build() error = %q, want substring %q", err, tt.text)
			}
		})
	}
}

func TestRejectsInvalidRolloutMode(t *testing.T) {
	for _, mode := range []RolloutMode{"", "off", "ENFORCE", "shadow\nforged"} {
		t.Run(string(mode), func(t *testing.T) {
			_, err := Build(BenchVersionV9, validModelInput(), validToolInput(), validThresholds(), mode)
			if !errors.Is(err, ErrInvalidEvidence) {
				t.Fatalf("Build() error = %v, want ErrInvalidEvidence", err)
			}
			if !strings.Contains(err.Error(), "rollout mode") {
				t.Fatalf("Build() error = %q, want rollout mode", err)
			}
		})
	}
}

func TestAuthoritativeToolGate(t *testing.T) {
	tests := []struct {
		name      string
		input     AuthoritativeToolInput
		threshold int
		coverage  int
		result    Result
		factor    int
		missing   int
		observed  int
	}{
		{name: "exact", input: AuthoritativeToolInput{10, 10, 0, true}, threshold: 10_000, coverage: 10_000, result: ResultPassed, factor: 10_000, observed: 10},
		{name: "threshold inclusive", input: AuthoritativeToolInput{10, 9, 0, true}, threshold: 9_000, coverage: 9_000, result: ResultPassed, factor: 10_000, missing: 1, observed: 9},
		{name: "below threshold", input: AuthoritativeToolInput{10, 8, 0, true}, threshold: 9_000, coverage: 8_000, result: ResultBelowThreshold, factor: 0, missing: 2, observed: 8},
		{name: "unexpected is diagnostic with full matches", input: AuthoritativeToolInput{10, 10, 1, true}, threshold: 9_000, coverage: 10_000, result: ResultPassed, factor: 10_000, observed: 11},
		{name: "unexpected with no denominator", input: AuthoritativeToolInput{0, 0, 1, true}, threshold: 9_000, coverage: 10_000, result: ResultNotApplicable, factor: 10_000, observed: 1},
		{name: "nothing expected or observed", input: AuthoritativeToolInput{0, 0, 0, true}, threshold: 9_000, coverage: 10_000, result: ResultNotApplicable, factor: 10_000, observed: 0},
		{name: "one of three floors", input: AuthoritativeToolInput{3, 1, 0, true}, threshold: 3_333, coverage: 3_333, result: ResultPassed, factor: 10_000, missing: 2, observed: 1},
		{name: "one of three misses rounded up threshold", input: AuthoritativeToolInput{3, 1, 0, true}, threshold: 3_334, coverage: 3_333, result: ResultBelowThreshold, factor: 0, missing: 2, observed: 1},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			thresholds := validThresholds()
			thresholds.AuthoritativeToolCoverageBPS = tt.threshold
			e, err := Build(BenchVersionV9, validModelInput(), tt.input, thresholds, RolloutEnforce)
			if err != nil {
				t.Fatalf("Build() error = %v", err)
			}
			got := e.AuthoritativeTool
			if got.CoverageBPS != tt.coverage || got.Result != tt.result || got.FactorBPS != tt.factor {
				t.Fatalf("coverage/result/factor = %d/%s/%d, want %d/%s/%d", got.CoverageBPS, got.Result, got.FactorBPS, tt.coverage, tt.result, tt.factor)
			}
			if got.MissingExecutions != tt.missing || got.ObservedExecutions != tt.observed {
				t.Fatalf("missing/observed = %d/%d, want %d/%d", got.MissingExecutions, got.ObservedExecutions, tt.missing, tt.observed)
			}
		})
	}
}

func TestOptionalExtraToolExecutionCannotEraseCompleteCoverage(t *testing.T) {
	in := validToolInput()
	in.MatchedExecutions = in.ExpectedExecutions
	in.UnexpectedExecutions = 5

	e, err := Build(BenchVersionV9, validModelInput(), in, validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if got := e.AuthoritativeTool.CoverageBPS; got != BasisPointScale {
		t.Fatalf("CoverageBPS = %d, want %d", got, BasisPointScale)
	}
	if got := e.AuthoritativeTool.UnexpectedExecutions; got != 5 {
		t.Fatalf("UnexpectedExecutions = %d, want 5", got)
	}
	if got := e.AuthoritativeTool.Result; got != ResultPassed {
		t.Fatalf("Result = %s, want %s", got, ResultPassed)
	}
	if got := e.AuthoritativeTool.FactorBPS; got != BasisPointScale {
		t.Fatalf("FactorBPS = %d, want %d", got, BasisPointScale)
	}
}

func TestSelfReportedToolCallsCannotCreateAuthoritativeCredit(t *testing.T) {
	// A harness response may claim any number of calls, but this package has no
	// self-report input. With no validator-observed match, every expected
	// execution remains missing and the factor is zero.
	in := AuthoritativeToolInput{
		ExpectedExecutions: 10,
		MatchedExecutions:  0,
		TelemetryComplete:  true,
	}

	e, err := Build(BenchVersionV9, validModelInput(), in, validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.AuthoritativeTool.ObservedExecutions != 0 {
		t.Fatalf("ObservedExecutions = %d, want 0", e.AuthoritativeTool.ObservedExecutions)
	}
	if e.AuthoritativeTool.MissingExecutions != 10 {
		t.Fatalf("MissingExecutions = %d, want 10", e.AuthoritativeTool.MissingExecutions)
	}
	if e.AuthoritativeTool.FactorBPS != 0 {
		t.Fatalf("FactorBPS = %d, want 0", e.AuthoritativeTool.FactorBPS)
	}
}

func TestRejectsInvalidToolInputs(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*AuthoritativeToolInput)
		is     error
		text   string
	}{
		{name: "telemetry incomplete", mutate: func(in *AuthoritativeToolInput) { in.TelemetryComplete = false }, is: ErrTelemetryUnavailable, text: "authoritative-tool"},
		{name: "negative expected", mutate: func(in *AuthoritativeToolInput) { in.ExpectedExecutions = -1 }, is: ErrInvalidEvidence, text: "expected_executions"},
		{name: "expected too large", mutate: func(in *AuthoritativeToolInput) { in.ExpectedExecutions = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "expected_executions"},
		{name: "negative matched", mutate: func(in *AuthoritativeToolInput) { in.MatchedExecutions = -1 }, is: ErrInvalidEvidence, text: "matched_executions"},
		{name: "matched too large", mutate: func(in *AuthoritativeToolInput) { in.MatchedExecutions = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "matched_executions"},
		{name: "negative unexpected", mutate: func(in *AuthoritativeToolInput) { in.UnexpectedExecutions = -1 }, is: ErrInvalidEvidence, text: "unexpected_executions"},
		{name: "unexpected too large", mutate: func(in *AuthoritativeToolInput) { in.UnexpectedExecutions = MaxCaseCount + 1 }, is: ErrInvalidEvidence, text: "unexpected_executions"},
		{name: "matched exceeds expected", mutate: func(in *AuthoritativeToolInput) { in.MatchedExecutions = in.ExpectedExecutions + 1 }, is: ErrInvalidEvidence, text: "exceeds"},
		{name: "derived observed too large", mutate: func(in *AuthoritativeToolInput) {
			in.ExpectedExecutions = MaxCaseCount
			in.MatchedExecutions = MaxCaseCount
			in.UnexpectedExecutions = 1
		}, is: ErrInvalidEvidence, text: "observed executions"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			in := validToolInput()
			tt.mutate(&in)
			_, err := Build(BenchVersionV9, validModelInput(), in, validThresholds(), RolloutEnforce)
			if !errors.Is(err, tt.is) {
				t.Fatalf("Build() error = %v, want errors.Is(%v)", err, tt.is)
			}
			if !strings.Contains(err.Error(), tt.text) {
				t.Fatalf("Build() error = %q, want substring %q", err, tt.text)
			}
		})
	}
}

func TestValidateRejectsTamperedDerivedEvidence(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Evidence)
	}{
		{name: "model missing", mutate: func(e *Evidence) { e.ModelUse.MissingInferenceCases++ }},
		{name: "model coverage", mutate: func(e *Evidence) { e.ModelUse.CoverageBPS++ }},
		{name: "model result", mutate: func(e *Evidence) { e.ModelUse.Result = ResultBelowThreshold }},
		{name: "model factor", mutate: func(e *Evidence) { e.ModelUse.FactorBPS = 0 }},
		{name: "tool missing", mutate: func(e *Evidence) { e.AuthoritativeTool.MissingExecutions++ }},
		{name: "tool observed", mutate: func(e *Evidence) { e.AuthoritativeTool.ObservedExecutions++ }},
		{name: "tool coverage", mutate: func(e *Evidence) { e.AuthoritativeTool.CoverageBPS++ }},
		{name: "tool result", mutate: func(e *Evidence) { e.AuthoritativeTool.Result = ResultBelowThreshold }},
		{name: "tool factor", mutate: func(e *Evidence) { e.AuthoritativeTool.FactorBPS = 0 }},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			e := mustEvidence(t)
			tt.mutate(&e)
			err := e.Validate()
			if !errors.Is(err, ErrInvalidEvidence) {
				t.Fatalf("Validate() error = %v, want ErrInvalidEvidence", err)
			}
		})
	}
}

func TestInvalidTrustedEvidencePropagatesThroughEveryConsumer(t *testing.T) {
	e := mustEvidence(t)
	e.ModelUse.AdministeredCases++ // breaks the eligible-plus-exclusions partition

	if err := e.Validate(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("Validate() error = %v, want ErrInvalidEvidence", err)
	}
	if _, err := e.CombinedFactorBPS(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("CombinedFactorBPS() error = %v, want ErrInvalidEvidence", err)
	}
	if _, err := ApplyForVersion(BenchVersionV9, Score{Composite: 0.5}, &e); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("ApplyForVersion() error = %v, want ErrInvalidEvidence", err)
	}
}

func TestValidateRejectsUnsupportedEvidenceVersions(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Evidence)
	}{
		{name: "zero schema", mutate: func(e *Evidence) { e.SchemaVersion = 0 }},
		{name: "future schema", mutate: func(e *Evidence) { e.SchemaVersion++ }},
		{name: "v8", mutate: func(e *Evidence) { e.BenchVersion = 8 }},
		{name: "future bench", mutate: func(e *Evidence) { e.BenchVersion = 12 }},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			e := mustEvidence(t)
			tt.mutate(&e)
			if err := e.Validate(); !errors.Is(err, ErrUnsupportedVersion) {
				t.Fatalf("Validate() error = %v, want ErrUnsupportedVersion", err)
			}
		})
	}
}

func TestEvidenceJSONRoundTrip(t *testing.T) {
	want := mustEvidence(t)
	b, err := json.Marshal(want)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	var got Evidence
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if got != want {
		t.Fatalf("round trip evidence differs:\n got: %+v\nwant: %+v", got, want)
	}
	if err := got.Validate(); err != nil {
		t.Fatalf("round-trip Validate() error = %v", err)
	}
}

func TestCanonicalBytesGolden(t *testing.T) {
	e := mustEvidence(t)
	got, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	want := "" +
		"ditto-score-gates-v1\n" +
		"schema_version=1\n" +
		"bench_version=9\n" +
		"rollout_mode=enforce\n" +
		"threshold_profile.id=v9-honest-v8-calibration-2026-08-08\n" +
		"threshold_profile.manifest_sha256=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n" +
		"model.administered_cases=12\n" +
		"model.eligible_cases=10\n" +
		"model.successful_inference_cases=9\n" +
		"model.missing_inference_cases=1\n" +
		"model.observed_requests=13\n" +
		"model.successful_requests=11\n" +
		"model.prompt_tokens=1100\n" +
		"model.completion_tokens=220\n" +
		"model.excluded.preflight=1\n" +
		"model.excluded.ablation=0\n" +
		"model.excluded.undelivered=1\n" +
		"model.excluded.validator_fault=0\n" +
		"model.case_attribution_complete=true\n" +
		"model.request_coverage_bps=10000\n" +
		"model.coverage_bps=9000\n" +
		"model.threshold_bps=9000\n" +
		"model.result=passed\n" +
		"model.factor_bps=10000\n" +
		"tool.expected_executions=10\n" +
		"tool.matched_executions=9\n" +
		"tool.missing_executions=1\n" +
		"tool.unexpected_executions=0\n" +
		"tool.observed_executions=9\n" +
		"tool.coverage_bps=9000\n" +
		"tool.threshold_bps=9000\n" +
		"tool.result=passed\n" +
		"tool.factor_bps=10000\n"
	if string(got) != want {
		t.Fatalf("CanonicalBytes() mismatch:\n--- got ---\n%s--- want ---\n%s", got, want)
	}
}

func TestDigestAndSignatureInputGolden(t *testing.T) {
	e := mustEvidence(t)
	digest, err := e.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex() error = %v", err)
	}
	const wantDigest = "1247129dd087bb44443b040ad392bc13bc03b330701935a4885d1c11c36fadcb"
	if digest != wantDigest {
		t.Fatalf("DigestHex() = %q, want %q", digest, wantDigest)
	}
	signatureInput, err := e.SignatureInput()
	if err != nil {
		t.Fatalf("SignatureInput() error = %v", err)
	}
	if want := "score-gates-v1:" + wantDigest; signatureInput != want {
		t.Fatalf("SignatureInput() = %q, want %q", signatureInput, want)
	}
}

func TestCanonicalDigestChangesForEveryTrustedInputFamily(t *testing.T) {
	base := mustEvidence(t)
	baseDigest, err := base.DigestHex()
	if err != nil {
		t.Fatalf("base DigestHex() error = %v", err)
	}
	tests := []struct {
		name       string
		model      func(*ModelUseInput)
		tool       func(*AuthoritativeToolInput)
		thresholds func(*Thresholds)
		mode       RolloutMode
	}{
		{name: "model case population", model: func(in *ModelUseInput) {
			in.AdministeredCases++
			in.EligibleCases++
			in.SuccessfulInferenceCases++
			in.SuccessfulRequests++
		}},
		{name: "model observed requests", model: func(in *ModelUseInput) { in.ObservedRequests++ }},
		{name: "model prompt tokens", model: func(in *ModelUseInput) { in.PromptTokens++ }},
		{name: "model completion tokens", model: func(in *ModelUseInput) { in.CompletionTokens++ }},
		{name: "model exclusions", model: func(in *ModelUseInput) { in.Excluded.Preflight--; in.Excluded.Ablation++ }},
		{name: "tool population", tool: func(in *AuthoritativeToolInput) { in.ExpectedExecutions++; in.MatchedExecutions++ }},
		{name: "tool unexpected", tool: func(in *AuthoritativeToolInput) { in.UnexpectedExecutions++ }},
		{name: "threshold profile id", thresholds: func(in *Thresholds) { in.Profile.ID = "v9-honest-v8-calibration-revised" }},
		{name: "threshold manifest", thresholds: func(in *Thresholds) { in.Profile.ManifestSHA256 = strings.Repeat("a", 64) }},
		{name: "model threshold", thresholds: func(in *Thresholds) { in.ModelUseCoverageBPS++ }},
		{name: "tool threshold", thresholds: func(in *Thresholds) { in.AuthoritativeToolCoverageBPS++ }},
		{name: "rollout mode", mode: RolloutShadow},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			model := validModelInput()
			tool := validToolInput()
			thresholds := validThresholds()
			mode := RolloutEnforce
			if tt.model != nil {
				tt.model(&model)
			}
			if tt.tool != nil {
				tt.tool(&tool)
			}
			if tt.thresholds != nil {
				tt.thresholds(&thresholds)
			}
			if tt.mode != "" {
				mode = tt.mode
			}
			e, err := Build(BenchVersionV9, model, tool, thresholds, mode)
			if err != nil {
				t.Fatalf("Build() error = %v", err)
			}
			digest, err := e.DigestHex()
			if err != nil {
				t.Fatalf("DigestHex() error = %v", err)
			}
			if digest == baseDigest {
				t.Fatalf("digest did not change after %s mutation", tt.name)
			}
		})
	}
}

func TestCanonicalMethodsRejectTamperedEvidence(t *testing.T) {
	e := mustEvidence(t)
	e.ModelUse.FactorBPS = 0

	if _, err := e.CanonicalBytes(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("CanonicalBytes() error = %v, want ErrInvalidEvidence", err)
	}
	if _, err := e.Digest(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("Digest() error = %v, want ErrInvalidEvidence", err)
	}
	if _, err := e.DigestHex(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("DigestHex() error = %v, want ErrInvalidEvidence", err)
	}
	if _, err := e.SignatureInput(); !errors.Is(err, ErrInvalidEvidence) {
		t.Fatalf("SignatureInput() error = %v, want ErrInvalidEvidence", err)
	}
}

func TestCanonicalMethodsAreConcurrencySafe(t *testing.T) {
	e := mustEvidence(t)
	wantBytes, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	wantDigest, err := e.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex() error = %v", err)
	}

	const goroutines = 32
	const iterations = 100
	var wg sync.WaitGroup
	errs := make(chan string, goroutines)
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < iterations; j++ {
				gotBytes, err := e.CanonicalBytes()
				if err != nil || string(gotBytes) != string(wantBytes) {
					errs <- "canonical bytes changed"
					return
				}
				gotDigest, err := e.DigestHex()
				if err != nil || gotDigest != wantDigest {
					errs <- "digest changed"
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
}

func TestApplyForVersionPreservesPreV9ScoreExactly(t *testing.T) {
	score := Score{
		Composite:       math.Float64frombits(0x3fe0123456789abc),
		CompositeStderr: math.Float64frombits(0x3fa0123456789abc),
	}

	for _, version := range []int{0, 1, 7, 8} {
		t.Run(string(rune('A'+version)), func(t *testing.T) {
			got, err := ApplyForVersion(version, score, nil)
			if err != nil {
				t.Fatalf("ApplyForVersion(%d) error = %v", version, err)
			}
			if math.Float64bits(got.Composite) != math.Float64bits(score.Composite) {
				t.Fatalf("composite bits changed: got %x want %x", math.Float64bits(got.Composite), math.Float64bits(score.Composite))
			}
			if math.Float64bits(got.CompositeStderr) != math.Float64bits(score.CompositeStderr) {
				t.Fatalf("stderr bits changed: got %x want %x", math.Float64bits(got.CompositeStderr), math.Float64bits(score.CompositeStderr))
			}
		})
	}
}

func TestShadowPublishesFailureWithoutApplyingIt(t *testing.T) {
	model := validModelInput()
	model.SuccessfulInferenceCases = 8
	e, err := Build(BenchVersionV9, model, validToolInput(), validThresholds(), RolloutShadow)
	if err != nil {
		t.Fatalf("Build() error = %v", err)
	}
	if e.ModelUse.Result != ResultBelowThreshold || e.ModelUse.FactorBPS != 0 {
		t.Fatalf("shadow result/factor = %s/%d, want below_threshold/0", e.ModelUse.Result, e.ModelUse.FactorBPS)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 0, nil", factor, err)
	}
	score := Score{
		Composite:       math.Float64frombits(0x3fe0123456789abc),
		CompositeStderr: math.Float64frombits(0x3fa0123456789abc),
	}
	got, err := ApplyForVersion(BenchVersionV9, score, &e)
	if err != nil {
		t.Fatalf("ApplyForVersion() error = %v", err)
	}
	if math.Float64bits(got.Composite) != math.Float64bits(score.Composite) ||
		math.Float64bits(got.CompositeStderr) != math.Float64bits(score.CompositeStderr) {
		t.Fatalf("shadow score changed bits: got %+v want %+v", got, score)
	}
}

func TestApplyForVersionContracts(t *testing.T) {
	passing := mustEvidence(t)
	failing := passing
	failing.ModelUse.SuccessfulInferenceCases = 8
	failing.ModelUse.MissingInferenceCases = 2
	failing.ModelUse.CoverageBPS = 8_000
	failing.ModelUse.Result = ResultBelowThreshold
	failing.ModelUse.FactorBPS = 0
	shadowFailing := failing
	shadowFailing.RolloutMode = RolloutShadow
	passingV10 := passing
	passingV10.BenchVersion = BenchVersionV10
	score := Score{Composite: 0.75, CompositeStderr: 0.125}

	tests := []struct {
		name     string
		version  int
		evidence *Evidence
		want     Score
		is       error
	}{
		{name: "v8 rejects v9 evidence", version: 8, evidence: &passing, is: ErrInvalidEvidence},
		{name: "v9 requires evidence", version: 9, is: ErrInvalidEvidence},
		{name: "v9 passing exact", version: 9, evidence: &passing, want: score},
		{name: "v9 enforce failing zero", version: 9, evidence: &failing, want: Score{}},
		{name: "v9 shadow publishes failure but preserves score", version: 9, evidence: &shadowFailing, want: score},
		{name: "v10 passing exact", version: 10, evidence: &passingV10, want: score},
		{name: "v10 rejects v9-stamped evidence", version: 10, evidence: &passing, is: ErrInvalidEvidence},
		{name: "v9 rejects v10-stamped evidence", version: 9, evidence: &passingV10, is: ErrInvalidEvidence},
		{name: "future version rejected", version: 12, evidence: &passingV10, is: ErrUnsupportedVersion},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := ApplyForVersion(tt.version, score, tt.evidence)
			if !errors.Is(err, tt.is) {
				t.Fatalf("ApplyForVersion() error = %v, want errors.Is(%v)", err, tt.is)
			}
			if err == nil && got != tt.want {
				t.Fatalf("ApplyForVersion() = %+v, want %+v", got, tt.want)
			}
		})
	}
}

func TestApplyForVersionRejectsInvalidScores(t *testing.T) {
	e := mustEvidence(t)
	tests := []struct {
		name  string
		score Score
	}{
		{name: "composite NaN", score: Score{Composite: math.NaN()}},
		{name: "composite positive infinity", score: Score{Composite: math.Inf(1)}},
		{name: "composite negative infinity", score: Score{Composite: math.Inf(-1)}},
		{name: "composite negative", score: Score{Composite: -0.00001}},
		{name: "composite above one", score: Score{Composite: 1.00001}},
		{name: "stderr NaN", score: Score{Composite: 0.5, CompositeStderr: math.NaN()}},
		{name: "stderr positive infinity", score: Score{Composite: 0.5, CompositeStderr: math.Inf(1)}},
		{name: "stderr negative infinity", score: Score{Composite: 0.5, CompositeStderr: math.Inf(-1)}},
		{name: "stderr negative", score: Score{Composite: 0.5, CompositeStderr: -0.00001}},
		{name: "stderr above one", score: Score{Composite: 0.5, CompositeStderr: 1.00001}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			for _, version := range []int{8, 9} {
				var evidence *Evidence
				if version == 9 {
					evidence = &e
				}
				if _, err := ApplyForVersion(version, tt.score, evidence); !errors.Is(err, ErrInvalidEvidence) {
					t.Fatalf("ApplyForVersion(%d) error = %v, want ErrInvalidEvidence", version, err)
				}
			}
		})
	}
}
