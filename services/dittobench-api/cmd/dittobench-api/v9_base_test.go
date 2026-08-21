package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/v9base"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestRunCaseWithModelAttributionOverlapsWithoutCaseWindows(t *testing.T) {
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			http.NotFound(w, r)
			return
		}
		var req protocol.RunRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req.InferenceBaseURL != "" {
			t.Errorf("inference_base_url=%q, want empty", req.InferenceBaseURL)
		}
		started <- struct{}{}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"final_text":"ok"}`))
	}))
	defer harness.Close()

	srv := &server{}
	errCh := make(chan error, 2)
	for _, caseID := range []string{"a", "b"} {
		go func(caseID string) {
			_, execution, err := srv.runCaseWithModelAttribution(
				runner.TrustSandbox(context.Background()), "session", harness.URL,
				caseID, "question", nil, runner.CaseOptions{
					BenchVersion: protocol.BenchVersionV10,
				},
			)
			if err != nil {
				errCh <- err
				return
			}
			if execution.ModelAttributionComplete || execution.ModelInferenceObserved || execution.ToolProvenance != nil {
				errCh <- fmt.Errorf("per-case attribution leaked: %+v", execution)
				return
			}
			errCh <- nil
		}(caseID)
	}
	for i := 0; i < 2; i++ {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("overlapping /run did not start")
		}
	}
	close(release)
	for i := 0; i < 2; i++ {
		if err := <-errCh; err != nil {
			t.Fatal(err)
		}
	}
}

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

func completeModelTranscripts(perCase []protocol.CaseScore, observed ...bool) []transcriptCase {
	items := make([]transcriptCase, len(perCase))
	for index, score := range perCase {
		used := index < len(observed) && observed[index]
		items[index] = transcriptCase{
			CaseID: score.CaseID,
			Execution: runner.CaseExecution{
				ModelAttributionComplete: true,
				ModelInferenceObserved:   used,
			},
		}
	}
	return items
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

	got, err := applyV9BaseEvidence(
		report, req, perCase, completeModelTranscripts(perCase, true, true), usage, execution, v9TranscriptSHA,
	)
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
	if !base.ScoreGates.ModelUse.CaseAttributionComplete || base.ScoreGates.ModelUse.Result != string(scoregates.ResultPassed) {
		t.Fatalf("trusted distinct-case coverage missing: %+v", base.ScoreGates.ModelUse)
	}
	if base.ScoreGates.ModelUse.RequestCoverageBPS != scoregates.BasisPointScale ||
		base.ScoreGates.ModelUse.CoverageBPS != scoregates.BasisPointScale ||
		base.SemanticGateFactorBPS != scoregates.BasisPointScale || base.AppliedGateFactorBPS != scoregates.BasisPointScale {
		t.Fatalf("enforced semantic evidence wrong: %+v", base)
	}
	if got.Composite != report.Composite || got.CompositeStderr != report.CompositeStderr {
		t.Fatalf("passing enforce gate changed score: got %v/%v want %v/%v", got.Composite, got.CompositeStderr, report.Composite, report.CompositeStderr)
	}
	if err := v9base.Validate(*base); err != nil {
		t.Fatal(err)
	}
	digest, err := v9base.DigestHex(*base)
	if err != nil || digest != got.BaseEvidenceSHA256 {
		t.Fatalf("root digest = %s, %v; report = %s", digest, err, got.BaseEvidenceSHA256)
	}
}

// Every gated version must turn a proven zero-inference run into the SAME
// signed, finalizable 0.00. v10/v11 matter as much as v9 here: v10 is saturated
// at the 0.997012 ceiling, so a zero-inference agent that fails to produce a
// v11 row keeps that ceiling by carry-forward. The score has to exist for the
// ledger to move off it.
func TestApplyV9BaseEvidenceAcceptsHealthyZeroInferenceAsFactorZero(t *testing.T) {
	for _, version := range gatedZeroUseVersions {
		report := sampleV9Report()
		perCase := []protocol.CaseScore{{CaseID: "memory"}}
		got, err := applyV9BaseEvidence(
			report,
			submitRequest{BenchVersion: version, TarballSHA256: v9ArtifactSHA},
			perCase,
			completeModelTranscripts(perCase, false),
			completeUsage(0, 0, 0, 0),
			relayExecutionSummary{RouteProbeAttempts: 2, RouteProbeRouted: 1},
			v9TranscriptSHA,
		)
		if err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
		base := got.Details.V9Base
		if base.ScoreGates.ModelUse.Result != string(scoregates.ResultZeroInference) || base.SemanticGateFactorBPS != 0 {
			t.Fatalf("v%d zero inference did not become valid zero-factor evidence: %+v", version, base)
		}
		if got.Composite != 0 || got.CompositeStderr != 0 || base.AppliedGateFactorBPS != 0 {
			t.Fatalf("v%d enforce zero-inference did not zero score: %+v", version, got)
		}
		if base.BenchVersion != version {
			t.Fatalf("v%d signed root carries bench version %d", version, base.BenchVersion)
		}
	}
}

func TestApplyV9BaseEvidenceAssemblesV10RootWithSettledAttribution(t *testing.T) {
	// The flip of the transitional v10 skip: base evidence is assembled for every
	// bench version >= 9 on ticket-scope model-use evidence, which survives
	// overlapping /run. A healthy v10 run therefore assembles the same signed
	// base-evidence root v9 does, restoring the score gates and the curve-v3
	// efficiency factor for v10/v11.
	report := sampleV9Report()
	report.Details.BenchVersion = protocol.BenchVersionV10
	req := submitRequest{
		BenchVersion:        protocol.BenchVersionV10,
		TarballSHA256:       v9ArtifactSHA,
		ScreenedImageSHA256: strings.Repeat("e", 64),
	}
	perCase := []protocol.CaseScore{
		{CaseID: "tool", Observed: true, Expected: []string{"search_web"}, Called: []string{"search_web"}},
		{CaseID: "memory"},
	}
	usage := completeUsage(20, 20, 2_000, 400)
	execution := relayExecutionSummary{Requests: 20, Successes: 20}

	got, err := applyV9BaseEvidence(
		report, req, perCase, completeModelTranscripts(perCase, true, true), usage, execution, v9TranscriptSHA,
	)
	if err != nil {
		t.Fatalf("v10 attribution settled but base evidence failed: %v", err)
	}
	base := got.Details.V9Base
	if base == nil || len(got.BaseEvidenceSHA256) != 64 {
		t.Fatalf("v10 must now carry a signed base evidence root: %+v", got)
	}
	if base.BenchVersion != protocol.BenchVersionV10 {
		t.Fatalf("root must bind the submitted v10 bench version, got %d", base.BenchVersion)
	}
	if !base.ScoreGates.ModelUse.CaseAttributionComplete ||
		base.ScoreGates.ModelUse.Result != string(scoregates.ResultPassed) {
		t.Fatalf("v10 distinct-case coverage missing: %+v", base.ScoreGates.ModelUse)
	}
	if got.Composite != report.Composite || got.CompositeStderr != report.CompositeStderr {
		t.Fatalf("passing enforce gate changed v10 score: got %v/%v want %v/%v",
			got.Composite, got.CompositeStderr, report.Composite, report.CompositeStderr)
	}
	if err := v9base.Validate(*base); err != nil {
		t.Fatal(err)
	}
	digest, err := v9base.DigestHex(*base)
	if err != nil || digest != got.BaseEvidenceSHA256 {
		t.Fatalf("root digest = %s, %v; report = %s", digest, err, got.BaseEvidenceSHA256)
	}
}

func TestApplyV9BaseEvidenceAcceptsSessionLevelAttribution(t *testing.T) {
	perCase := []protocol.CaseScore{{CaseID: "memory"}}
	got, err := applyV9BaseEvidence(
		sampleV9Report(),
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		perCase,
		[]transcriptCase{{CaseID: "memory"}},
		completeUsage(1, 1, 100, 20),
		relayExecutionSummary{Requests: 1, Successes: 1},
		v9TranscriptSHA,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Details.V9Base.ScoreGates.ModelUse.CaseAttributionComplete ||
		got.Details.V9Base.ScoreGates.ModelUse.Result != string(scoregates.ResultPassed) {
		t.Fatalf("session-level model_use = %+v", got.Details.V9Base.ScoreGates.ModelUse)
	}

	zero, err := applyV9BaseEvidence(
		sampleV9Report(),
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		perCase,
		[]transcriptCase{{CaseID: "memory"}},
		completeUsage(0, 0, 0, 0),
		relayExecutionSummary{},
		v9TranscriptSHA,
	)
	if err != nil {
		t.Fatal(err)
	}
	if zero.Details.V9Base.ScoreGates.ModelUse.Result != string(scoregates.ResultZeroInference) {
		t.Fatalf("zero inference = %+v", zero.Details.V9Base.ScoreGates.ModelUse)
	}
}

func TestApplyV9BaseEvidenceOmitsV12PerCaseGates(t *testing.T) {
	report := sampleV9Report()
	report.Details.BenchVersion = protocol.BenchVersionV12
	got, err := applyV9BaseEvidence(
		report,
		submitRequest{BenchVersion: protocol.BenchVersionV12, TarballSHA256: v9ArtifactSHA},
		[]protocol.CaseScore{{CaseID: "memory"}},
		[]transcriptCase{{CaseID: "memory"}},
		completeUsage(1, 1, 100, 20),
		relayExecutionSummary{Requests: 1, Successes: 1},
		v9TranscriptSHA,
	)
	if err != nil {
		t.Fatal(err)
	}
	dep := got.Details.V9Base.ScoreGates.ModelDependence
	if dep.Result != string(scoregates.ResultInsufficientEvidence) || dep.FactorBPS != scoregates.BasisPointScale ||
		dep.SliceAttributionComplete {
		t.Fatalf("v12 dependence should be fail-open insufficient_evidence: %+v", dep)
	}
	if got.Details.V9Base.ScoreGates.InferenceLatency.Result != "" ||
		got.Details.V9Base.ScoreGates.AnswerStuffing.Result != "" {
		t.Fatalf("v12 per-case gates still attached: %+v", got.Details.V9Base.ScoreGates)
	}
}

func TestApplyV9BaseEvidenceFailsClosedOnIncompleteAggregateTelemetry(t *testing.T) {
	usage := completeUsage(2, 2, 100, 20)
	perCase := []protocol.CaseScore{{CaseID: "memory"}}
	_, err := applyV9BaseEvidence(
		sampleV9Report(),
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		perCase,
		completeModelTranscripts(perCase, true),
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
			perCase := []protocol.CaseScore{{CaseID: "a"}}
			_, err := applyV9BaseEvidence(report, req, perCase, completeModelTranscripts(perCase, false), completeUsage(0, 0, 0, 0), relayExecutionSummary{}, transcript)
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
	got, err := applyV9BaseEvidence(report, submitRequest{BenchVersion: protocol.BenchVersionV8}, nil, nil, protocol.TokenUsage{}, relayExecutionSummary{}, "")
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

func TestV9AggregateModelTelemetryUsesOnlyTrustedDistinctCases(t *testing.T) {
	usage := completeUsage(1_000, 999, 999_000, 100_000)
	perCase := []protocol.CaseScore{{CaseID: "a"}, {CaseID: "b"}, {CaseID: "c", Undelivered: true}}
	got := v9AggregateModelTelemetry(
		usage,
		relayExecutionSummary{Requests: 1_000, Successes: 999},
		perCase,
		completeModelTranscripts(perCase, true, false, true),
	)
	if !got.TelemetryComplete || !got.DistinctCaseAttributionComplete || got.SuccessfulDistinctCases != 2 {
		t.Fatalf("session-level attribution wrong: %+v", got)
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
			if got := v9AggregateModelTelemetry(tt.usage, tt.execution, nil, nil).TelemetryComplete; got != tt.want {
				t.Fatalf("complete = %t, want %t", got, tt.want)
			}
		})
	}
}

// Under concurrent /run the scorer reads each case's session-scoped tool
// provenance from the broker right after /run returns. A case whose endpoint
// request consumed a model emission settles clean; a case whose request had no
// backing settles with an unmatched attempt; a pre-v10 case and a run without a
// broker session attach nothing.
func TestRunCaseWithModelAttributionAttachesSessionScopedToolProvenance(t *testing.T) {
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"final_text":"ok"}`))
	}))
	defer harness.Close()

	broker := newInferenceBroker(1)
	const sessionID = "v10-session"
	session := &brokerSession{benchVersion: protocol.BenchVersionV10}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()
	session.mu.Lock()
	recordModelToolCallsLocked(session, 0, []byte(`{"choices":[{"message":{"tool_calls":[{
		"id":"call-1","type":"function","function":{"name":"search_web","arguments":"{\"query\":\"veltrix\"}"}
	}]}}]}`))
	if !consumeSessionModelToolCallLocked(session, "clean", "search_web", mustCanonicalArgs(t, `{"query":"veltrix"}`), nil) {
		t.Fatal("clean consumption should succeed")
	}
	if consumeSessionModelToolCallLocked(session, "unbacked", "send_email", mustCanonicalArgs(t, `{}`), nil) {
		t.Fatal("unbacked consumption should fail")
	}
	session.mu.Unlock()

	srv := &server{broker: broker}
	ctx := runner.TrustSandbox(context.Background())
	run := func(sessionID, caseID string, version int) runner.CaseExecution {
		t.Helper()
		_, execution, err := srv.runCaseWithModelAttribution(
			ctx, sessionID, harness.URL, caseID, "question", nil,
			runner.CaseOptions{BenchVersion: version},
		)
		if err != nil {
			t.Fatal(err)
		}
		return execution
	}
	clean := run(sessionID, "clean", protocol.BenchVersionV10).ToolProvenance
	if clean == nil || clean.Matched != 1 || clean.Unmatched != 0 || !clean.Complete {
		t.Fatalf("clean case evidence=%+v", clean)
	}
	unbacked := run(sessionID, "unbacked", protocol.BenchVersionV11).ToolProvenance
	if unbacked == nil || unbacked.Matched != 0 || unbacked.Unmatched != 1 || !unbacked.Complete {
		t.Fatalf("unbacked case evidence=%+v", unbacked)
	}
	if evidence := run(sessionID, "clean", protocol.BenchVersionV9).ToolProvenance; evidence != nil {
		t.Fatalf("v9 must stay frozen without provenance evidence: %+v", evidence)
	}
	if evidence := run("", "clean", protocol.BenchVersionV10).ToolProvenance; evidence != nil {
		t.Fatalf("no session must attach no evidence: %+v", evidence)
	}
	if evidence := run("missing-session", "clean", protocol.BenchVersionV10).ToolProvenance; evidence != nil {
		t.Fatalf("unknown session must attach no evidence: %+v", evidence)
	}
}

func mustCanonicalArgs(t *testing.T, raw string) string {
	t.Helper()
	digest, err := canonicalToolArguments([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	return digest
}
