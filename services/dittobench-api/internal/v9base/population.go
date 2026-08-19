package v9base

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const modelRoutePreflightCaseID = "__dittobench_model_route_preflight__"

const (
	// LatencyFloorMS is the default per-case plausibility floor for the v12
	// inference-latency gate: a case that verifiably reached the model yet
	// returned below this wall time is implausible for a real gpt-oss-20b
	// completion. It is conservative -- well under the honest board's 900-5000ms
	// spread and well above an in-memory parser's ~119ms -- so a fast-but-honest
	// agent is not flagged. Operator-overridable at the integration point.
	LatencyFloorMS = 400
	// LatencySubFloorShareBPS is the default share of the eligible (inference-
	// required, model-reached) population that must return sub-floor before the
	// run is flagged: a majority. A genuine agent's latency varies with case
	// complexity so its sub-floor share is near zero; an emulator's is flat and
	// low, so nearly the whole population is sub-floor.
	LatencySubFloorShareBPS = 5_000
)

const (
	// AnswerStuffingSubShareBPS is retained only as the default for the PUBLISHED
	// stuffed-share statistic; it no longer governs the gate. The gate is now driven
	// by AnswerStuffingMinCases below.
	AnswerStuffingSubShareBPS = 5_000
	// AnswerStuffingMinCases is the default LOW count threshold: the run flags when
	// the number of PROVABLE stuffed cases is >= this. Detection is now STRICT +
	// PROVABLE per case -- a computed case is flagged only when its answer value is
	// nowhere in the run's seeded memory (no legitimate retrieval could surface it)
	// AND appears in a model input before any completion (the model did not derive
	// it), which excludes the operand-equals-answer coincidence at the source. Since
	// a single such case is already proof of injection, the default is 1: a minority
	// stuffer (real exploit lets_5.0 stuffs ~1 of 5 computed cases) now gates.
	AnswerStuffingMinCases = 1
	// AnswerStuffingReviewShareBPS is the default LOOSE systematic-review bar: when
	// the share of COMPUTED cases that fed the finished answer into a model INPUT
	// before any completion (regardless of whether the value also appears in seeded
	// memory) crosses this, the run routes to human REVIEW (never an auto-zero). This
	// catches a coinciding-value stuffer (real exploit lets_5.0 injects a computed
	// answer whose value ALSO appears in memory) that the provable auto-gate cannot
	// prove against RAG. An honest RAG/legit-compute agent's computed answers appear
	// in COMPLETIONS, not inputs, so its loose share is ~0 and it never routes to
	// review. 40% -- a systematic pattern, not an isolated coincidence.
	AnswerStuffingReviewShareBPS = 4_000
)

// DefaultAnswerStuffingPosture is PENALIZE: a flagged run takes a graduated,
// capped penalty proportional to the stuffed share and is NEVER zeroed (one
// flagged case out of many computed cases costs a little; systematic stuffing
// costs up to the cap). It also emits signed evidence for human source review.
//
// This was ENFORCE, on the premise that a finished COMPUTED answer in the
// harness's model-input is a deliberate injection. A real-data calibration run
// disproved that premise: the genuine cleared champion Hogwarts_v1 v18 computes
// a candidate figure, shows it to the model, and the model reasons over it --
// model_dependence PASSED 24/24 dependent (the answer collapses under model
// ablation, i.e. the model IS doing the work) -- yet answer_stuffing flagged one
// such case and, at MinCases=1 enforce, zeroed the whole run. Meanwhile the real
// exploit lets_5.0 is NOT flagged (its laundered value is in-memory, excluded by
// the strict not-in-memory bar). So an ENFORCE auto-gate here floors the honest
// board and misses the exploit. The distinction between "computed a candidate and
// the model still reasoned" (honest) and "computed the answer for the model to
// copy" (Class-D) cannot be made from the runtime signal -- both put a computed
// value in the prompt and both route the answer through the model. Per the Rev7
// production-engine test that is a SOURCE-REVIEW judgment. So this gate surfaces a
// lead; the dataset (drops lets_5.0 to 0.219), the screener source fingerprints,
// and human review are the Class-D defense. An operator may raise it to enforce
// only if a genuinely discriminating signal is found.
var DefaultAnswerStuffingPosture = scoregates.AnswerStuffingPenalize

// DefaultLatencyPosture is REVIEW: the gate emits signed evidence and routes a
// flagged run to human review without zeroing the composite. Latency is gameable
// by burning wall-clock (which already costs the curve-v3 efficiency term), so
// the conservative posture flags rather than gates until the honest-cohort
// sub-floor distribution is measured -- the same shadow-before-enforce ladder the
// relay delay-fingerprint follows. An operator may raise it to enforce.
var DefaultLatencyPosture = scoregates.LatencyReview

// InferenceLatencyTelemetry carries the trusted per-case wall-time aggregate the
// v12 inference-latency gate consumes. It is produced by the relay/scorer path
// that reads runner.CaseExecution.TotalDurationMs for every inference-required,
// model-reached case (see cmd/dittobench-api/v12_latency.go
// v12InferenceLatencyTelemetry -- the single integration point). EligibleCases is
// the number of such cases that carried a trusted wall time; FlaggedCases is how
// many of those returned below the floor.
type InferenceLatencyTelemetry struct {
	AdministeredCases   int
	EligibleCases       int
	FlaggedCases        int
	TelemetryComplete   bool
	AttributionComplete bool
}

// LatencyGateConfig is the operator-tunable posture and thresholds for the v12
// inference-latency gate.
type LatencyGateConfig struct {
	FloorMS  int64
	ShareBPS int
	Posture  scoregates.LatencyPosture
}

// DefaultLatencyGateConfig returns the compiled defaults.
func DefaultLatencyGateConfig() LatencyGateConfig {
	return LatencyGateConfig{
		FloorMS:  LatencyFloorMS,
		ShareBPS: LatencySubFloorShareBPS,
		Posture:  DefaultLatencyPosture,
	}
}

// AttachInferenceLatencyGate layers the v12 inference-latency review gate onto
// already-built gate evidence. It is a no-op for bench_version<12 (v9..v11
// evidence stays byte-identical), and for bench_version>=12 it binds the signed
// latency evidence into the gate digest.
func AttachInferenceLatencyGate(benchVersion int, gates scoregates.Evidence, telemetry InferenceLatencyTelemetry, cfg LatencyGateConfig) (scoregates.Evidence, error) {
	if benchVersion < scoregates.BenchVersionV12 {
		return gates, nil
	}
	return scoregates.AttachInferenceLatency(gates, scoregates.InferenceLatencyInput{
		AdministeredCases:   telemetry.AdministeredCases,
		EligibleCases:       telemetry.EligibleCases,
		FlaggedCases:        telemetry.FlaggedCases,
		FloorMS:             cfg.FloorMS,
		ShareThresholdBPS:   cfg.ShareBPS,
		Posture:             cfg.Posture,
		TelemetryComplete:   telemetry.TelemetryComplete,
		AttributionComplete: telemetry.AttributionComplete,
	})
}

// AnswerStuffingTelemetry carries the trusted per-case answer-provenance
// aggregate the v12 answer-stuffing gate consumes. It is produced by the scorer
// path that, for every COMPUTED-answer, model-reached case, checks the broker's
// recorded clean-pass model I/O for the value-in-input-before-completion
// signature (see cmd/dittobench-api/v9_base.go v12AnswerStuffingTelemetry -- the
// single integration point). EligibleCases is the number of computed cases whose
// provenance settled; StuffedCases is how many of those were flagged.
type AnswerStuffingTelemetry struct {
	AdministeredCases int
	EligibleCases     int
	StuffedCases      int
	// LooseEligibleCases / LooseStuffedCases carry the loose systematic-review
	// signal: the count of COMPUTED cases (answers not verbatim-recalled) whose
	// answer value was fed into a model input before any completion, REGARDLESS of
	// whether the value also appears in seeded memory. It is a superset of
	// StuffedCases and drives the review routing (never an auto-zero).
	LooseEligibleCases  int
	LooseStuffedCases   int
	TelemetryComplete   bool
	AttributionComplete bool
	// ReviewRequired is set when at least one computed, model-reached case could
	// not have its provenance settled because its captured model I/O overflowed the
	// generous per-side bound (a pathological, oversized prompt). It routes the run
	// to human review (full factor) instead of the prior fail-open pass.
	ReviewRequired bool
}

// AnswerStuffingGateConfig is the operator-tunable posture and thresholds for the
// v12 answer-stuffing gate. MinCases is the primary gating lever (the low provable
// stuffed-case count that flags the run); ShareBPS remains only as the published
// stuffed-share statistic.
type AnswerStuffingGateConfig struct {
	MinCases int
	ShareBPS int
	// ReviewShareBPS is the loose systematic-review bar (see
	// AnswerStuffingReviewShareBPS). Zero disables the loose-review signal.
	ReviewShareBPS int
	Posture        scoregates.AnswerStuffingPosture
}

// DefaultAnswerStuffingGateConfig returns the compiled defaults.
func DefaultAnswerStuffingGateConfig() AnswerStuffingGateConfig {
	return AnswerStuffingGateConfig{
		MinCases:       AnswerStuffingMinCases,
		ShareBPS:       AnswerStuffingSubShareBPS,
		ReviewShareBPS: AnswerStuffingReviewShareBPS,
		Posture:        DefaultAnswerStuffingPosture,
	}
}

// AttachAnswerStuffingGate layers the v12 answer-stuffing gate onto already-built
// gate evidence. It is a no-op for bench_version<12 (v9..v11 evidence stays
// byte-identical), and for bench_version>=12 it binds the signed answer-stuffing
// evidence into the gate digest.
func AttachAnswerStuffingGate(benchVersion int, gates scoregates.Evidence, telemetry AnswerStuffingTelemetry, cfg AnswerStuffingGateConfig) (scoregates.Evidence, error) {
	if benchVersion < scoregates.BenchVersionV12 {
		return gates, nil
	}
	return scoregates.AttachAnswerStuffing(gates, scoregates.AnswerStuffingInput{
		AdministeredCases:       telemetry.AdministeredCases,
		EligibleCases:           telemetry.EligibleCases,
		StuffedCases:            telemetry.StuffedCases,
		MinCases:                cfg.MinCases,
		ShareThresholdBPS:       cfg.ShareBPS,
		LooseEligibleCases:      telemetry.LooseEligibleCases,
		LooseStuffedCases:       telemetry.LooseStuffedCases,
		ReviewShareThresholdBPS: cfg.ReviewShareBPS,
		Posture:                 cfg.Posture,
		TelemetryComplete:       telemetry.TelemetryComplete,
		AttributionComplete:     telemetry.AttributionComplete,
		ReviewRequired:          telemetry.ReviewRequired,
	})
}

// AggregateModelTelemetry combines trusted run-level broker accounting with
// scorer-observed non-overlapping ordinary-case windows. Aggregate request
// counts alone still cannot prove how many different cases reached inference.
type AggregateModelTelemetry struct {
	ObservedRequests                uint64
	SuccessfulRequests              uint64
	PromptTokens                    uint64
	CompletionTokens                uint64
	TelemetryComplete               bool
	DistinctCaseAttributionComplete bool
	SuccessfulDistinctCases         int
}

// ModelDependenceTelemetry carries the trusted counterfactual-slice aggregate
// the v12 causal model-dependence gate consumes. It is produced by the relay
// path that re-scores each model-reached case under a deterministically
// perturbed completion (see cmd/dittobench-api/v9_base.go
// v9DependenceTelemetryForVersion — the single integration point). EligibleCases
// is the number of counterfactual-slice cases that settled a verdict;
// DependentCases is how many of those had their scored answer CHANGE.
type ModelDependenceTelemetry struct {
	EligibleCases            int
	DependentCases           int
	TelemetryComplete        bool
	SliceAttributionComplete bool
}

// ExcludedFromModelPopulation reports whether a scored case is outside the
// eligible model population (and therefore outside the v12 counterfactual
// slice): preflight probes, ablation cases, undelivered cases, and validator
// faults. It mirrors the partition BuildGateEvidence applies below.
func ExcludedFromModelPopulation(score protocol.CaseScore) bool {
	return isPreflight(score.CaseID) || isAblation(score.CaseID) || score.ValidatorFault || score.Undelivered
}

// BuildGateEvidence converts trusted ordinary-run observations into the pure
// scoregates contract. It fails closed if aggregate telemetry itself is
// incomplete; lack of distinct-case attribution is published as insufficient
// evidence and therefore receives a zero factor under the enforce contract.
//
// The optional dependence argument carries the v12 counterfactual slice: it
// must be absent for bench_version<12 and, for bench_version>=12, supplies the
// causal model-dependence gate (fail-open to insufficient_evidence with a full
// factor when the relay has not settled the per-case counterfactual comparison).
func BuildGateEvidence(benchVersion int, perCase []protocol.CaseScore, model AggregateModelTelemetry, toolTelemetryComplete bool, dependence ...ModelDependenceTelemetry) (scoregates.Evidence, error) {
	modelInput := scoregates.ModelUseInput{
		AdministeredCases: len(perCase),
		ObservedRequests:  model.ObservedRequests, SuccessfulRequests: model.SuccessfulRequests,
		PromptTokens: model.PromptTokens, CompletionTokens: model.CompletionTokens,
		TelemetryComplete:        model.TelemetryComplete,
		CaseAttributionComplete:  model.DistinctCaseAttributionComplete,
		SuccessfulInferenceCases: model.SuccessfulDistinctCases,
	}
	toolInput := scoregates.AuthoritativeToolInput{TelemetryComplete: toolTelemetryComplete}

	for _, item := range perCase {
		switch {
		case isPreflight(item.CaseID):
			modelInput.Excluded.Preflight++
			continue
		case isAblation(item.CaseID):
			modelInput.Excluded.Ablation++
			continue
		case item.ValidatorFault:
			modelInput.Excluded.ValidatorFault++
			continue
		case item.Undelivered:
			modelInput.Excluded.Undelivered++
			continue
		}
		modelInput.EligibleCases++
		// Called is authoritative only when the scorer marked the trajectory as
		// observed through tool_endpoint. An exact harness self-report cannot
		// create matched or unexpected validator-observed executions.
		authoritativeCalled := item.Called
		if !item.Observed {
			authoritativeCalled = nil
		}
		expected, matched, unexpected := observedToolCounts(item.Expected, authoritativeCalled)
		toolInput.ExpectedExecutions += expected
		toolInput.MatchedExecutions += matched
		toolInput.UnexpectedExecutions += unexpected
	}

	if model.DistinctCaseAttributionComplete && model.SuccessfulDistinctCases > modelInput.EligibleCases {
		return scoregates.Evidence{}, fmt.Errorf("successful distinct inference cases exceed eligible population")
	}

	thresholds := ContractThresholds()
	dependenceInputs, err := dependenceGateInputs(benchVersion, len(perCase), dependence, &thresholds)
	if err != nil {
		return scoregates.Evidence{}, err
	}
	return scoregates.Build(benchVersion, modelInput, toolInput, thresholds, scoregates.RolloutEnforce, dependenceInputs...)
}

// dependenceGateInputs enforces the version contract for the optional
// counterfactual telemetry and, for bench_version>=12, pins the compiled
// dependence threshold and returns the single scoregates dependence input.
func dependenceGateInputs(benchVersion, administeredCases int, dependence []ModelDependenceTelemetry, thresholds *scoregates.Thresholds) ([]scoregates.ModelDependenceInput, error) {
	if benchVersion < scoregates.BenchVersionV12 {
		if len(dependence) != 0 {
			return nil, fmt.Errorf("model-dependence telemetry is defined only for bench v%d and later", scoregates.BenchVersionV12)
		}
		return nil, nil
	}
	if len(dependence) > 1 {
		return nil, fmt.Errorf("at most one model-dependence telemetry aggregate is permitted")
	}
	thresholds.ModelDependenceCoverageBPS = ModelDependenceCoverageBPS
	var telemetry ModelDependenceTelemetry
	if len(dependence) == 1 {
		telemetry = dependence[0]
	}
	return []scoregates.ModelDependenceInput{{
		AdministeredCases:        administeredCases,
		EligibleCases:            telemetry.EligibleCases,
		DependentCases:           telemetry.DependentCases,
		TelemetryComplete:        telemetry.TelemetryComplete,
		SliceAttributionComplete: telemetry.SliceAttributionComplete,
	}}, nil
}

func observedToolCounts(expected, called []string) (expectedN, matched, unexpected int) {
	remaining := make(map[string]int, len(expected))
	for _, name := range expected {
		remaining[name]++
	}
	for _, name := range called {
		if remaining[name] > 0 {
			remaining[name]--
			matched++
		} else {
			unexpected++
		}
	}
	return len(expected), matched, unexpected
}

func isPreflight(caseID string) bool {
	return strings.HasPrefix(caseID, "preflight:") || caseID == modelRoutePreflightCaseID
}

func isAblation(caseID string) bool {
	return strings.HasPrefix(caseID, "ablation:") || strings.HasPrefix(caseID, "__dittobench_ablation_")
}
