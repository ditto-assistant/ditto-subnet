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

// runCaseWithModelAttribution runs one scored case over the run-wide inference
// session. Exclusive per-case windows (beginCaseSnapshot plus a case-scoped
// inference URL) forced serial /run; concurrent scoring uses the process-wide
// session URL instead, so no per-case attribution state is opened or closed
// here. Ticket-scope model_use carries model-use anti-cheat; v10+ tool credit is
// carried by session-scoped tool provenance: the broker forwards a
// tool_endpoint request only after consuming a matching model-emitted tool call
// from this session, and the per-case outcome is read here after /run returns.
// Dataset difficulty carries the rest.
func (s *server) runCaseWithModelAttribution(
	ctx context.Context,
	inferenceSessionID string,
	harnessURL string,
	caseID string,
	prompt string,
	tools []protocol.ToolDefinition,
	opts runner.CaseOptions,
) (protocol.RunResponse, runner.CaseExecution, error) {
	response, execution, runErr := runner.RunCaseWithTelemetry(ctx, harnessURL, caseID, prompt, tools, opts)
	if opts.BenchVersion >= protocol.BenchVersionV10 && inferenceSessionID != "" && s.broker != nil {
		execution.ToolProvenance = s.broker.sessionToolProvenance(inferenceSessionID, caseID)
	}
	return response, execution, runErr
}

// v9DelayInconsistentCases counts scored cases whose shadow delay evidence
// came back inconsistent. Reporting only -- the count goes to the operator
// log, never to a score.
//
// The per-case producer of runner.CaseExecution.RelayDelayConsistent was the
// exclusive case window, which concurrent /run no longer opens, so today every
// verdict is nil and this count is always zero. The reader is kept because the
// transcript fields remain in the published contract and a restored per-case
// window would repopulate them.
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

func applyV9BaseEvidence(
	report protocol.ScoreReport,
	req submitRequest,
	perCase []protocol.CaseScore,
	transcripts []transcriptCase,
	usage protocol.TokenUsage,
	execution relayExecutionSummary,
	transcriptSHA256 string,
) (protocol.ScoreReport, error) {
	// Every bench version >= 9 carries the v9 evidence, gate, and curve-v3 stack:
	// the signed base-evidence root, the score gates, and the curve-v3 efficiency
	// factor. Model-use evidence is ticket-scope (session accounting), which
	// survives overlapping /run; the per-case window gates that did not are not
	// administered here.
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
	// verifiably held a response inside that case's window. Log-only; the
	// signed gate evidence below is deliberately untouched.
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
