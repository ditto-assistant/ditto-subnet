package scoregates

// The v12 answer-stuffing gate is the load-bearing defense against Class-D
// post-model coercion (adversary review D). A Class-D harness deterministically
// COMPUTES a case's answer in engine code (real examples: lets_5.0
// pipeline/flow.py:452 injects a `requestedValue` field; Crown src/reply.rs:182
// "Copy its bare value exactly...") and then feeds the finished answer into the
// model's INPUT with a copy-exactly directive so the model parrots it. Because
// the answer routes THROUGH the model, the C3 causal-ablation gate
// (model_dependence) MISSES it: under full ablation the model returns nothing,
// the parrot has nothing to copy, correctness collapses, and the counterfactual
// verdict reads DEPENDENT ("genuine"). The model did not derive the answer; the
// harness did, and handed it over.
//
// Detection is value-provenance, NOT directive keywords (keyword matching is
// brittle and would trip on legitimate RAG prose). A COMPUTED-answer case is
// flagged IFF, in the trusted clean-pass model I/O the relay recorded, the
// finished answer value appears in a model INPUT the harness sent BEFORE it
// appears in any model COMPLETION. The scorer -- which alone holds the dataset
// answer -- runs this check post-run over the broker's bounded/normalized I/O;
// the broker never sees the answer key.
//
// What it cannot false-trigger on:
//
//   - Normal RAG / verbatim recall: only COMPUTED cases are eligible (the answer
//     is not verbatim present in the case's seeded evidence). A retrieved memory
//     that literally contains the recall answer is verbatim-recall, excluded from
//     the slice before any I/O is inspected. This is the primary RAG guard.
//   - Legit model-derivation: the answer value is produced by the model, so it
//     only ever appears in a COMPLETION -- never in an input -> not stuffed.
//   - Legit multi-turn compute: the model computes the value in an earlier
//     completion and the agent reuses it in a later input; the value's earliest
//     occurrence is a completion, before any input -> not stuffed.
//
// Relationship to the other model gates:
//
//   - model_use proves a case reached the relay; model_dependence proves the
//     scored answer is causally downstream of the completion; inference_latency
//     flags a run that never really ran the model. answer_stuffing is orthogonal:
//     it catches a harness that DID route through the model but fed it the
//     pre-computed answer to launder.
//
// Posture: PENALIZE by default. A finished COMPUTED answer in the harness
// model-input is a lead, not a proof of Class-D coercion; enforce false-zeroed
// an honest champion. The default is a graduated, capped, never-zero penalty,
// flippable to review or enforce via env.
//
// Gating bar: STRICT + PROVABLE per case, with a LOW count threshold. Each flagged
// case is now provable on its own -- the scorer flags a computed case ONLY when
// its answer value appears NOWHERE in the entire run's retrievable seeded memory
// (so no legitimate retrieval could ever surface it) AND that value shows up in a
// model INPUT before any COMPLETION (so the model did not derive it either). A
// value that is nowhere in memory yet fed into the model input cannot be a numeric
// coincidence with an operand: an operand-equals-answer value is present in memory
// as that operand and is therefore excluded before it can be flagged. Because a
// single flagged case is already proof of injection, the run zeroes when the COUNT
// of provable stuffed cases crosses a low threshold (default 1) rather than a
// majority share -- the old majority rule let a minority stuffer (real exploit
// lets_5.0 stuffs ~1 of 5 computed cases) slip through. The stuffed share is still
// published for observability.

import "fmt"

type AnswerStuffingPosture string

const (
	// AnswerStuffingEnforce zeroes the composite when the provable stuffed COUNT
	// reaches MinCases. Default: a computed answer in the harness's model-input is a
	// deliberate injection, so the honest posture is a hard gate.
	AnswerStuffingEnforce AnswerStuffingPosture = "enforce"
	// AnswerStuffingReview emits the signed evidence and routes a flagged run to
	// human review WITHOUT zeroing the composite (FactorBPS stays full). Reserved
	// for measuring the honest-cohort stuffed-share distribution before enforce.
	AnswerStuffingReview AnswerStuffingPosture = "review"
	// AnswerStuffingPenalize applies a GRADUATED, CAPPED penalty proportional to the
	// provable stuffed SHARE, and NEVER zeroes. The "computed answer in the model
	// input" signal is ambiguous -- a genuine agent that computes a candidate figure
	// and lets the model reason over it produces the same signal as a launderer that
	// injects the answer for the model to copy -- so a single flagged case must not
	// zero an honest run. FactorBPS = BasisPointScale - min(AnswerStuffingMaxPenaltyBPS,
	// stuffedShareBPS): one flagged case out of many computed cases costs a little,
	// systematic stuffing costs up to the cap, and the composite is never destroyed.
	// This is the default posture.
	AnswerStuffingPenalize AnswerStuffingPosture = "penalize"
)

// AnswerStuffingMaxPenaltyBPS caps the penalize-posture factor reduction so the
// gate can never take more than this fraction of the composite (never zero). It
// is a fixed contract constant, not an operator knob (posture is the knob).
const AnswerStuffingMaxPenaltyBPS = 5_000

// ResultAnswerStuffed marks a run whose COMPUTED-answer cases were fed the
// finished answer through the model input above the run-level stuffed-share
// threshold. Under AnswerStuffingEnforce the FactorBPS is zero; under
// AnswerStuffingReview the FactorBPS stays full (a review routing signal only).
const ResultAnswerStuffed Result = "answer_stuffed"

// ResultReviewRequired marks a run whose provenance capture COULD NOT be fully
// settled for at least one COMPUTED case because its model I/O overflowed the
// generous capture bound (a pathological, oversized prompt). The gate can neither
// clear the case (pass) nor prove it stuffed, so it routes the run to human
// review with the FactorBPS left FULL -- it never auto-zeroes (which would risk an
// honest giant-RAG agent) and never silently passes (the current fail-open bug on
// truncation). It takes precedence over an enforce-zero: once capture is
// incomplete the automatic verdict is not trusted to zero, only to flag.
const ResultReviewRequired Result = "review_required"

// answerStuffingReviewMinCases is the floor on the loose systematic-review
// signal: a run routes to review only when at least this many COMPUTED cases fed
// the finished answer into a model input (and the loose share crosses the
// threshold). It ensures an ISOLATED single-case coincidence -- one computed
// answer that happens to also appear in an input -- never trips review; only a
// systematic pattern (which an honest RAG/legit-compute agent does not produce)
// does. The provable auto-gate (MinCases, default 1) is unaffected by this floor.
const answerStuffingReviewMinCases = 2

// AnswerStuffingInput carries the trusted per-case answer-provenance aggregate
// the v12 answer-stuffing gate consumes. It is produced by the scorer path that,
// for every COMPUTED-answer, model-reached, non-excluded scored case, checks the
// broker's recorded clean-pass model I/O for the value-in-input-before-completion
// signature (see cmd/dittobench-api/v9_base.go v12AnswerStuffingTelemetry --the
// single integration point). EligibleCases is the number of computed cases whose
// provenance settled; StuffedCases is how many of those were flagged.
// AttributionComplete is the single trust bit: it is true only when every
// eligible case carried a settled provenance verdict. The gate fails OPEN on
// incomplete attribution -- a detection gate must never penalize an honest run
// for missing capture.
type AnswerStuffingInput struct {
	AdministeredCases int
	EligibleCases     int
	StuffedCases      int
	// MinCases is the LOW count threshold that governs the gate: the run flags when
	// the number of PROVABLE stuffed cases is >= MinCases. Because each stuffed case
	// is provable on its own (its answer is nowhere in seeded memory yet appears in a
	// model input before any completion), the default is 1. Must be >= 1.
	MinCases int
	// ShareThresholdBPS is retained only as the published stuffed-share statistic
	// (StuffedBPS / ThresholdBPS in the evidence); it no longer governs the gate.
	ShareThresholdBPS int
	// LooseEligibleCases / LooseStuffedCases carry the "loose" systematic-review
	// signal: over the COMPUTED population (answers that are not verbatim-recall),
	// REGARDLESS of whether the answer value also appears elsewhere in seeded memory,
	// how many cases fed the finished answer into a model INPUT before any
	// COMPLETION. This is a SUPERSET of the provable StuffedCases slice: a launderer
	// that injects an answer whose value ALSO appears in memory (a coinciding-value
	// stuffer) cannot be auto-proven against RAG, so it is excluded from StuffedCases
	// but counted here. When the loose share crosses ReviewShareThresholdBPS the run
	// routes to REVIEW (never an auto-zero). LooseStuffedCases <= LooseEligibleCases.
	LooseEligibleCases int
	LooseStuffedCases  int
	// ReviewShareThresholdBPS is the loose-share bar that routes a run to review: a
	// systematic pattern of feeding computed answers into the model input. Zero
	// disables the loose-review signal (an operator opt-out and the pre-Fix-B
	// default for callers that do not populate it); otherwise it must be in
	// [1,BasisPointScale]. An isolated single-case coincidence never trips review
	// regardless of the threshold (see answerStuffingReviewMinCases).
	ReviewShareThresholdBPS int
	Posture                 AnswerStuffingPosture
	TelemetryComplete       bool
	AttributionComplete     bool
	// ReviewRequired is set when at least one COMPUTED, model-reached case could
	// not have its provenance settled because its captured model I/O overflowed the
	// generous per-side bound (an oversized/pathological prompt). It routes the run
	// to ResultReviewRequired (full factor, never a pass, never an auto-zero). It is
	// distinct from AttributionComplete=false: a truncated computed case IS settled
	// as "needs review" rather than left pending, so a run whose only unsettled
	// cases are truncations still has AttributionComplete=true and is surfaced
	// rather than hidden behind fail-open.
	ReviewRequired bool
}

// AnswerStuffingEvidence is the signed, pure evidence for the v12 answer-stuffing
// gate. It mirrors the model_use / model_dependence / inference_latency schema so
// the public projection and validator consume it uniformly. StuffedBPS is the
// run-level distribution statistic: the fraction of the eligible (computed,
// model-reached) population that was stuffed. For bench_version<12 every field is
// the zero value and the gate is neither canonicalized nor multiplied into the
// composite.
type AnswerStuffingEvidence struct {
	AdministeredCases   int  `json:"administered_cases"`
	EligibleCases       int  `json:"eligible_cases"`
	StuffedCases        int  `json:"stuffed_cases"`
	CleanCases          int  `json:"clean_cases"`
	AttributionComplete bool `json:"attribution_complete"`
	// ReviewRequired echoes the trust bit that at least one computed case's I/O
	// overflowed the capture bound, so the run was routed to ResultReviewRequired
	// instead of a pass. It is part of the signed canonical bytes for
	// bench_version>=12.
	ReviewRequired bool                  `json:"review_required"`
	Posture        AnswerStuffingPosture `json:"posture"`
	StuffedBPS     int                   `json:"stuffed_bps"`
	ThresholdBPS   int                   `json:"threshold_bps"`
	// MinCases is the low count threshold the gate flags on: StuffedCases >= MinCases
	// zeroes the composite under enforce. Bound into the signed canonical bytes for
	// bench_version>=12.
	MinCases int `json:"min_cases"`
	// LooseEligibleCases / LooseStuffedCases / LooseStuffedBPS publish the loose
	// systematic-review signal (see AnswerStuffingInput): the count and share of
	// COMPUTED cases whose answer value was fed into a model input before any
	// completion, regardless of memory presence. ReviewShareThresholdBPS echoes the
	// bar that routes the run to review. All are bound into the signed canonical
	// bytes for bench_version>=12 for observability and reproducibility.
	LooseEligibleCases      int    `json:"loose_eligible_cases"`
	LooseStuffedCases       int    `json:"loose_stuffed_cases"`
	LooseStuffedBPS         int    `json:"loose_stuffed_bps"`
	ReviewShareThresholdBPS int    `json:"review_share_threshold_bps"`
	Result                  Result `json:"result"`
	FactorBPS               int    `json:"factor_bps"`
}

// administered reports whether the gate was actually run for this evidence. A
// zero-value AnswerStuffingEvidence (Result unset) means "not administered": it
// is omitted from the canonical bytes and never affects the combined factor, so a
// v12 Evidence built without the answer-stuffing gate stays byte-identical to the
// contract without it.
func (e AnswerStuffingEvidence) administered() bool { return e.Result != "" }

// buildAnswerStuffing turns the trusted per-case answer-provenance aggregate into
// pure, version-bound evidence. It fails on invalid telemetry, fails OPEN
// (insufficient_evidence, full factor, no flag) on incomplete attribution, routes
// to review (ResultReviewRequired, full factor) when a computed case's capture
// overflowed the bound, and otherwise flags when the stuffed share crosses the
// threshold -- zeroing only under the enforce posture.
//
// Outcome precedence, most conservative first:
//  1. incomplete attribution (a computed case's I/O was never captured) -> fail
//     OPEN (insufficient_evidence), never penalize an honest run for a capture gap;
//  2. review required (a computed case's I/O overflowed the generous bound) ->
//     ResultReviewRequired, full factor -- surface for an operator rather than
//     auto-zero (which would risk an honest giant-RAG agent) or silently pass
//     (the prior fail-open bug). This precedes the enforce-zero: with capture
//     incomplete the automatic verdict is trusted to FLAG, not to zero;
//  3. otherwise the provable stuffed COUNT governs: below MinCases passes; at or
//     above it flags, zeroing only under the enforce posture.
func buildAnswerStuffing(in AnswerStuffingInput) (AnswerStuffingEvidence, error) {
	if in.Posture != AnswerStuffingEnforce && in.Posture != AnswerStuffingReview &&
		in.Posture != AnswerStuffingPenalize {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing posture must be enforce, penalize, or review")
	}
	if in.ShareThresholdBPS < 1 || in.ShareThresholdBPS > BasisPointScale {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing share threshold must be in [1,%d]", BasisPointScale)
	}
	if in.MinCases < 1 {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing min_cases must be >= 1")
	}
	if in.ReviewShareThresholdBPS < 0 || in.ReviewShareThresholdBPS > BasisPointScale {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing review_share_threshold_bps must be in [0,%d]", BasisPointScale)
	}
	if !in.TelemetryComplete {
		return AnswerStuffingEvidence{}, fmt.Errorf("%w: answer-stuffing", ErrTelemetryUnavailable)
	}
	caseCounts := []struct {
		name  string
		count int
	}{
		{"answer_stuffing.administered_cases", in.AdministeredCases},
		{"answer_stuffing.eligible_cases", in.EligibleCases},
		{"answer_stuffing.stuffed_cases", in.StuffedCases},
		{"answer_stuffing.loose_eligible_cases", in.LooseEligibleCases},
		{"answer_stuffing.loose_stuffed_cases", in.LooseStuffedCases},
	}
	for _, item := range caseCounts {
		if err := validateBoundedCases(item.name, item.count); err != nil {
			return AnswerStuffingEvidence{}, err
		}
	}
	if in.EligibleCases > in.AdministeredCases {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing eligible_cases exceed administered_cases")
	}
	if in.StuffedCases > in.EligibleCases {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing stuffed_cases exceed eligible_cases")
	}
	if in.LooseEligibleCases > in.AdministeredCases {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing loose_eligible_cases exceed administered_cases")
	}
	if in.LooseStuffedCases > in.LooseEligibleCases {
		return AnswerStuffingEvidence{}, invalid("answer-stuffing loose_stuffed_cases exceed loose_eligible_cases")
	}
	e := AnswerStuffingEvidence{
		AdministeredCases: in.AdministeredCases, EligibleCases: in.EligibleCases,
		StuffedCases: in.StuffedCases, CleanCases: in.EligibleCases - in.StuffedCases,
		AttributionComplete: in.AttributionComplete, ReviewRequired: in.ReviewRequired,
		Posture: in.Posture, ThresholdBPS: in.ShareThresholdBPS, MinCases: in.MinCases,
		LooseEligibleCases: in.LooseEligibleCases, LooseStuffedCases: in.LooseStuffedCases,
		ReviewShareThresholdBPS: in.ReviewShareThresholdBPS,
	}
	if !in.AttributionComplete {
		// Fail OPEN: without a settled per-case provenance verdict for every eligible
		// case the gate cannot fairly judge the distribution, and a detection gate
		// must never penalize an honest run for missing validator capture.
		e.Result, e.FactorBPS, e.StuffedBPS = ResultInsufficientEvidence, BasisPointScale, 0
		return e, nil
	}
	// Publish the partial stuffed distributions (provable slice and loose slice) for
	// observability, even when the run ultimately routes to review or passes below.
	if in.EligibleCases > 0 {
		e.StuffedBPS = coverageBPS(in.StuffedCases, in.EligibleCases)
	}
	if in.LooseEligibleCases > 0 {
		e.LooseStuffedBPS = coverageBPS(in.LooseStuffedCases, in.LooseEligibleCases)
	}
	// The loose systematic-review signal: a SYSTEMATIC share of computed answers fed
	// into the model input, which an honest RAG/legit-compute agent does not produce.
	// It only ROUTES TO REVIEW (never auto-zeroes), needs a systematic count (an
	// isolated single-case coincidence never trips it), and is disabled when the
	// threshold is zero.
	looseReview := in.ReviewShareThresholdBPS > 0 &&
		in.LooseStuffedCases >= answerStuffingReviewMinCases &&
		e.LooseStuffedBPS >= in.ReviewShareThresholdBPS
	if in.ReviewRequired {
		// A computed case's capture overflowed the generous bound. Capture is
		// incomplete for that case, so the automatic verdict is trusted only to
		// FLAG, not to zero: surface the run for an operator with a full factor.
		// Never auto-zero (an honest giant-RAG agent could legitimately overflow) and
		// never silently pass (the prior fail-open bug on truncation).
		e.Result, e.FactorBPS = ResultReviewRequired, BasisPointScale
		return e, nil
	}
	// Provable stuffed count at or above MinCases. enforce zeroes; penalize applies a
	// graduated capped factor (never zero); review flags at full factor.
	if in.EligibleCases > 0 && e.StuffedCases >= in.MinCases {
		e.Result = ResultAnswerStuffed
		switch in.Posture {
		case AnswerStuffingEnforce:
			e.FactorBPS = 0
		case AnswerStuffingPenalize:
			// Graduated, capped penalty proportional to the provable stuffed share --
			// one flagged case out of many computed cases costs a little, systematic
			// stuffing costs up to the cap, the composite is never destroyed.
			penalty := e.StuffedBPS
			if penalty > AnswerStuffingMaxPenaltyBPS {
				penalty = AnswerStuffingMaxPenaltyBPS
			}
			e.FactorBPS = BasisPointScale - penalty
		default: // AnswerStuffingReview
			e.FactorBPS = BasisPointScale
		}
		return e, nil
	}
	// No provable auto-gate. A systematic loose (coinciding-value) pattern that could
	// not be auto-proven against RAG routes the run to human REVIEW with a FULL
	// factor -- surfaced, never auto-zeroed.
	if looseReview {
		e.Result, e.FactorBPS = ResultReviewRequired, BasisPointScale
		return e, nil
	}
	if in.EligibleCases == 0 {
		// No computed, model-reached case to inspect and no systematic loose pattern;
		// a verbatim-recall-only or model-independent run is governed by other gates.
		e.Result, e.FactorBPS = ResultNotApplicable, BasisPointScale
		return e, nil
	}
	// Below the low provable count threshold and no systematic loose pattern: pass.
	e.Result, e.FactorBPS = ResultPassed, BasisPointScale
	return e, nil
}

// AttachAnswerStuffing binds the v12 answer-stuffing gate onto an already-built
// v12 Evidence. Keeping it a post-Build attach leaves scoregates.Build and every
// v9..v11 caller source- and byte-identical: the base evidence is assembled
// first, and only bench_version>=12 layers this gate on top. The returned
// Evidence is fully re-validated (its digest now binds the answer-stuffing
// evidence too).
func AttachAnswerStuffing(e Evidence, in AnswerStuffingInput) (Evidence, error) {
	if e.BenchVersion < BenchVersionV12 {
		return Evidence{}, invalid("answer-stuffing gate is defined only for bench v%d and later", BenchVersionV12)
	}
	stuffing, err := buildAnswerStuffing(in)
	if err != nil {
		return Evidence{}, err
	}
	e.AnswerStuffing = stuffing
	if err := e.Validate(); err != nil {
		return Evidence{}, err
	}
	return e, nil
}
