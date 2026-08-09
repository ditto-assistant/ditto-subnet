package main

import (
	"encoding/json"
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const (
	v9ArtifactSHA   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	v9DatasetSHA    = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	v9TranscriptSHA = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
)

func sampleV9Report() protocol.ScoreReport {
	return protocol.ScoreReport{
		RunID: "run-v9", Composite: 0.75, CompositeStderr: 0.125,
		ToolMean: 0.8, MemoryMean: 0.7, N: 2,
		Details: &protocol.RunDetails{
			BenchVersion: protocol.BenchVersionV9, DatasetSHA256: v9DatasetSHA,
			ToolMean: 0.8, MemoryMean: 0.7,
		},
	}
}

func completeUsage(requests, successes, prompt, completion uint64) protocol.TokenUsage {
	return protocol.TokenUsage{
		AccountingVersion: 2, Status: "complete", Requests: requests, Successes: successes,
		UsageAvailable: successes, PromptTokens: prompt, CompletionTokens: completion,
		TotalTokens: prompt + completion,
	}
}

func TestApplyV9BaseEvidencePublishesTypedSignedRoot(t *testing.T) {
	report := sampleV9Report()
	req := submitRequest{
		BenchVersion:        protocol.BenchVersionV9,
		TarballSHA256:       v9ArtifactSHA,
		ScreenedImageSHA256: strings.Repeat("e", 64),
	}
	perCase := []protocol.CaseScore{
		{CaseID: "tool", Observed: true, Expected: []string{"search_web"}, Called: []string{"search_web"}},
		{CaseID: "memory"},
	}
	usage := completeUsage(20, 20, 2_000, 400)
	execution := relayExecutionSummary{Requests: 20, Successes: 20}

	got, err := applyV9BaseEvidence(report, req, perCase, usage, execution, v9TranscriptSHA)
	if err != nil {
		t.Fatal(err)
	}
	if got.Details == nil || got.Details.V9Base == nil || len(got.BaseEvidenceSHA256) != 64 {
		t.Fatalf("missing typed v9 root: %+v", got)
	}
	base := got.Details.V9Base
	if base.RunID != report.RunID || base.ArtifactSHA256 != v9ArtifactSHA ||
		base.DatasetSHA256 != v9DatasetSHA || base.TranscriptSHA256 != v9TranscriptSHA {
		t.Fatalf("identity not bound: %+v", base)
	}
	if base.ScoreGates.ModelUse.CaseAttributionComplete || base.ScoreGates.ModelUse.Result != string(scoregates.ResultInsufficientEvidence) {
		t.Fatalf("aggregate requests claimed distinct-case coverage: %+v", base.ScoreGates.ModelUse)
	}
	if base.ScoreGates.ModelUse.RequestCoverageBPS != scoregates.BasisPointScale ||
		base.ScoreGates.ModelUse.CoverageBPS != 0 || base.SemanticGateFactorBPS != 0 || base.AppliedGateFactorBPS != scoregates.BasisPointScale {
		t.Fatalf("shadow diagnostic/application split wrong: %+v", base)
	}
	if got.Composite != report.Composite || got.CompositeStderr != report.CompositeStderr {
		t.Fatalf("shadow changed score: got %v/%v want %v/%v", got.Composite, got.CompositeStderr, report.Composite, report.CompositeStderr)
	}
	if err := v9base.Validate(*base); err != nil {
		t.Fatal(err)
	}
	digest, err := v9base.DigestHex(*base)
	if err != nil || digest != got.BaseEvidenceSHA256 {
		t.Fatalf("root digest = %s, %v; report = %s", digest, err, got.BaseEvidenceSHA256)
	}
}

func TestApplyV9BaseEvidenceAcceptsHealthyZeroInferenceAsFactorZero(t *testing.T) {
	report := sampleV9Report()
	got, err := applyV9BaseEvidence(
		report,
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		[]protocol.CaseScore{{CaseID: "memory"}},
		completeUsage(0, 0, 0, 0), relayExecutionSummary{}, v9TranscriptSHA,
	)
	if err != nil {
		t.Fatal(err)
	}
	base := got.Details.V9Base
	if base.ScoreGates.ModelUse.Result != string(scoregates.ResultZeroInference) || base.SemanticGateFactorBPS != 0 {
		t.Fatalf("zero inference did not become valid zero-factor evidence: %+v", base)
	}
	if got.Composite != report.Composite {
		t.Fatalf("shadow zero-inference changed score: %v != %v", got.Composite, report.Composite)
	}
}

func TestApplyV9BaseEvidenceFailsClosedOnIncompleteAggregateTelemetry(t *testing.T) {
	usage := completeUsage(2, 2, 100, 20)
	_, err := applyV9BaseEvidence(
		sampleV9Report(),
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		[]protocol.CaseScore{{CaseID: "memory"}},
		usage, relayExecutionSummary{Requests: 3, Successes: 2}, v9TranscriptSHA,
	)
	if err == nil || !strings.Contains(err.Error(), "telemetry unavailable") {
		t.Fatalf("error = %v, want fail-closed telemetry error", err)
	}
}

func TestApplyV9BaseEvidenceRejectsMissingTypedIdentity(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*protocol.ScoreReport, *submitRequest, *string)
	}{
		{"details", func(r *protocol.ScoreReport, _ *submitRequest, _ *string) { r.Details = nil }},
		{"artifact", func(_ *protocol.ScoreReport, r *submitRequest, _ *string) { r.TarballSHA256 = "" }},
		{"dataset", func(r *protocol.ScoreReport, _ *submitRequest, _ *string) { r.Details.DatasetSHA256 = "" }},
		{"transcript", func(_ *protocol.ScoreReport, _ *submitRequest, s *string) { *s = "" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			report := sampleV9Report()
			req := submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA}
			transcript := v9TranscriptSHA
			tt.mutate(&report, &req, &transcript)
			_, err := applyV9BaseEvidence(report, req, []protocol.CaseScore{{CaseID: "a"}}, completeUsage(0, 0, 0, 0), relayExecutionSummary{}, transcript)
			if err == nil {
				t.Fatal("missing v9 identity unexpectedly passed")
			}
		})
	}
}

func TestApplyV9BaseEvidencePreservesPreV9ReportBitsAndJSON(t *testing.T) {
	report := protocol.ScoreReport{
		RunID: "run-v8", Seed: 8, GeneratedAt: "epoch",
		Composite: math.Float64frombits(0x3fe0123456789abc), CompositeStderr: math.Float64frombits(0x3fa0123456789abc),
		ToolMean: 0.75, MemoryMean: 0.5, N: 2,
		Details: &protocol.RunDetails{BenchVersion: protocol.BenchVersionV8, DatasetSHA256: v9DatasetSHA, ToolMean: 0.75, MemoryMean: 0.5},
	}
	wantJSON, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	got, err := applyV9BaseEvidence(report, submitRequest{BenchVersion: protocol.BenchVersionV8}, nil, protocol.TokenUsage{}, relayExecutionSummary{}, "")
	if err != nil {
		t.Fatal(err)
	}
	gotJSON, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, report) || string(gotJSON) != string(wantJSON) ||
		math.Float64bits(got.Composite) != math.Float64bits(report.Composite) ||
		math.Float64bits(got.CompositeStderr) != math.Float64bits(report.CompositeStderr) {
		t.Fatalf("pre-v9 report changed:\ngot  %s\nwant %s", gotJSON, wantJSON)
	}
}

func TestV9AggregateModelTelemetryNeverInventsDistinctCases(t *testing.T) {
	usage := completeUsage(1_000, 999, 999_000, 100_000)
	got := v9AggregateModelTelemetry(usage, relayExecutionSummary{Requests: 1_000, Successes: 999})
	if !got.TelemetryComplete || got.DistinctCaseAttributionComplete || got.SuccessfulDistinctCases != 0 {
		t.Fatalf("aggregate telemetry invented case attribution: %+v", got)
	}
}

func TestV9AggregateModelTelemetryCompletenessMatrix(t *testing.T) {
	valid := completeUsage(2, 2, 100, 20)
	tests := []struct {
		name      string
		usage     protocol.TokenUsage
		execution relayExecutionSummary
		want      bool
	}{
		{"complete", valid, relayExecutionSummary{Requests: 2, Successes: 2}, true},
		{"healthy zero", completeUsage(0, 0, 0, 0), relayExecutionSummary{}, true},
		{"accounting", func() protocol.TokenUsage { v := valid; v.AccountingVersion = 1; return v }(), relayExecutionSummary{Requests: 2, Successes: 2}, false},
		{"status", func() protocol.TokenUsage { v := valid; v.Status = "partial"; return v }(), relayExecutionSummary{Requests: 2, Successes: 2}, false},
		{"request mismatch", valid, relayExecutionSummary{Requests: 3, Successes: 2}, false},
		{"success mismatch", valid, relayExecutionSummary{Requests: 2, Successes: 1}, false},
		{"usage missing", func() protocol.TokenUsage { v := valid; v.UsageAvailable = 1; return v }(), relayExecutionSummary{Requests: 2, Successes: 2}, false},
		{"usage unavailable", func() protocol.TokenUsage { v := valid; v.UsageUnavailable = 1; return v }(), relayExecutionSummary{Requests: 2, Successes: 2}, false},
		{"total", func() protocol.TokenUsage { v := valid; v.TotalTokens++; return v }(), relayExecutionSummary{Requests: 2, Successes: 2}, false},
		{"successful no prompt", completeUsage(1, 1, 0, 1), relayExecutionSummary{Requests: 1, Successes: 1}, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := v9AggregateModelTelemetry(tt.usage, tt.execution).TelemetryComplete; got != tt.want {
				t.Fatalf("complete = %t, want %t", got, tt.want)
			}
		})
	}
}
