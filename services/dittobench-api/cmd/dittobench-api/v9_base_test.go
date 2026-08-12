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

func TestRunCaseWithModelAttributionExcludesBrokerTailFromReturnedAnswer(t *testing.T) {
	broker := newInferenceBroker(1)
	sessionID := "case-attribution-tail"
	session := &brokerSession{requests: 10, successes: 10}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()

	requestStarted := make(chan struct{})
	releaseRequest := make(chan struct{})
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/run" {
			http.NotFound(w, r)
			return
		}
		session.mu.Lock()
		generation := session.activeCaseGeneration
		snapshot := session.caseSnapshots[generation]
		snapshot.Requests++
		snapshot.InFlight++
		session.caseSnapshots[generation] = snapshot
		session.requests++
		session.inFlight++
		session.mu.Unlock()
		close(requestStarted)
		go func() {
			<-releaseRequest
			session.mu.Lock()
			snapshot := session.caseSnapshots[generation]
			snapshot.Successes++
			snapshot.InFlight--
			session.caseSnapshots[generation] = snapshot
			session.successes++
			session.inFlight--
			session.mu.Unlock()
		}()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"final_text":"ok"}`))
	}))
	defer harness.Close()

	type outcome struct {
		execution runner.CaseExecution
		err       error
	}
	result := make(chan outcome, 1)
	go func() {
		_, execution, err := (&server{broker: broker}).runCaseWithModelAttribution(
			runner.TrustSandbox(context.Background()), sessionID, harness.URL,
			"case-1", "question", nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV9},
		)
		result <- outcome{execution: execution, err: err}
	}()
	<-requestStarted
	select {
	case got := <-result:
		if got.err != nil {
			t.Fatal(got.err)
		}
		if !got.execution.ModelAttributionComplete || got.execution.ModelInferenceObserved {
			t.Fatalf("model attribution = %+v", got.execution)
		}
	case <-time.After(time.Second):
		t.Fatal("case waited for a request that could not inform its returned answer")
	}
	close(releaseRequest)
	deadline := time.Now().Add(time.Second)
	for {
		snapshot, err := broker.generationCaseSnapshot(sessionID, 1)
		if err != nil {
			t.Fatal(err)
		}
		if snapshot == (brokerCaseSnapshot{Requests: 1, Successes: 1}) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("tail did not remain on original generation: %+v", snapshot)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestRunCaseWithModelAttributionExcludesAdmittedUncountedTail(t *testing.T) {
	broker := newInferenceBroker(1)
	sessionID := "case-attribution-admitted-tail"
	session := &brokerSession{}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()

	requestStarted := make(chan struct{})
	releaseRequest := make(chan struct{})
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		session.mu.Lock()
		generation := session.activeCaseGeneration
		snapshot := session.caseSnapshots[generation]
		snapshot.InFlight++
		session.caseSnapshots[generation] = snapshot
		session.inFlight++
		session.mu.Unlock()
		close(requestStarted)
		go func() {
			<-releaseRequest
			session.mu.Lock()
			snapshot := session.caseSnapshots[generation]
			snapshot.InFlight--
			session.caseSnapshots[generation] = snapshot
			session.inFlight--
			session.mu.Unlock()
		}()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"final_text":"ok"}`))
	}))
	defer harness.Close()

	type outcome struct {
		execution runner.CaseExecution
		err       error
	}
	result := make(chan outcome, 1)
	go func() {
		_, execution, err := (&server{broker: broker}).runCaseWithModelAttribution(
			runner.TrustSandbox(context.Background()), sessionID, harness.URL,
			"case-1", "question", nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV9},
		)
		result <- outcome{execution: execution, err: err}
	}()
	<-requestStarted
	got := <-result
	if got.err != nil {
		t.Fatal(got.err)
	}
	if !got.execution.ModelAttributionComplete || got.execution.ModelInferenceObserved {
		t.Fatalf("model attribution = %+v", got.execution)
	}
	close(releaseRequest)
}

func TestRunCaseWithModelAttributionCountsSettledGenerationSuccess(t *testing.T) {
	broker := newInferenceBroker(1)
	sessionID := "case-attribution-success"
	session := &brokerSession{}
	broker.mu.Lock()
	broker.sessions[sessionID] = session
	broker.mu.Unlock()
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		session.mu.Lock()
		generation := session.activeCaseGeneration
		snapshot := session.caseSnapshots[generation]
		snapshot.Requests++
		snapshot.Successes++
		session.caseSnapshots[generation] = snapshot
		session.requests++
		session.successes++
		session.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"final_text":"ok"}`))
	}))
	defer harness.Close()
	_, execution, err := (&server{broker: broker}).runCaseWithModelAttribution(
		runner.TrustSandbox(context.Background()), sessionID, harness.URL,
		"case-1", "question", nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV9},
	)
	if err != nil {
		t.Fatal(err)
	}
	if !execution.ModelAttributionComplete || !execution.ModelInferenceObserved {
		t.Fatalf("model attribution = %+v", execution)
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

func TestApplyV9BaseEvidenceAcceptsHealthyZeroInferenceAsFactorZero(t *testing.T) {
	report := sampleV9Report()
	perCase := []protocol.CaseScore{{CaseID: "memory"}}
	got, err := applyV9BaseEvidence(
		report,
		submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
		perCase,
		completeModelTranscripts(perCase, false),
		completeUsage(0, 0, 0, 0), relayExecutionSummary{}, v9TranscriptSHA,
	)
	if err != nil {
		t.Fatal(err)
	}
	base := got.Details.V9Base
	if base.ScoreGates.ModelUse.Result != string(scoregates.ResultZeroInference) || base.SemanticGateFactorBPS != 0 {
		t.Fatalf("zero inference did not become valid zero-factor evidence: %+v", base)
	}
	if got.Composite != 0 || got.CompositeStderr != 0 || base.AppliedGateFactorBPS != 0 {
		t.Fatalf("enforce zero-inference did not zero score: %+v", got)
	}
}

func TestApplyV9BaseEvidenceRejectsIncompleteCaseAttribution(t *testing.T) {
	perCase := []protocol.CaseScore{{CaseID: "memory"}}
	tests := []struct {
		name      string
		usage     protocol.TokenUsage
		execution relayExecutionSummary
	}{
		{
			name:      "successful relay request",
			usage:     completeUsage(1, 1, 100, 20),
			execution: relayExecutionSummary{Requests: 1, Successes: 1},
		},
		{
			name:      "zero inference",
			usage:     completeUsage(0, 0, 0, 0),
			execution: relayExecutionSummary{},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := applyV9BaseEvidence(
				sampleV9Report(),
				submitRequest{BenchVersion: protocol.BenchVersionV9, TarballSHA256: v9ArtifactSHA},
				perCase,
				[]transcriptCase{{CaseID: "memory"}},
				tt.usage,
				tt.execution,
				v9TranscriptSHA,
			)
			if err == nil || !strings.Contains(err.Error(), "v9 case attribution unavailable") {
				t.Fatalf("error = %v, want typed attribution failure", err)
			}
		})
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
	if !got.TelemetryComplete || !got.DistinctCaseAttributionComplete || got.SuccessfulDistinctCases != 1 {
		t.Fatalf("trusted case attribution wrong: %+v", got)
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

func TestV9GenerationCaseDeltaExcludesUnsettledTail(t *testing.T) {
	tests := []struct {
		name                string
		before, after       brokerCaseSnapshot
		beforeErr, afterErr error
		observed, complete  bool
	}{
		{name: "zero use", complete: true},
		{name: "one success", after: brokerCaseSnapshot{Requests: 1, Successes: 1}, observed: true, complete: true},
		{name: "failed request", after: brokerCaseSnapshot{Requests: 1}, complete: true},
		{name: "unfinished counted request", after: brokerCaseSnapshot{Requests: 1, InFlight: 1}, complete: true},
		{name: "admitted before request count", after: brokerCaseSnapshot{InFlight: 1}, complete: true},
		{name: "multiple unfinished handlers", after: brokerCaseSnapshot{Requests: 1, InFlight: 2}, complete: true},
		{name: "nonzero new-generation baseline", before: brokerCaseSnapshot{Requests: 1}, after: brokerCaseSnapshot{Requests: 1}},
		{name: "negative in-flight", after: brokerCaseSnapshot{InFlight: -1}},
		{name: "success exceeds requests", after: brokerCaseSnapshot{Successes: 1}},
		{name: "before unavailable", beforeErr: fmt.Errorf("missing")},
		{name: "after unavailable", afterErr: fmt.Errorf("missing")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			observed, complete := v9GenerationCaseDelta(tt.before, tt.after, tt.beforeErr, tt.afterErr)
			if observed != tt.observed || complete != tt.complete {
				t.Fatalf("got observed=%t complete=%t, want %t/%t", observed, complete, tt.observed, tt.complete)
			}
		})
	}
}

func TestV9DistinctModelCasesRequiresExactEligibleTranscriptPopulation(t *testing.T) {
	base := []protocol.CaseScore{{CaseID: "a"}, {CaseID: "b"}, {CaseID: "c", Undelivered: true}}
	tests := []struct {
		name       string
		perCase    []protocol.CaseScore
		transcript []transcriptCase
		complete   bool
		successful int
	}{
		{name: "complete", perCase: base, transcript: completeModelTranscripts(base, true, false, true), complete: true, successful: 1},
		{name: "missing transcript", perCase: base, transcript: completeModelTranscripts(base[:2], true, false)},
		{name: "wrong order", perCase: base, transcript: []transcriptCase{{CaseID: "b"}, {CaseID: "a"}, {CaseID: "c"}}},
		{name: "duplicate case", perCase: []protocol.CaseScore{{CaseID: "a"}, {CaseID: "a"}}, transcript: completeModelTranscripts([]protocol.CaseScore{{CaseID: "a"}, {CaseID: "a"}}, true, true)},
		{name: "empty case", perCase: []protocol.CaseScore{{CaseID: ""}}, transcript: completeModelTranscripts([]protocol.CaseScore{{CaseID: ""}}, true)},
		{name: "eligible attribution missing", perCase: base, transcript: []transcriptCase{{CaseID: "a"}, {CaseID: "b"}, {CaseID: "c"}}},
		{name: "excluded attribution missing", perCase: base, transcript: []transcriptCase{
			{CaseID: "a", Execution: runner.CaseExecution{ModelAttributionComplete: true}},
			{CaseID: "b", Execution: runner.CaseExecution{ModelAttributionComplete: true}},
			{CaseID: "c"},
		}, complete: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			complete, successful := v9DistinctModelCases(tt.perCase, tt.transcript)
			if complete != tt.complete || successful != tt.successful {
				t.Fatalf("got complete=%t successful=%d, want %t/%d", complete, successful, tt.complete, tt.successful)
			}
		})
	}
}
