package main

import (
	"context"
	"fmt"

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
	if opts.BenchVersion != protocol.BenchVersionV9 || inferenceSessionID == "" {
		return runner.RunCaseWithTelemetry(ctx, harnessURL, caseID, prompt, tools, opts)
	}
	generation, before, beforeErr := s.broker.beginCaseSnapshot(inferenceSessionID)
	response, execution, runErr := runner.RunCaseWithTelemetry(
		ctx, harnessURL, caseID, prompt, tools, opts,
	)
	after, afterErr := s.broker.endCaseSnapshot(inferenceSessionID, generation)
	execution.ModelInferenceObserved, execution.ModelAttributionComplete =
		v9GenerationCaseDelta(before, after, beforeErr, afterErr)
	return response, execution, runErr
}

func v9GenerationCaseDelta(
	before brokerCaseSnapshot,
	after brokerCaseSnapshot,
	beforeErr error,
	afterErr error,
) (observed bool, complete bool) {
	if beforeErr != nil || afterErr != nil || before.InFlight != 0 || after.InFlight < 0 ||
		after.Requests < before.Requests || after.Successes < before.Successes {
		return false, false
	}
	requestDelta := after.Requests - before.Requests
	successDelta := after.Successes - before.Successes
	if successDelta > requestDelta {
		return false, false
	}
	if uint64(after.InFlight) > requestDelta-successDelta {
		return false, false
	}
	// A request still in flight after /run returned cannot have contributed to
	// the returned answer. Its eventual completion stays on this generation and
	// is conservatively excluded instead of poisoning the next case window.
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
	if req.BenchVersion != protocol.BenchVersionV9 {
		return report, nil
	}
	if report.Details == nil {
		return protocol.ScoreReport{}, fmt.Errorf("v9 report lacks typed details")
	}
	// The Platform binds v9 base evidence to the submitted agent artifact. The
	// screened image is a separate derived build product and must never replace
	// the tarball identity at this trust boundary.
	artifactSHA256 := req.TarballSHA256
	model := v9AggregateModelTelemetry(usage, execution, perCase, transcripts)
	if !model.DistinctCaseAttributionComplete {
		return protocol.ScoreReport{}, fmt.Errorf(
			"v9 case attribution unavailable: trusted relay windows did not settle",
		)
	}
	gates, err := v9base.BuildGateEvidence(perCase, model, true)
	if err != nil {
		return protocol.ScoreReport{}, fmt.Errorf("build v9 score-gate evidence: %w", err)
	}
	details, digest, effective, err := v9base.Build(v9base.Inputs{
		RunID: report.RunID, ArtifactSHA256: artifactSHA256,
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
