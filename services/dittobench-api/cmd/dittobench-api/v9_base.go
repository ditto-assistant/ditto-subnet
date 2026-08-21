package main

import (
	"context"
	"fmt"
	"log"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func (s *server) runCaseWithModelAttribution(
	ctx context.Context,
	inferenceSessionID string,
	harnessURL string,
	caseID string,
	prompt string,
	tools []protocol.ToolDefinition,
	opts runner.CaseOptions,
) (protocol.RunResponse, runner.CaseExecution, error) {
	// Exclusive case windows (beginCaseSnapshot / case-scoped inference URLs)
	// forced serial /run. Concurrent scoring uses the process-wide session URL.
	// Ticket-scope model_use carries model-use anti-cheat; v10+ tool credit is
	// carried by session-scoped tool provenance: the broker forwards a
	// tool_endpoint request only after consuming a matching model-emitted tool
	// call from this session, and the per-case outcome is read here after /run
	// returns. Dataset difficulty carries the rest.
	opts.InferenceBaseURL = ""
	opts.CaseScopedInference = false
	response, execution, runErr := runner.RunCaseWithTelemetry(ctx, harnessURL, caseID, prompt, tools, opts)
	if opts.BenchVersion >= protocol.BenchVersionV10 && inferenceSessionID != "" && s.broker != nil {
		execution.ToolProvenance = s.broker.sessionToolProvenance(inferenceSessionID, caseID)
	}
	return response, execution, runErr
}

// v9RelayDelayEvidence turns the case window's delay-fingerprint counters into
// per-case shadow evidence: the total delay the broker verifiably injected
// inside this window, and whether the case's trusted wall time can contain it.
//
// Concurrent calls overlap their holds, so the wall clock is only guaranteed
// to cover the LARGEST single injected delay, which the counters do not carry.
// The per-call mean is a floor on that maximum, so comparing against the mean
// can never flag an honest case -- an inconsistency here means the harness
// returned its answer in less wall time than the relay verifiably spent
// holding a response the same window counts as delivered. nil means
// unmeasured: no delayed call landed in the window (fingerprint off, or the
// case never reached the model).
func v9RelayDelayEvidence(
	before brokerCaseSnapshot,
	after brokerCaseSnapshot,
	totalDurationMs int64,
) (int64, *bool) {
	if after.DelayedRequests < before.DelayedRequests ||
		after.InjectedDelayMS < before.InjectedDelayMS {
		return 0, nil
	}
	injected := after.InjectedDelayMS - before.InjectedDelayMS
	delayed := after.DelayedRequests - before.DelayedRequests
	if delayed == 0 {
		return int64(injected), nil
	}
	consistent := totalDurationMs >= int64(injected/delayed)
	return int64(injected), &consistent
}

// v9DelayInconsistentCases counts scored cases whose shadow delay evidence
// came back inconsistent. Reporting only -- the count goes to the operator
// log, never to a score.
func v9DelayInconsistentCases(transcripts []transcriptCase) int {
	inconsistent := 0
	for _, transcript := range transcripts {
		verdict := transcript.Execution.RelayDelayConsistent
		if verdict != nil && !*verdict {
			inconsistent++
		}
	}
	return inconsistent
}

func v9GenerationCaseDelta(
	before brokerCaseSnapshot,
	after brokerCaseSnapshot,
	beforeErr error,
	afterErr error,
) (observed bool, complete bool) {
	// A fresh v10+ case window opens with ToolEvidenceComplete already true --
	// the per-case tool-evidence ledger is clean until a call invalidates it --
	// whereas a v9 window opens it false. That one bit is initial metadata, not
	// accrued request/success/delay activity, so normalize it away before
	// demanding the window start otherwise empty. Requiring the raw struct to
	// equal the zero value rejected every v10 case (its opening snapshot is
	// non-zero), which is why base evidence had to be skipped for v10+; the
	// empty-window invariant on the real counters is preserved exactly.
	freshBefore := before
	freshBefore.ToolEvidenceComplete = false
	if beforeErr != nil || afterErr != nil || freshBefore != (brokerCaseSnapshot{}) || after.InFlight < 0 ||
		after.Requests < before.Requests || after.Successes < before.Successes ||
		after.DelayedRequests < before.DelayedRequests ||
		after.InjectedDelayMS < before.InjectedDelayMS ||
		after.DelayedRequests > after.Successes {
		return false, false
	}
	requestDelta := after.Requests - before.Requests
	successDelta := after.Successes - before.Successes
	if successDelta > requestDelta {
		return false, false
	}
	// A request still in flight after /run returned cannot have contributed to
	// the returned answer. That includes a handler admitted just before the
	// response which has not read its body or incremented Requests yet. Exclude
	// every unfinished call; its eventual completion stays on this generation
	// and cannot poison this or the next case window.
	return successDelta > 0, true
}

func v9DistinctModelCases(
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
) (complete bool, successful int) {
	if len(perCase) != len(transcripts) {
		return false, 0
	}
	seen := make(map[string]struct{}, len(perCase))
	for index, score := range perCase {
		if score.CaseID == "" || score.CaseID != transcripts[index].CaseID {
			return false, 0
		}
		if _, duplicate := seen[score.CaseID]; duplicate {
			return false, 0
		}
		seen[score.CaseID] = struct{}{}
		if score.Undelivered || score.ValidatorFault {
			continue
		}
		execution := transcripts[index].Execution
		if !execution.ModelAttributionComplete {
			return false, 0
		}
		if execution.ModelInferenceObserved {
			successful++
		}
	}
	return true, successful
}

func applyV9BaseEvidence(
	report protocol.ScoreReport,
	req submitRequest,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
	usage protocol.TokenUsage,
	execution relayExecutionSummary,
	transcriptSHA256 string,
) (protocol.ScoreReport, error) {
	// Bench v10+ carries the v9 evidence, gate, and curve-v3 stack forward. The
	// v10 case-scoped inference path now settles complete per-case attribution:
	// v9GenerationCaseDelta no longer rejects the non-zero opening snapshot every
	// v10 window carries (ToolEvidenceComplete starts true), which was the sole
	// reason enforcing the guard failed every v10 run closed and forced the
	// transitional skip. Base evidence is assembled for every version >= 9 again,
	// so v10/v11 regain the signed root, score gates, and curve-v3 efficiency
	// factor that v9 has.
	if req.BenchVersion < protocol.BenchVersionV9 {
		return report, nil
	}
	if report.Details == nil {
		return protocol.ScoreReport{}, fmt.Errorf("v9 report lacks typed details")
	}
	// The Platform binds v9 base evidence to the submitted agent artifact. The
	// screened image is a separate derived build product and must never replace
	// the tarball identity at this trust boundary.
	artifactSHA256 := req.TarballSHA256
	// Delay-fingerprint shadow reporting (delay_fingerprint.go). An
	// inconsistent case returned its answer in less wall time than the relay
	// verifiably held a response inside that case's window. Log-only until the
	// honest-cohort distribution is measured; the signed gate evidence below
	// is deliberately untouched.
	if inconsistent := v9DelayInconsistentCases(transcripts); inconsistent > 0 {
		log.Printf(
			"run %s: delay-fingerprint shadow: %d case(s) returned faster than their injected relay delay",
			report.RunID, inconsistent,
		)
	}
	model := v9AggregateModelTelemetry(usage, execution, perCase, transcripts)
	// Per-case model-reach, delay-fingerprint, inference-latency, dependence,
	// and answer-stuffing gates required exclusive inference windows. Concurrent
	// /run cannot settle those windows, so they are not administered. v12 still
	// emits model_dependence so the signed digest stays valid, using the
	// contract's fail-open encoding for an unsettled counterfactual slice:
	// SliceAttributionComplete=false -> insufficient_evidence with a full factor.
	// (not_applicable would assert a settled slice with no model cases, which is
	// not what happened.)
	var dependence []v9base.ModelDependenceTelemetry
	if req.BenchVersion >= protocol.BenchVersionV12 {
		dependence = []v9base.ModelDependenceTelemetry{{
			TelemetryComplete:        true,
			SliceAttributionComplete: false,
		}}
	}
	gates, err := v9base.BuildGateEvidence(req.BenchVersion, perCase, model, true, dependence...)
	if err != nil {
		return protocol.ScoreReport{}, fmt.Errorf("build v9 score-gate evidence: %w", err)
	}
	details, digest, effective, err := v9base.Build(v9base.Inputs{
		RunID: report.RunID, BenchVersion: req.BenchVersion, ArtifactSHA256: artifactSHA256,
		DatasetSHA256: report.Details.DatasetSHA256, TranscriptSHA256: transcriptSHA256,
		Ordinary: scoregates.Score{Composite: report.Composite, CompositeStderr: report.CompositeStderr},
		Gates:    gates,
	})
	if err != nil {
		return protocol.ScoreReport{}, fmt.Errorf("build v9 base evidence root: %w", err)
	}
	report.Details.V9Base = &details
	report.BaseEvidenceSHA256 = digest
	report.Composite = effective.Composite
	report.CompositeStderr = effective.CompositeStderr
	return report, nil
}

func v9AggregateModelTelemetry(
	usage protocol.TokenUsage,
	execution relayExecutionSummary,
	perCase []protocol.CaseScore,
	_ []transcriptCase,
) v9base.AggregateModelTelemetry {
	complete := usage.AccountingVersion == 2 && usage.Status == "complete" &&
		usage.Requests == execution.Requests && usage.Successes == execution.Successes &&
		usage.Successes <= usage.Requests && usage.UsageAvailable == usage.Successes &&
		usage.UsageUnavailable == 0 && usage.PromptTokens <= ^uint64(0)-usage.CompletionTokens &&
		usage.TotalTokens == usage.PromptTokens+usage.CompletionTokens
	if usage.Successes == 0 {
		complete = complete && usage.PromptTokens == 0 && usage.CompletionTokens == 0 && usage.TotalTokens == 0
	} else {
		complete = complete && usage.PromptTokens > 0 && usage.TotalTokens > 0
	}
	// Ticket-scope attribution: session accounting is the trust bit. Distinct
	// case windows are no longer required. Cap successes at the eligible
	// population so retries cannot inflate coverage past 100%.
	successfulCases := 0
	if complete && usage.Successes > 0 {
		successfulCases = int(usage.Successes)
		if eligible := v9EligibleModelCases(perCase); successfulCases > eligible {
			successfulCases = eligible
		}
	}
	return v9base.AggregateModelTelemetry{
		ObservedRequests: execution.Requests, SuccessfulRequests: execution.Successes,
		PromptTokens: usage.PromptTokens, CompletionTokens: usage.CompletionTokens,
		TelemetryComplete:               complete,
		DistinctCaseAttributionComplete: complete,
		SuccessfulDistinctCases:         successfulCases,
	}
}

func v9EligibleModelCases(perCase []protocol.CaseScore) int {
	eligible := 0
	for _, score := range perCase {
		if !v9base.ExcludedFromModelPopulation(score) {
			eligible++
		}
	}
	return eligible
}

// v9DependenceTelemetryForVersion is the SINGLE integration point for the Bench
// v12 causal model-dependence gate (issue #532). For every model-reached,
// non-excluded scored case it reads the relay's counterfactual verdict
// (CaseExecution.ModelCounterfactualObserved / ModelCounterfactualDependent):
// the case was re-run under FULL model ablation (no usable completion) and its
// answer graded against the same expected answer. The verdict is
// CORRECTNESS-preserved:
//
//   - ModelCounterfactualObserved=true, ModelCounterfactualDependent=&true:
//     administered, clean-correct, ablated INCORRECT -> DEPENDENT (the agent
//     needed the model). Counted in EligibleCases AND DependentCases.
//   - ...Dependent=&false: administered, clean-correct, ablated STILL correct ->
//     INDEPENDENT (a launderer recovered the answer with no working model).
//     Counted in EligibleCases only.
//   - ...Observed=true, Dependent=nil: administered but the clean run was already
//     incorrect -> excluded from the dependent/independent tally (kept out of the
//     denominator), yet it still counts toward slice-attribution completeness.
//   - ...Observed=false: NOT administered -> pending; the reader keeps
//     SliceAttributionComplete false so the gate fails OPEN.
//
// A launderer stays correct under ablation, so DependentCases stays low against
// EligibleCases and the dependent share falls below threshold -> the gate floors
// the composite; a genuine agent's correctness collapses under ablation, so
// DependentCases dominates and it passes.
//
// The gate consumes this aggregate as trusted evidence. SliceAttributionComplete
// is the one trust bit that requires the relay: it is true only once EVERY
// eligible model-reached case has been administered a verdict. Until the relay
// populates the per-case CaseExecution fields, at least one eligible case lacks a
// verdict (or none exist), SliceAttributionComplete is false, and the gate
// publishes insufficient_evidence with a full factor -- a signed review
// signal rather than a false zero. Returns an empty slice for bench_version<12
// so v9..v11 evidence is byte-identical.
func v9DependenceTelemetryForVersion(
	benchVersion int,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
) []v9base.ModelDependenceTelemetry {
	if benchVersion < protocol.BenchVersionV12 {
		return nil
	}
	// Misaligned transcripts cannot be trusted to attribute counterfactuals;
	// leave telemetry incomplete so BuildGateEvidence rejects the evidence.
	if len(perCase) != len(transcripts) {
		return []v9base.ModelDependenceTelemetry{{}}
	}
	eligible, dependent, pending, modelReached := 0, 0, 0, 0
	for index, score := range perCase {
		if v9base.ExcludedFromModelPopulation(score) {
			continue
		}
		execution := transcripts[index].Execution
		if !execution.ModelInferenceObserved {
			// A case that never reached the model cannot depend on it; it is not
			// part of the counterfactual slice.
			continue
		}
		modelReached++
		if !execution.ModelCounterfactualObserved {
			// The counterfactual was never administered for this case.
			pending++
			continue
		}
		// Administered. A nil verdict is an administered-but-clean-incorrect case,
		// excluded from the dependent/independent tally but NOT pending.
		if execution.ModelCounterfactualDependent != nil {
			eligible++
			if *execution.ModelCounterfactualDependent {
				dependent++
			}
		}
	}
	return []v9base.ModelDependenceTelemetry{{
		EligibleCases:            eligible,
		DependentCases:           dependent,
		TelemetryComplete:        true,
		SliceAttributionComplete: modelReached > 0 && pending == 0,
	}}
}

// v12AnswerStuffingTelemetry is the SINGLE integration point for the Bench v12
// Class-D answer-stuffing gate. For every model-reached, non-excluded scored case
// it reads the two per-case telemetry fields the scorer's provenance pass wrote
// (CaseExecution.AnswerStuffObserved / AnswerStuffed):
//
//   - Observed=false: the broker's clean-pass I/O for this model-reached case was
//     unavailable or truncated -> PENDING; the reader keeps AttributionComplete
//     false so the gate fails OPEN (a detection gate never penalizes an honest run
//     for missing capture).
//   - Observed=true, AnswerStuffed=nil: settled, but the case is verbatim-recall
//     (non-computed) -> excluded from the stuffed/clean tally (kept out of the
//     denominator), yet counted as settled.
//   - Observed=true, AnswerStuffed=&true: a COMPUTED case whose finished answer
//     appeared in a model input before any completion -> EligibleCases AND
//     StuffedCases.
//   - Observed=true, AnswerStuffed=&false: a COMPUTED case with clean provenance
//     -> EligibleCases only.
//   - Observed=true, AnswerStuffReviewRequired=true: a COMPUTED case whose capture
//     overflowed the per-side ceiling (a prompt beyond the model's full context
//     window). NOT pending (it does not fail the run open) but sets ReviewRequired,
//     routing the run to human review with a full factor. A legitimate full-context
//     prompt can never reach the ceiling, so this never fires on honest deep-RAG.
//
// Returns an empty slice's zero aggregate for bench_version<12 so v9..v11 evidence
// is byte-identical. Unlike model_dependence, an all-clean run (0 stuffed) is a
// PASS: this gate detects a bad thing rather than proving a good thing, so absence
// of stuffing is not fail-closed.
func v12AnswerStuffingTelemetry(
	benchVersion int,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
) v9base.AnswerStuffingTelemetry {
	if benchVersion < protocol.BenchVersionV12 {
		return v9base.AnswerStuffingTelemetry{}
	}
	telemetry := v9base.AnswerStuffingTelemetry{
		AdministeredCases: len(perCase),
		TelemetryComplete: true,
	}
	if len(perCase) != len(transcripts) {
		// Misaligned transcripts cannot attribute provenance; leave attribution
		// incomplete so the gate fails open.
		return telemetry
	}
	pending := 0
	for index, score := range perCase {
		if v9base.ExcludedFromModelPopulation(score) {
			continue
		}
		execution := transcripts[index].Execution
		if !execution.ModelInferenceObserved {
			// A case that never reached the model cannot be an answer-stuffing case.
			continue
		}
		if !execution.AnswerStuffObserved {
			// Provenance was not settled for this model-reached case (a genuine capture
			// gap). Fail OPEN.
			pending++
			continue
		}
		if execution.AnswerStuffReviewRequired {
			// Settled as "capture overflowed the per-side ceiling on a COMPUTED case"
			// (a pathological prompt beyond the model's full context window). Not
			// pending -- so it does not fail the run open -- but it routes the run to
			// human review with a full factor. A legitimate full-context prompt can
			// never reach the ceiling, so this never fires on honest deep-RAG.
			telemetry.ReviewRequired = true
			continue
		}
		// Settled. A nil verdict is a verbatim-recall (non-computed) case, excluded
		// from the tally but NOT pending.
		if execution.AnswerStuffed != nil {
			telemetry.EligibleCases++
			if *execution.AnswerStuffed {
				telemetry.StuffedCases++
			}
		}
		// The LOOSE systematic-review signal is a superset of the provable slice: it
		// counts every COMPUTED (not verbatim-recall) case, regardless of whether the
		// answer value also appears in seeded memory. A coinciding-value stuffer that
		// the provable slice excludes still lands here, so the run can route to review.
		if execution.AnswerStuffLoose != nil {
			telemetry.LooseEligibleCases++
			if *execution.AnswerStuffLoose {
				telemetry.LooseStuffedCases++
			}
		}
	}
	telemetry.AttributionComplete = pending == 0
	return telemetry
}
