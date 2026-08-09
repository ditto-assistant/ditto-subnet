package main

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func applyV9BaseEvidence(
	report protocol.ScoreReport,
	req submitRequest,
	perCase []protocol.CaseScore,
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
	model := v9AggregateModelTelemetry(usage, execution)
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

func v9AggregateModelTelemetry(usage protocol.TokenUsage, execution relayExecutionSummary) v9base.AggregateModelTelemetry {
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
	return v9base.AggregateModelTelemetry{
		ObservedRequests: execution.Requests, SuccessfulRequests: execution.Successes,
		PromptTokens: usage.PromptTokens, CompletionTokens: usage.CompletionTokens,
		TelemetryComplete: complete,
		// Existing broker accounting is run-aggregate. It intentionally cannot
		// claim distinct-case attribution; repeated requests remain diagnostic.
		DistinctCaseAttributionComplete: false,
		SuccessfulDistinctCases:         0,
	}
}
