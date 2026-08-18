package scoregates

import (
	"strings"
	"testing"
)

// stuffingInput is the trusted per-case answer-provenance aggregate for a v12
// run: an eligible population of computed, model-reached cases, of which stuffed
// had the finished answer fed into a model input before any completion.
func stuffingInput(eligible, stuffed int, posture AnswerStuffingPosture) AnswerStuffingInput {
	return AnswerStuffingInput{
		AdministeredCases: eligible + 2, EligibleCases: eligible, StuffedCases: stuffed,
		MinCases: 1, ShareThresholdBPS: 5_000, Posture: posture,
		TelemetryComplete: true, AttributionComplete: true,
	}
}

func attachStuffing(t *testing.T, e Evidence, in AnswerStuffingInput) Evidence {
	t.Helper()
	out, err := AttachAnswerStuffing(e, in)
	if err != nil {
		t.Fatalf("AttachAnswerStuffing() error = %v", err)
	}
	return out
}

// A Class-D answer-stuffer (its whole computed-answer population fed the finished
// answer through the model input) is flagged. Under the default enforce posture
// it zeroes the composite; under review it is signed evidence and a routing
// signal but never zeroes.
func TestAnswerStuffingStufferFlagged(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))

	enforce := attachStuffing(t, base, stuffingInput(10, 10, AnswerStuffingEnforce))
	if enforce.AnswerStuffing.Result != ResultAnswerStuffed || enforce.AnswerStuffing.FactorBPS != 0 {
		t.Fatalf("enforce result/factor = %s/%d, want %s/0", enforce.AnswerStuffing.Result, enforce.AnswerStuffing.FactorBPS, ResultAnswerStuffed)
	}
	if enforce.AnswerStuffing.StuffedBPS != BasisPointScale || enforce.AnswerStuffing.CleanCases != 0 {
		t.Fatalf("enforce stuffed_bps/clean = %d/%d", enforce.AnswerStuffing.StuffedBPS, enforce.AnswerStuffing.CleanCases)
	}
	if factor, err := enforce.CombinedFactorBPS(); err != nil || factor != 0 {
		t.Fatalf("enforce CombinedFactorBPS() = %d, %v; want 0, nil (enforce zeroes)", factor, err)
	}
	score := Score{Composite: 0.9, CompositeStderr: 0.01}
	if got, err := ApplyForVersion(BenchVersionV12, score, &enforce); err != nil || got != (Score{}) {
		t.Fatalf("enforce ApplyForVersion() = %+v, %v; want zero score", got, err)
	}

	review := attachStuffing(t, base, stuffingInput(10, 10, AnswerStuffingReview))
	if review.AnswerStuffing.Result != ResultAnswerStuffed || review.AnswerStuffing.FactorBPS != BasisPointScale {
		t.Fatalf("review result/factor = %s/%d, want %s/%d", review.AnswerStuffing.Result, review.AnswerStuffing.FactorBPS, ResultAnswerStuffed, BasisPointScale)
	}
	if factor, err := review.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("review CombinedFactorBPS() = %d, %v; want %d, nil (review never zeroes)", factor, err, BasisPointScale)
	}
	if got, err := ApplyForVersion(BenchVersionV12, score, &review); err != nil || got != score {
		t.Fatalf("review ApplyForVersion() = %+v, %v; want %+v (review preserves composite)", got, err, score)
	}
}

// A genuine agent that never fed a computed answer through the model input keeps
// its stuffed share at zero, so the gate passes under either posture and the
// composite is preserved.
func TestAnswerStuffingHonestAgentPasses(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	for _, posture := range []AnswerStuffingPosture{AnswerStuffingEnforce, AnswerStuffingReview} {
		e := attachStuffing(t, base, stuffingInput(10, 0, posture))
		if e.AnswerStuffing.Result != ResultPassed || e.AnswerStuffing.FactorBPS != BasisPointScale {
			t.Fatalf("posture %s result/factor = %s/%d, want passed/%d", posture, e.AnswerStuffing.Result, e.AnswerStuffing.FactorBPS, BasisPointScale)
		}
		if e.AnswerStuffing.StuffedBPS != 0 {
			t.Fatalf("posture %s stuffed_bps = %d, want 0", posture, e.AnswerStuffing.StuffedBPS)
		}
		if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
			t.Fatalf("posture %s CombinedFactorBPS() = %d, %v; want %d, nil", posture, factor, err, BasisPointScale)
		}
	}
}

// A run with no computed, model-reached case (every case was verbatim-recall or
// tool-only and excluded upstream) has an empty eligible population:
// not_applicable, full factor, never flagged -- even under enforce.
func TestAnswerStuffingEmptyEligibleNotApplicable(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	in := stuffingInput(0, 0, AnswerStuffingEnforce)
	in.AdministeredCases = 12
	e := attachStuffing(t, base, in)
	if e.AnswerStuffing.Result != ResultNotApplicable || e.AnswerStuffing.FactorBPS != BasisPointScale {
		t.Fatalf("result/factor = %s/%d, want not_applicable/%d", e.AnswerStuffing.Result, e.AnswerStuffing.FactorBPS, BasisPointScale)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("CombinedFactorBPS() = %d, %v; want %d, nil", factor, err, BasisPointScale)
	}
}

// Incomplete attribution (an eligible case's I/O was not captured) fails OPEN
// under BOTH postures: a detection gate must never penalize an honest run for
// missing validator capture.
func TestAnswerStuffingIncompleteAttributionFailsOpen(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	for _, posture := range []AnswerStuffingPosture{AnswerStuffingEnforce, AnswerStuffingReview} {
		in := stuffingInput(10, 10, posture)
		in.AttributionComplete = false
		e := attachStuffing(t, base, in)
		if e.AnswerStuffing.Result != ResultInsufficientEvidence || e.AnswerStuffing.FactorBPS != BasisPointScale {
			t.Fatalf("posture %s result/factor = %s/%d, want insufficient_evidence/%d", posture, e.AnswerStuffing.Result, e.AnswerStuffing.FactorBPS, BasisPointScale)
		}
		if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
			t.Fatalf("posture %s CombinedFactorBPS() = %d, %v; want %d, nil (fail open)", posture, factor, err, BasisPointScale)
		}
	}
}

// The gate is now driven by the PROVABLE stuffed COUNT against a low MinCases
// threshold, not the share. A count strictly below MinCases passes; at or above it
// flags. With the default MinCases=1 a single provable stuffed case among many
// clean ones -- a minority stuffer like lets_5.0 -- gates the run, which the old
// majority-share rule missed. Each flagged case is provable on its own (its answer
// is nowhere in seeded memory yet appears in a model input before any completion),
// so no share threshold is needed to avoid coincidence false-positives.
func TestAnswerStuffingMinCasesBoundary(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	// 0 provable stuffed cases < MinCases 1 -> passes.
	if e := attachStuffing(t, base, stuffingInput(10, 0, AnswerStuffingEnforce)); e.AnswerStuffing.Result != ResultPassed {
		t.Fatalf("0/10 result = %s, want passed", e.AnswerStuffing.Result)
	}
	// 1/10 (a MINORITY, 1000 bps, far below the old 5000 majority) == MinCases 1 -> flagged.
	if e := attachStuffing(t, base, stuffingInput(10, 1, AnswerStuffingEnforce)); e.AnswerStuffing.Result != ResultAnswerStuffed {
		t.Fatalf("1/10 result = %s, want answer_stuffed", e.AnswerStuffing.Result)
	}

	// An operator may raise the bar: with MinCases=3, a 2-case minority passes and 3 flags.
	high := func(stuffed int) AnswerStuffingInput {
		in := stuffingInput(10, stuffed, AnswerStuffingEnforce)
		in.MinCases = 3
		return in
	}
	if e := attachStuffing(t, base, high(2)); e.AnswerStuffing.Result != ResultPassed {
		t.Fatalf("2/10 at min_cases=3 result = %s, want passed", e.AnswerStuffing.Result)
	}
	if e := attachStuffing(t, base, high(3)); e.AnswerStuffing.Result != ResultAnswerStuffed {
		t.Fatalf("3/10 at min_cases=3 result = %s, want answer_stuffed", e.AnswerStuffing.Result)
	}

	// MinCases < 1 is invalid.
	bad := stuffingInput(10, 1, AnswerStuffingEnforce)
	bad.MinCases = 0
	if _, err := AttachAnswerStuffing(base, bad); err == nil {
		t.Fatal("AttachAnswerStuffing accepted min_cases=0")
	}
}

// The attached gate is bound into the v12 canonical bytes and digest, and a
// tampered answer-stuffing evidence no longer validates.
func TestAnswerStuffingCanonicalAndTamper(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))
	e := attachStuffing(t, base, stuffingInput(10, 10, AnswerStuffingEnforce))

	body, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	for _, want := range []string{
		"answer_stuffing.administered_cases=12",
		"answer_stuffing.eligible_cases=10",
		"answer_stuffing.stuffed_cases=10",
		"answer_stuffing.clean_cases=0",
		"answer_stuffing.attribution_complete=true",
		"answer_stuffing.posture=enforce",
		"answer_stuffing.stuffed_bps=10000",
		"answer_stuffing.threshold_bps=5000",
		"answer_stuffing.min_cases=1",
		"answer_stuffing.result=answer_stuffed",
		"answer_stuffing.factor_bps=0",
	} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("canonical bytes missing %q:\n%s", want, body)
		}
	}

	withoutStuffing, err := base.DigestHex()
	if err != nil {
		t.Fatalf("base DigestHex() error = %v", err)
	}
	withStuffing, err := e.DigestHex()
	if err != nil {
		t.Fatalf("DigestHex() error = %v", err)
	}
	if withStuffing == withoutStuffing {
		t.Fatal("attaching the answer-stuffing gate did not change the signed digest")
	}

	tampered := e
	tampered.AnswerStuffing.FactorBPS = BasisPointScale // claim a full factor the flagged inputs do not derive
	if err := tampered.Validate(); err == nil {
		t.Fatal("Validate() accepted tampered answer-stuffing factor")
	}
}

// A run with a residual-truncation review flag routes to ResultReviewRequired with
// a FULL factor: never a silent pass, never an auto-zero. Review takes precedence
// over an enforce-zero -- once a computed case's capture is incomplete the
// automatic verdict is trusted only to FLAG for an operator, not to zero.
func TestAnswerStuffingReviewRequiredRoutesReview(t *testing.T) {
	base := buildV12(t, RolloutEnforce, dependenceInput(10, 10))

	// Below threshold but review required: routes to review, full factor.
	clean := stuffingInput(10, 0, AnswerStuffingEnforce)
	clean.ReviewRequired = true
	e := attachStuffing(t, base, clean)
	if e.AnswerStuffing.Result != ResultReviewRequired || e.AnswerStuffing.FactorBPS != BasisPointScale {
		t.Fatalf("review result/factor = %s/%d, want %s/%d", e.AnswerStuffing.Result, e.AnswerStuffing.FactorBPS, ResultReviewRequired, BasisPointScale)
	}
	if !e.AnswerStuffing.ReviewRequired {
		t.Fatalf("ReviewRequired not echoed: %+v", e.AnswerStuffing)
	}
	if factor, err := e.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("review CombinedFactorBPS() = %d, %v; want %d (review never zeroes)", factor, err, BasisPointScale)
	}

	// Even a majority-stuffed eligible subset does NOT auto-zero while review is
	// pending: capture is incomplete, so flag rather than zero.
	stuffedToo := stuffingInput(10, 10, AnswerStuffingEnforce)
	stuffedToo.ReviewRequired = true
	z := attachStuffing(t, base, stuffedToo)
	if z.AnswerStuffing.Result != ResultReviewRequired || z.AnswerStuffing.FactorBPS != BasisPointScale {
		t.Fatalf("review-over-enforce result/factor = %s/%d, want %s/%d", z.AnswerStuffing.Result, z.AnswerStuffing.FactorBPS, ResultReviewRequired, BasisPointScale)
	}
	if factor, err := z.CombinedFactorBPS(); err != nil || factor != BasisPointScale {
		t.Fatalf("review-over-enforce CombinedFactorBPS() = %d, %v; want %d (never auto-zero on truncation)", factor, err, BasisPointScale)
	}

	// The review bit is bound into the signed canonical bytes.
	body, err := e.CanonicalBytes()
	if err != nil {
		t.Fatalf("CanonicalBytes() error = %v", err)
	}
	for _, want := range []string{
		"answer_stuffing.review_required=true",
		"answer_stuffing.result=review_required",
		"answer_stuffing.factor_bps=10000",
	} {
		if !strings.Contains(string(body), want) {
			t.Fatalf("canonical bytes missing %q:\n%s", want, body)
		}
	}

	// Incomplete attribution (a genuine capture gap) still takes precedence and
	// fails OPEN as insufficient_evidence, not review.
	gap := stuffingInput(10, 0, AnswerStuffingEnforce)
	gap.ReviewRequired = true
	gap.AttributionComplete = false
	g := attachStuffing(t, base, gap)
	if g.AnswerStuffing.Result != ResultInsufficientEvidence || g.AnswerStuffing.FactorBPS != BasisPointScale {
		t.Fatalf("attribution-gap precedence result/factor = %s/%d, want insufficient_evidence/%d", g.AnswerStuffing.Result, g.AnswerStuffing.FactorBPS, BasisPointScale)
	}
}

// v9..v11 evidence never carries the answer-stuffing gate: it cannot be attached,
// its canonical bytes omit it, and the combined factor is unaffected.
func TestAnswerStuffingPreV12Rejected(t *testing.T) {
	for _, version := range []int{BenchVersionV9, BenchVersionV10, BenchVersionV11} {
		e, err := Build(version, validModelInput(), validToolInput(), validThresholds(), RolloutEnforce)
		if err != nil {
			t.Fatalf("Build(v%d) error = %v", version, err)
		}
		if _, err := AttachAnswerStuffing(e, stuffingInput(10, 10, AnswerStuffingEnforce)); err == nil {
			t.Fatalf("v%d accepted an answer-stuffing gate", version)
		}
		body, err := e.CanonicalBytes()
		if err != nil {
			t.Fatalf("CanonicalBytes(v%d) error = %v", version, err)
		}
		if strings.Contains(string(body), "answer_stuffing") {
			t.Fatalf("v%d canonical bytes leaked answer_stuffing:\n%s", version, body)
		}
	}
}
