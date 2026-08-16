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
	if opts.BenchVersion < protocol.BenchVersionV9 || inferenceSessionID == "" {
		return runner.RunCaseWithTelemetry(ctx, harnessURL, caseID, prompt, tools, opts)
	}
	if opts.BenchVersion >= protocol.BenchVersionV10 && opts.CaseScopedInference {
		generation, before, capability, beforeErr := s.broker.beginCaseCapability(
			inferenceSessionID, caseID,
		)
		if beforeErr == nil {
			opts.InferenceBaseURL = harnessGateway(inferenceSessionID) + "/cases/" + capability
		}
		response, execution, runErr := runner.RunCaseWithTelemetry(
			ctx, harnessURL, caseID, prompt, tools, opts,
		)
		after, afterErr := s.broker.endCaseCapability(inferenceSessionID, generation)
		execution.ModelInferenceObserved, execution.ModelAttributionComplete =
			v9GenerationCaseDelta(before, after, beforeErr, afterErr)
		if execution.ModelAttributionComplete {
			execution.RelayInjectedDelayMs, execution.RelayDelayConsistent =
				v9RelayDelayEvidence(before, after, execution.TotalDurationMs)
		}
		if beforeErr == nil && afterErr == nil {
			execution.ToolProvenance = toolProvenanceEvidence(after)
		}
		return response, execution, runErr
	}
	generation, before, beforeErr := s.broker.beginCaseSnapshot(inferenceSessionID, caseID)
	response, execution, runErr := runner.RunCaseWithTelemetry(
		ctx, harnessURL, caseID, prompt, tools, opts,
	)
	after, afterErr := s.broker.endCaseSnapshot(inferenceSessionID, generation)
	execution.ModelInferenceObserved, execution.ModelAttributionComplete =
		v9GenerationCaseDelta(before, after, beforeErr, afterErr)
	if execution.ModelAttributionComplete {
		execution.RelayInjectedDelayMs, execution.RelayDelayConsistent =
			v9RelayDelayEvidence(before, after, execution.TotalDurationMs)
	}
	if opts.BenchVersion >= protocol.BenchVersionV10 && beforeErr == nil && afterErr == nil {
		execution.ToolProvenance = toolProvenanceEvidence(after)
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
	if !model.DistinctCaseAttributionComplete {
		return protocol.ScoreReport{}, fmt.Errorf(
			"v9 case attribution unavailable: trusted relay windows did not settle",
		)
	}
	gates, err := v9base.BuildGateEvidence(req.BenchVersion, perCase, model, true)
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
	transcripts []transcriptCase,
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
	attributionComplete, successfulCases := v9DistinctModelCases(perCase, transcripts)
	return v9base.AggregateModelTelemetry{
		ObservedRequests: execution.Requests, SuccessfulRequests: execution.Successes,
		PromptTokens: usage.PromptTokens, CompletionTokens: usage.CompletionTokens,
		TelemetryComplete:               complete,
		DistinctCaseAttributionComplete: attributionComplete,
		SuccessfulDistinctCases:         successfulCases,
	}
}
