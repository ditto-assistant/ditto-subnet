package scoregates

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func validThresholdsV12() Thresholds {
	t := validThresholds()
	t.ModelDependenceCoverageBPS = 9_000
	return t
}

// dependenceInput models the relay's counterfactual slice: eligible cases were
// each scored under the clean model completion and a perturbed completion, and
// dependent cases are those whose scored answer changed.
func dependenceInput(eligible, dependent int) ModelDependenceInput {
	return ModelDependenceInput{
		AdministeredCases: eligible + 2, EligibleCases: eligible, DependentCases: dependent,
		TelemetryComplete: true, SliceAttributionComplete: true,
	}
}

func buildV12(t *testing.T, mode RolloutMode, dep ModelDependenceInput) Evidence {
	t.Helper()
	e, err := Build(BenchVersionV12, validModelInput(), validToolInput(), validThresholdsV12(), mode, dep)
	if err != nil {
		t.Fatalf("Build(v12) error = %v", err)
	}
	return e
}

// A genuine agent's answers track the model completions: every eligible case in
// the counterfactual slice moves, so the dependence gate passes and the
// composite is preserved under enforce.
func TestModelDependenceGenuineAgentPasses(t *testing.T) {
	e := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	if e.ModelDependence.Result != ResultPassed || e.ModelDependence.FactorBPS != BasisPointScale {
		t.Fatalf("dependence result/factor = %s/%d, want passed/%d", e.ModelDependence.Result, e.ModelDependence.FactorBPS, BasisPointScale)
	}
	if e.ModelDependence.DependenceBPS != BasisPointScale || e.ModelDependence.IndependentCases != 0 {
		t.Fatalf("dependence bps/independent = %d/%d", e.ModelDependence.DependenceBPS, e.ModelDependence.IndependentCases)
	}
	if err := e.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want %d, nil", factor, err, BasisPointScale)
	}
	score := Score{Composite: 0.997012, CompositeStderr: 0.01}
	got, err := ApplyForVersion(BenchVersionV12, score, &e)
	if err != nil {
		t.Fatalf("ApplyForVersion() error = %v", err)
	}
	if got != score {
		t.Fatalf("genuine agent score = %+v, want %+v", got, score)
	}
}

// A KV-parser fires model calls (so model_use and authoritative_tool both pass)
// but computes answers deterministically, ignoring the completions. Its clean
// and counterfactual answers are identical, so no eligible case moves: the
// dependence gate zeroes the composite even though the ordinary score is 0.997.
func TestModelDependenceKVParserGatedToZero(t *testing.T) {
	e := buildV12(t, RolloutEnforce, dependenceInput(10, 0))
	if e.ModelUse.FactorBPS != BasisPointScale || e.AuthoritativeTool.FactorBPS != BasisPointScale {
		t.Fatalf("KV-parser should still satisfy model_use/tool: %d/%d", e.ModelUse.FactorBPS, e.AuthoritativeTool.FactorBPS)
	}
	if e.ModelDependence.Result != ResultBelowThreshold || e.ModelDependence.FactorBPS != 0 {
		t.Fatalf("dependence result/factor = %s/%d, want below_threshold/0", e.ModelDependence.Result, e.ModelDependence.FactorBPS)
	}
	if e.ModelDependence.DependenceBPS != 0 || e.ModelDependence.IndependentCases != 10 {
		t.Fatalf("dependence bps/independent = %d/%d, want 0/10", e.ModelDependence.DependenceBPS, e.ModelDependence.IndependentCases)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 0, nil", factor, err)
	}
	got, err := ApplyForVersion(BenchVersionV12, Score{Composite: 0.997012, CompositeStderr: 0.01}, &e)
	if err != nil {
		t.Fatalf("ApplyForVersion() error = %v", err)
	}
	if got != (Score{}) {
		t.Fatalf("KV-parser score = %+v, want zero", got)
	}
}

func TestModelDependenceThresholdBoundaryIsInclusive(t *testing.T) {
	tests := []struct {
		name       string
		dependent  int
		threshold  int
		wantResult Result
		wantFactor int
	}{
		{name: "equal", dependent: 9, threshold: 9_000, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "one bp above coverage", dependent: 9, threshold: 9_001, wantResult: ResultBelowThreshold, wantFactor: 0},
		{name: "all required", dependent: 10, threshold: 10_000, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "all required but one independent", dependent: 9, threshold: 10_000, wantResult: ResultBelowThreshold, wantFactor: 0},
		{name: "prove any dependence", dependent: 1, threshold: 1, wantResult: ResultPassed, wantFactor: BasisPointScale},
		{name: "none dependent", dependent: 0, threshold: 1, wantResult: ResultBelowThreshold, wantFactor: 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			thresholds := validThresholdsV12()
			thresholds.ModelDependenceCoverageBPS = tt.threshold
			e, err := Build(BenchVersionV12, validModelInput(), validToolInput(), thresholds, RolloutEnforce, dependenceInput(10, tt.dependent))
			if err != nil {
				t.Fatalf("Build() error = %v", err)
			}
			if e.ModelDependence.Result != tt.wantResult || e.ModelDependence.FactorBPS != tt.wantFactor {
				t.Fatalf("result/factor = %s/%d, want %s/%d", e.ModelDependence.Result, e.ModelDependence.FactorBPS, tt.wantResult, tt.wantFactor)
			}
		})
	}
}

func TestModelDependenceFailsClosedWithoutSettledAttribution(t *testing.T) {
	dep := dependenceInput(10, 10)
	dep.SliceAttributionComplete = false
	e := buildV12(t, RolloutEnforce, dep)
	if e.ModelDependence.Result != ResultInsufficientEvidence || e.ModelDependence.FactorBPS != 0 || e.ModelDependence.DependenceBPS != 0 {
		t.Fatalf("unsettled attribution not fail-closed: %+v", e.ModelDependence)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want 0, nil", factor, err)
	}
}

func TestModelDependenceEmptySliceIsNeutralOnceSettled(t *testing.T) {
	e := buildV12(t, RolloutEnforce, dependenceInput(0, 0))
	if e.ModelDependence.Result != ResultNotApplicable || e.ModelDependence.FactorBPS != BasisPointScale {
		t.Fatalf("empty settled slice not neutral: %+v", e.ModelDependence)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want %d, nil", factor, err, BasisPointScale)
	}
}

func TestModelDependenceRequiredForV12(t *testing.T) {
	_, err := Build(BenchVersionV12, validModelInput(), validToolInput(), validThresholdsV12(), RolloutEnforce)
	if !errors.Is(err, ErrTelemetryUnavailable) {
		t.Fatalf("missing dependence input error = %v, want telemetry unavailable", err)
	}
	dep := dependenceInput(10, 10)
	dep.TelemetryComplete = false
	_, err = Build(BenchVersionV12, validModelInput(), validToolInput(), validThresholdsV12(), RolloutEnforce, dep)
	if !errors.Is(err, ErrTelemetryUnavailable) {
		t.Fatalf("incomplete dependence telemetry error = %v, want telemetry unavailable", err)
	}
}

func TestModelDependenceThresholdRequiredForV12(t *testing.T) {
	thresholds := validThresholds() // no ModelDependenceCoverageBPS
	_, err := Build(BenchVersionV12, validModelInput(), validToolInput(), thresholds, RolloutEnforce, dependenceInput(10, 10))
	if !errors.Is(err, ErrInvalidEvidence) || !strings.Contains(err.Error(), "model-dependence threshold") {
		t.Fatalf("v12 without dependence threshold error = %v, want invalid dependence threshold", err)
	}
}

func TestModelDependenceRejectsInvalidCounts(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*ModelDependenceInput)
		text   string
	}{
		{name: "negative eligible", mutate: func(in *ModelDependenceInput) { in.EligibleCases = -1 }, text: "dependence.eligible_cases"},
		{name: "negative dependent", mutate: func(in *ModelDependenceInput) { in.DependentCases = -1 }, text: "dependence.dependent_cases"},
		{name: "eligible exceeds administered", mutate: func(in *ModelDependenceInput) { in.EligibleCases = in.AdministeredCases + 1 }, text: "eligible_cases exceed"},
		{name: "dependent exceeds eligible", mutate: func(in *ModelDependenceInput) { in.DependentCases = in.EligibleCases + 1 }, text: "dependent_cases exceed"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dep := dependenceInput(10, 10)
			tt.mutate(&dep)
			_, err := Build(BenchVersionV12, validModelInput(), validToolInput(), validThresholdsV12(), RolloutEnforce, dep)
			if !errors.Is(err, ErrInvalidEvidence) || !strings.Contains(err.Error(), tt.text) {
				t.Fatalf("Build() error = %v, want invalid %q", err, tt.text)
			}
		})
	}
}

func TestModelDependenceShadowPublishesFailureWithoutApplying(t *testing.T) {
	e := buildV12(t, RolloutShadow, dependenceInput(10, 0))
	if e.ModelDependence.Result != ResultBelowThreshold || e.ModelDependence.FactorBPS != 0 {
		t.Fatalf("shadow dependence result/factor = %s/%d", e.ModelDependence.Result, e.ModelDependence.FactorBPS)
	}
	score := Score{Composite: 0.5, CompositeStderr: 0.1}
	got, err := ApplyForVersion(BenchVersionV12, score, &e)
	if err != nil {
		t.Fatalf("ApplyForVersion() error = %v", err)
	}
	if got != score {
		t.Fatalf("shadow score changed: got %+v want %+v", got, score)
	}
}

func TestModelDependenceValidateRejectsTamper(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Evidence)
	}{
		{name: "dependent count", mutate: func(e *Evidence) { e.ModelDependence.DependentCases = 0 }},
		{name: "dependence bps", mutate: func(e *Evidence) { e.ModelDependence.DependenceBPS++ }},
		{name: "result", mutate: func(e *Evidence) { e.ModelDependence.Result = ResultBelowThreshold }},
		{name: "factor", mutate: func(e *Evidence) { e.ModelDependence.FactorBPS = 0 }},
		{name: "independent", mutate: func(e *Evidence) { e.ModelDependence.IndependentCases++ }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			e := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
			tt.mutate(&e)
			if err := e.Validate(); !errors.Is(err, ErrInvalidEvidence) {
				t.Fatalf("Validate() error = %v, want ErrInvalidEvidence", err)
			}
		})
	}
}

// v9..v11 must never carry, canonicalize, or be scored against dependence
// evidence. Pre-v12 Build must reject a dependence argument, and a stray
// dependence threshold on a pre-v12 contract is invalid.
func TestModelDependenceRejectedBeforeV12(t *testing.T) {
	for _, version := range []int{BenchVersionV9, BenchVersionV10, BenchVersionV11} {
		_, err := Build(version, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce, dependenceInput(10, 10))
		if !errors.Is(err, ErrInvalidEvidence) {
			t.Fatalf("v%d accepted dependence input: %v", version, err)
		}
		thresholds := validThresholds()
		thresholds.ModelDependenceCoverageBPS = 9_000
		_, err = Build(version, validModelInput(), validToolInput(), thresholds, RolloutEnforce)
		if !errors.Is(err, ErrInvalidEvidence) {
			t.Fatalf("v%d accepted dependence threshold: %v", version, err)
		}
	}
}

// v9..v11 evidence and its canonical bytes/digest must be byte-identical to the
// pre-v12 contract: no model_dependence lines, and a zero-value ModelDependence
// that never affects the combined factor.
func TestPreV12CanonicalOmitsDependence(t *testing.T) {
	for _, version := range []int{BenchVersionV9, BenchVersionV10, BenchVersionV11} {
		e, err := Build(version, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce)
		if err != nil {
			t.Fatalf("Build(v%d) error = %v", version, err)
		}
		if e.ModelDependence != (ModelDependenceEvidence{}) {
			t.Fatalf("v%d carried non-zero dependence: %+v", version, e.ModelDependence)
		}
		body, err := e.CanonicalBytes()
		if err != nil {
			t.Fatalf("CanonicalBytes(v%d) error = %v", version, err)
		}
		if strings.Contains(string(body), "model_dependence") {
			t.Fatalf("v%d canonical bytes leaked model_dependence:\n%s", version, body)
		}
		if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
			t.Fatalf("v%d combined factor = %d, %v; want %d, nil", version, factor, err, BasisPointScale)
		}
	}
}

func TestV12CanonicalIncludesDependenceAndChangesDigest(t *testing.T) {
	e := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	body, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	for _, want := range []string{
		"model_dependence.administered_cases=12",
		"model_dependence.eligible_cases=10",
		"model_dependence.dependent_cases=10",
		"model_dependence.independent_cases=0",
		"model_dependence.slice_attribution_complete=true",
		"model_dependence.dependence_bps=10000",
		"model_dependence.threshold_bps=9000",
		"model_dependence.result=passed",
		"model_dependence.factor_bps=10000",
	} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("v12 canonical bytes missing %q:\n%s", want, body)
		}
	}
	v12Digest, err := e.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex() error = %v", err)
	}
	// Same underlying model_use/tool inputs, stamped v11: dependence changes the
	// digest, proving the gate is bound into the signature for v12 only.
	v11, err := Build(BenchVersionV11, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce)
	if err != nil {
		t.Fatalf("Build(v11) error = %v", err)
	}
	v11Digest, err := v11.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex(v11) error = %v", err)
	}
	if v12Digest == v11Digest {
		t.Fatal("v12 dependence gate did not change the signed digest")
	}
}

func TestV12DigestChangesForEveryDependenceInputFamily(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	baseDigest, err := base.DigestHex()
	if err != nil {
		t.Fatalf("base DigestHex() error = %v", err)
	}
	tests := []struct {
		name string
		dep  ModelDependenceInput
	}{
		{name: "fewer dependent", dep: dependenceInput(10, 9)},
		{name: "different eligible", dep: dependenceInput(9, 9)},
		{name: "unsettled", dep: func() ModelDependenceInput {
			d := dependenceInput(10, 10)
			d.SliceAttributionComplete = false
			return d
		}()},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			e := buildV12(t, RolloutEnforce, tt.dep)
			digest, err := e.DigestHex()
			if err != nil {
				t.Fatalf("DigestHex() error = %v", err)
			}
			if digest == baseDigest {
				t.Fatalf("digest did not change after %s", tt.name)
			}
		})
	}
}

func TestV12EvidenceJSONRoundTrip(t *testing.T) {
	want := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	b, err := json.Marshal(want)
	if err != nil {
		t.Fatalf("json.Marshal() error = %v", err)
	}
	if !strings.Contains(string(b), "model_dependence") {
		t.Fatalf("v12 JSON omitted model_dependence: %s", b)
	}
	var got Evidence
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatalf("json.Unmarshal() error = %v", err)
	}
	if got != want {
		t.Fatalf("round trip differs:\n got: %+v\nwant: %+v", got, want)
	}
	if err := got.Validate(); err != nil {
		t.Fatalf("round-trip Validate() error = %v", err)
	}
}
