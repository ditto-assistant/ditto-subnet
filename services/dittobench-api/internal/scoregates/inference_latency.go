package scoregates

// The v12 inference-latency gate is a cheap, high-yield tell against harnesses
// that emulate a model instead of running one (adversary review C2). A held
// agent answered scored cases in a flat ~119ms while the honest board runs
// 900-5000ms, because it parsed the answer in memory with no model call. A real
// gpt-oss-20b completion has a minimum-plausible wall time; a case that
// verifiably reached the model yet returned far below that floor is
// implausible, and a whole run whose inference-required, model-reached cases are
// mostly sub-floor is emulating.
//
// Relationship to the other model gates:
//
//   - model_use proves a case reached the relay at all (trusted request/success
//     windows). A parser that fakes token counts to satisfy it still cannot fake
//     the trusted validator-side wall clock this gate reads.
//   - model_dependence proves the scored answer is causally downstream of the
//     completion. It needs the counterfactual relay path; this gate needs only
//     the wall clock the runner already records, so it is the cheaper backstop.
//
// Posture: REVIEW by default, not gate-zero. Latency is gameable by burning
// wall-clock (sleeping to clear the floor), which already costs the curve-v3
// efficiency term, so the honest incentive is preserved without a hard gate.
// Zeroing on a soft, absolute floor also risks false-positives on a genuinely
// fast-but-honest agent. The gate therefore emits SIGNED evidence and routes a
// flagged run to human review while leaving the composite untouched; an operator
// can flip the posture to enforce (hard zero) once the honest-cohort sub-floor
// distribution has been measured in review mode -- the same shadow-before-enforce
// ladder the relay delay-fingerprint follows.

import "fmt"

// maxLatencyFloorMS bounds the configured plausibility floor. It sits well under
// the per-case budget (5 minutes): a floor above a minute would be nonsensical
// for a single completion and is rejected rather than silently clamped.
const maxLatencyFloorMS = 60_000

type LatencyPosture string

const (
	// LatencyReview emits the signed latency evidence and routes a flagged run to
	// human review WITHOUT zeroing the composite (FactorBPS stays full). Default.
	LatencyReview LatencyPosture = "review"
	// LatencyEnforce zeroes the composite when the sub-floor share crosses the
	// threshold, the same hard posture as model_dependence. Reserved for after the
	// honest-cohort sub-floor distribution is measured under review.
	LatencyEnforce LatencyPosture = "enforce"
)

// ResultLatencyImplausible marks a run whose inference-latency distribution
// crossed the sub-floor share threshold: too large a fraction of its
// inference-required, model-reached cases returned below the plausibility floor.
// Under LatencyReview the FactorBPS stays full (a review routing signal only);
// under LatencyEnforce the FactorBPS is zero.
const ResultLatencyImplausible Result = "latency_implausible"

// InferenceLatencyInput carries the trusted, per-case wall-time observations the
// v12 inference-latency gate consumes. It is produced by the validator side from
// runner.CaseExecution.TotalDurationMs (never miner self-report): the eligible
// population is the inference-required cases that VERIFIABLY reached the model
// (ModelInferenceObserved), and a case is flagged when its trusted wall time is
// below FloorMS. AttributionComplete is the single trust bit: it is true only
// when every eligible case carried a trusted wall time. The gate fails OPEN on
// incomplete attribution -- a review signal must never penalize an honest run
// for missing validator telemetry.
type InferenceLatencyInput struct {
	AdministeredCases   int
	EligibleCases       int
	FlaggedCases        int
	FloorMS             int64
	ShareThresholdBPS   int
	Posture             LatencyPosture
	TelemetryComplete   bool
	AttributionComplete bool
}

// InferenceLatencyEvidence is the signed, pure evidence for the v12
// inference-latency gate. It mirrors the model_use / model_dependence schema so
// the public projection and validator consume it uniformly. SubFloorBPS is the
// run-level distribution statistic: the fraction of the eligible population that
// returned below the floor. For bench_version<12 every field is the zero value
// and the gate is neither canonicalized nor multiplied into the composite.
type InferenceLatencyEvidence struct {
	AdministeredCases   int            `json:"administered_cases"`
	EligibleCases       int            `json:"eligible_cases"`
	FlaggedCases        int            `json:"flagged_cases"`
	UnflaggedCases      int            `json:"unflagged_cases"`
	FloorMS             int64          `json:"floor_ms"`
	AttributionComplete bool           `json:"attribution_complete"`
	Posture             LatencyPosture `json:"posture"`
	SubFloorBPS         int            `json:"sub_floor_bps"`
	ThresholdBPS        int            `json:"threshold_bps"`
	Result              Result         `json:"result"`
	FactorBPS           int            `json:"factor_bps"`
}

// administered reports whether the gate was actually run for this evidence. A
// zero-value InferenceLatencyEvidence (Result unset) means "not administered":
// it is omitted from the canonical bytes and never affects the combined factor,
// so a v12 Evidence built without the latency gate stays byte-identical to the
// dependence-only contract.
func (e InferenceLatencyEvidence) administered() bool { return e.Result != "" }

// buildInferenceLatency turns the trusted per-case wall-time aggregate into
// pure, version-bound evidence. It fails on invalid telemetry, fails OPEN
// (insufficient_evidence, full factor, no flag) on incomplete attribution, and
// otherwise flags when the sub-floor share crosses the threshold -- zeroing only
// under the enforce posture.
func buildInferenceLatency(in InferenceLatencyInput) (InferenceLatencyEvidence, error) {
	if in.Posture != LatencyReview && in.Posture != LatencyEnforce {
		return InferenceLatencyEvidence{}, invalid("inference-latency posture must be review or enforce")
	}
	if in.FloorMS < 1 || in.FloorMS > maxLatencyFloorMS {
		return InferenceLatencyEvidence{}, invalid("inference-latency floor_ms must be in [1,%d]", maxLatencyFloorMS)
	}
	if in.ShareThresholdBPS < 1 || in.ShareThresholdBPS > BasisPointScale {
		return InferenceLatencyEvidence{}, invalid("inference-latency share threshold must be in [1,%d]", BasisPointScale)
	}
	if !in.TelemetryComplete {
		return InferenceLatencyEvidence{}, fmt.Errorf("%w: inference-latency", ErrTelemetryUnavailable)
	}
	caseCounts := []struct {
		name  string
		count int
	}{
		{"latency.administered_cases", in.AdministeredCases},
		{"latency.eligible_cases", in.EligibleCases},
		{"latency.flagged_cases", in.FlaggedCases},
	}
	for _, item := range caseCounts {
		if err := validateBoundedCases(item.name, item.count); err != nil {
			return InferenceLatencyEvidence{}, err
		}
	}
	if in.EligibleCases > in.AdministeredCases {
		return InferenceLatencyEvidence{}, invalid("latency eligible_cases exceed administered_cases")
	}
	if in.FlaggedCases > in.EligibleCases {
		return InferenceLatencyEvidence{}, invalid("latency flagged_cases exceed eligible_cases")
	}
	e := InferenceLatencyEvidence{
		AdministeredCases: in.AdministeredCases, EligibleCases: in.EligibleCases,
		FlaggedCases: in.FlaggedCases, UnflaggedCases: in.EligibleCases - in.FlaggedCases,
		FloorMS: in.FloorMS, AttributionComplete: in.AttributionComplete,
		Posture: in.Posture, ThresholdBPS: in.ShareThresholdBPS,
	}
	if !in.AttributionComplete {
		// Fail OPEN: without a settled per-case wall time for every eligible case
		// the gate cannot fairly judge the distribution, and a review signal must
		// never penalize an honest run for missing validator telemetry.
		e.Result, e.FactorBPS, e.SubFloorBPS = ResultInsufficientEvidence, BasisPointScale, 0
		return e, nil
	}
	if in.EligibleCases == 0 {
		// No inference-required, model-reached case to time; the model_use gate
		// already governs the zero-inference outcome.
		e.Result, e.FactorBPS, e.SubFloorBPS = ResultNotApplicable, BasisPointScale, 0
		return e, nil
	}
	e.SubFloorBPS = coverageBPS(in.FlaggedCases, in.EligibleCases)
	switch {
	case e.SubFloorBPS < in.ShareThresholdBPS:
		e.Result, e.FactorBPS = ResultPassed, BasisPointScale
	case in.Posture == LatencyEnforce:
		e.Result, e.FactorBPS = ResultLatencyImplausible, 0
	default: // LatencyReview
		e.Result, e.FactorBPS = ResultLatencyImplausible, BasisPointScale
	}
	return e, nil
}

// AttachInferenceLatency binds the v12 inference-latency review gate onto an
// already-built v12 Evidence. Keeping it a post-Build attach leaves
// scoregates.Build and every v9..v11 caller source- and byte-identical: the base
// evidence is assembled first, and only bench_version>=12 layers this gate on
// top. The returned Evidence is fully re-validated (its digest now binds the
// latency evidence too).
func AttachInferenceLatency(e Evidence, in InferenceLatencyInput) (Evidence, error) {
	if e.BenchVersion < BenchVersionV12 {
		return Evidence{}, invalid("inference-latency gate is defined only for bench v%d and later", BenchVersionV12)
	}
	lat, err := buildInferenceLatency(in)
	if err != nil {
		return Evidence{}, err
	}
	e.InferenceLatency = lat
	if err := e.Validate(); err != nil {
		return Evidence{}, err
	}
	return e, nil
}
