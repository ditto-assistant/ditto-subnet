package scoregates

import (
	"strings"
	"testing"
)

// latencyInput is the trusted per-case wall-time aggregate for a v12 run: an
// eligible population of inference-required, model-reached cases, of which
// flagged returned below the floor.
func latencyInput(eligible, flagged int, posture LatencyPosture) InferenceLatencyInput {
	return InferenceLatencyInput{
		AdministeredCases: eligible + 2, EligibleCases: eligible, FlaggedCases: flagged,
		FloorMS: 400, ShareThresholdBPS: 5_000, Posture: posture,
		TelemetryComplete: true, AttributionComplete: true,
	}
}

func attachLatency(t *testing.T, e Evidence, in InferenceLatencyInput) Evidence {
	t.Helper()
	out, err := AttachInferenceLatency(e, in)
	if err != nil {
		t.Fatalf("AttachInferenceLatency() error = %v", err)
	}
	return out
}

// A flat, low-latency emulator (nearly every eligible case sub-floor) is flagged.
// Under the default review posture it is signed evidence and a routing signal
// but never zeroes the composite; under enforce it zeroes.
func TestInferenceLatencyEmulatorFlagged(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))

	review := attachLatency(t, base, latencyInput(10, 10, LatencyReview))
	if review.InferenceLatency.Result != ResultLatencyImplausible {
		t.Fatalf("review result = %s, want %s", review.InferenceLatency.Result, ResultLatencyImplausible)
	}
	if review.InferenceLatency.SubFloorBPS != BasisPointScale || review.InferenceLatency.FactorBPS != BasisPointScale {
		t.Fatalf("review sub_floor/factor = %d/%d, want %d/%d", review.InferenceLatency.SubFloorBPS, review.InferenceLatency.FactorBPS, BasisPointScale, BasisPointScale)
	}
	if factor, err := review.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("review CombinedFactorBPS() = %d, %v; want %d, nil (review never zeroes)", factor, err, BasisPointScale)
	}
	score := Score{Composite: 0.9, CompositeStderr: 0.01}
	if got, err := ApplyForVersion(BenchVersionV12, score, &review); err != nil || got != score {
		t.Fatalf("review ApplyForVersion() = %+v, %v; want %+v (review preserves composite)", got, err, score)
	}

	enforce := attachLatency(t, base, latencyInput(10, 10, LatencyEnforce))
	if enforce.InferenceLatency.Result != ResultLatencyImplausible || enforce.InferenceLatency.FactorBPS != 0 {
		t.Fatalf("enforce result/factor = %s/%d, want %s/0", enforce.InferenceLatency.Result, enforce.InferenceLatency.FactorBPS, ResultLatencyImplausible)
	}
	if factor, err := enforce.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("enforce CombinedFactorBPS() = %d, %v; want 0, nil (enforce zeroes)", factor, err)
	}
	if got, err := ApplyForVersion(BenchVersionV12, score, &enforce); err != nil || got != (Score{}) {
		t.Fatalf("enforce ApplyForVersion() = %+v, %v; want zero score", got, err)
	}
}

// A genuine agent whose latency varies with case complexity keeps its sub-floor
// share below the threshold, so the gate passes under either posture and the
// composite is preserved.
func TestInferenceLatencyHonestAgentPasses(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	for _, posture := range []LatencyPosture{LatencyReview, LatencyEnforce} {
		e := attachLatency(t, base, latencyInput(10, 0, posture))
		if e.InferenceLatency.Result != ResultPassed || e.InferenceLatency.FactorBPS != BasisPointScale {
			t.Fatalf("posture %s result/factor = %s/%d, want passed/%d", posture, e.InferenceLatency.Result, e.InferenceLatency.FactorBPS, BasisPointScale)
		}
		if e.InferenceLatency.SubFloorBPS != 0 {
			t.Fatalf("posture %s sub_floor = %d, want 0", posture, e.InferenceLatency.SubFloorBPS)
		}
		if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
			t.Fatalf("posture %s CombinedFactorBPS() = %d, %v; want %d, nil", posture, factor, err, BasisPointScale)
		}
	}
}

// A run with no inference-required, model-reached case (every fast case was
// tool-only/conversational and excluded upstream) has an empty eligible
// population: not_applicable, full factor, never flagged -- even under enforce.
func TestInferenceLatencyEmptyEligibleNotApplicable(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	in := latencyInput(0, 0, LatencyEnforce)
	in.AdministeredCases = 12
	e := attachLatency(t, base, in)
	if e.InferenceLatency.Result != ResultNotApplicable || e.InferenceLatency.FactorBPS != BasisPointScale {
		t.Fatalf("result/factor = %s/%d, want not_applicable/%d", e.InferenceLatency.Result, e.InferenceLatency.FactorBPS, BasisPointScale)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want %d, nil", factor, err, BasisPointScale)
	}
}

// Incomplete attribution (an eligible case lacked a trusted wall time) fails
// OPEN under BOTH postures: a review signal must never penalize an honest run
// for missing validator telemetry.
func TestInferenceLatencyIncompleteAttributionFailsOpen(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	for _, posture := range []LatencyPosture{LatencyReview, LatencyEnforce} {
		in := latencyInput(10, 10, posture)
		in.AttributionComplete = false
		e := attachLatency(t, base, in)
		if e.InferenceLatency.Result != ResultInsufficientEvidence || e.InferenceLatency.FactorBPS != BasisPointScale {
			t.Fatalf("posture %s result/factor = %s/%d, want insufficient_evidence/%d", posture, e.InferenceLatency.Result, e.InferenceLatency.FactorBPS, BasisPointScale)
		}
		if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
			t.Fatalf("posture %s CombinedFactorBPS() = %d, %v; want %d, nil (fail open)", posture, factor, err, BasisPointScale)
		}
	}
}

// A sub-floor share strictly below the threshold passes; exactly at the
// threshold flags. Proves the boundary is inclusive on the flag side.
func TestInferenceLatencyShareThresholdBoundary(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	// 4/10 = 4000 bps < 5000 threshold -> passes.
	if e := attachLatency(t, base, latencyInput(10, 4, LatencyReview)); e.InferenceLatency.Result != ResultPassed {
		t.Fatalf("4/10 result = %s, want passed", e.InferenceLatency.Result)
	}
	// 5/10 = 5000 bps == 5000 threshold -> flagged.
	if e := attachLatency(t, base, latencyInput(10, 5, LatencyReview)); e.InferenceLatency.Result != ResultLatencyImplausible {
		t.Fatalf("5/10 result = %s, want latency_implausible", e.InferenceLatency.Result)
	}
}

// The attached gate is bound into the v12 canonical bytes and digest, and a
// tampered latency evidence no longer validates.
func TestInferenceLatencyCanonicalAndTamper(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	e := attachLatency(t, base, latencyInput(10, 10, LatencyReview))

	body, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	for _, want := range []string{
		"inference_latency.administered_cases=12",
		"inference_latency.eligible_cases=10",
		"inference_latency.flagged_cases=10",
		"inference_latency.floor_ms=400",
		"inference_latency.posture=review",
		"inference_latency.sub_floor_bps=10000",
		"inference_latency.threshold_bps=5000",
		"inference_latency.result=latency_implausible",
		"inference_latency.factor_bps=10000",
	} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("canonical bytes missing %q:\n%s", want, body)
		}
	}

	withoutDigest, err := base.DigestHex()
	if err != nil {
		t.Fatalf("base DigestHex() error = %v", err)
	}
	withDigest, err := e.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex() error = %v", err)
	}
	if withDigest == withoutDigest {
		t.Fatal("attaching the inference-latency gate did not change the signed digest")
	}

	tampered := e
	tampered.InferenceLatency.FactorBPS = 0 // claim a zero factor the inputs do not derive
	if err := tampered.Validate(); err == nil {
		t.Fatal("Validate() accepted tampered inference-latency factor")
	}
}

// v9..v11 evidence never carries the inference-latency gate: it cannot be
// attached, its canonical bytes omit it, and the combined factor is unaffected.
func TestInferenceLatencyPreV12Rejected(t *testing.T) {
	for _, version := range []int{BenchVersionV9, BenchVersionV10, BenchVersionV11} {
		e, err := Build(version, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce)
		if err != nil {
			t.Fatalf("Build(v%d) error = %v", version, err)
		}
		if _, err := AttachInferenceLatency(e, latencyInput(10, 10, LatencyReview)); err == nil {
			t.Fatalf("v%d accepted an inference-latency gate", version)
		}
		body, err := e.CanonicalBytes()
		if err != nil {
			t.Fatalf("CanonicalBytes(v%d) error = %v", version, err)
		}
		if strings.Contains(string(body), "inference_latency") {
			t.Fatalf("v%d canonical bytes leaked inference_latency:\n%s", version, body)
		}
	}
}
