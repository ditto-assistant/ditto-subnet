package longmemeval

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/refharness"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestExecutorStarterCompatibleEndToEnd(t *testing.T) {
	profile, raw, dataset := runtimeFixture(t)
	executor, harness, judge, meter := validExecutor(profile)
	result, err := executor.Execute(
		context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey,
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Evidence.Score.LongMemMean != 1 || result.Evidence.Score.LongMemStdErr != 0 || result.Evidence.Score.CaseCount != 12 {
		t.Fatalf("score=%#v", result.Evidence.Score)
	}
	if result.selection.CaseSetDigest != dataset.Selection.CaseSetDigest {
		t.Fatal("execution result lost its private selection commitment")
	}
	if err := result.Validate(profile); err != nil {
		t.Fatalf("executor evidence failed replay validation: %v", err)
	}
	if _, err := result.Digest(profile); err != nil {
		t.Fatalf("executor evidence was not signable: %v", err)
	}
	if harness.runs != 12 || len(judge.inputs) != 12 {
		t.Fatalf("runs=%d judges=%d", harness.runs, len(judge.inputs))
	}
	snapshot, err := meter.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, lane := range snapshot {
		if lane.Requests != 12 || lane.Successes != 12 || lane.ReceiptedRequests != 12 {
			t.Fatalf("lane %q accounting=%#v", lane.Lane, lane)
		}
	}
	for _, capability := range result.Evidence.Score.PerCapability {
		if capability.Count != 2 || capability.Correct != 2 || capability.Mean != 1 {
			t.Fatalf("capability score=%#v", capability)
		}
	}
}

func TestExecutionResultSerializationExposesEvidenceOnly(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, _, _, _ := validExecutor(profile)
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(encoded, []byte(`"evidence"`)) || bytes.Contains(encoded, []byte("question_id")) ||
		bytes.Contains(encoded, []byte("selection")) {
		t.Fatalf("execution result exposed private selection: %s", encoded)
	}
	for _, selected := range result.selection.Cases {
		if bytes.Contains(encoded, []byte(selected.QuestionID)) {
			t.Fatalf("execution result exposed question %q", selected.QuestionID)
		}
	}
}

func TestExecutionResultDigestRevalidatesEvidenceMutation(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, _, _, _ := validExecutor(profile)
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := result.Digest(profile); err != nil {
		t.Fatal(err)
	}
	result.Evidence.ProviderEvidence[0].FallbackUsed = true
	if err := result.Validate(profile); err == nil {
		t.Fatal("mutated execution result validated")
	}
	if _, err := result.Digest(profile); err == nil {
		t.Fatal("mutated execution result became signable")
	}
}

func TestExecutorReferenceHarnessHTTPSEndToEnd(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/seed":
			var seed protocol.SeedRequest
			if err := json.NewDecoder(request.Body).Decode(&seed); err != nil {
				writer.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(writer).Encode(protocol.SeedResponse{
				Pairs: len(seed.Pairs), Subjects: len(seed.Subjects), Links: len(seed.Links),
			})
		case "/run":
			var run protocol.RunRequest
			if err := json.NewDecoder(request.Body).Decode(&run); err != nil {
				writer.WriteHeader(http.StatusBadRequest)
				return
			}
			meter.add(ReaderLane, 10, 2, 5, true)
			_ = json.NewEncoder(writer).Encode(protocol.RunResponse{
				FinalText: "ok",
				ToolCalls: refharness.Route(run.UserInput, run.Tools),
			})
		default:
			writer.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	harness, err := NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err != nil {
		t.Fatal(err)
	}
	if result.Evidence.Score.CaseCount != 12 || result.Evidence.Score.LongMemMean != 0 {
		t.Fatalf("reference floor score=%#v", result.Evidence.Score)
	}
	if err := result.Validate(profile); err != nil {
		t.Fatal(err)
	}
}

func TestExecutorCrossCaseIsolation(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, harness, _, _ := validExecutor(profile)
	if _, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey); err != nil {
		t.Fatal(err)
	}
	harness.mu.Lock()
	defer harness.mu.Unlock()
	if len(harness.stores) != 12 {
		t.Fatalf("isolated stores=%d", len(harness.stores))
	}
	pairIDs := make(map[string]string)
	for userID, store := range harness.stores {
		if len(store) != 1 {
			t.Fatalf("user %q retained %d pairs", userID, len(store))
		}
		for pairID := range store {
			if previous, reused := pairIDs[pairID]; reused {
				t.Fatalf("pair %q shared by users %q and %q", pairID, previous, userID)
			}
			pairIDs[pairID] = userID
		}
	}
	for index, runUser := range harness.runUsers {
		if harness.seedUsers[index] != runUser {
			t.Fatalf("case %d seeded %q but ran %q", index, harness.seedUsers[index], runUser)
		}
	}
}

func TestExecutorJudgeBoundaryReceivesReferencesHarnessNeverDoes(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, harness, judge, _ := validExecutor(profile)
	if _, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey); err != nil {
		t.Fatal(err)
	}
	if len(judge.inputs) != 12 {
		t.Fatalf("judge inputs=%d", len(judge.inputs))
	}
	for _, input := range judge.inputs {
		if input.Reference.QuestionID == "" || input.Reference.QuestionType == "" ||
			input.Reference.Answer == "" || len(input.Reference.AnswerSessionIDs) == 0 {
			t.Fatalf("trusted judge lost official reference fields: %#v", input.Reference)
		}
		if input.Hypothesis != input.Reference.Answer {
			t.Fatalf("hypothesis=%q answer=%q", input.Hypothesis, input.Reference.Answer)
		}
	}
	harness.mu.Lock()
	defer harness.mu.Unlock()
	for userID, store := range harness.stores {
		if strings.Contains(userID, "private-") || strings.Contains(userID, "answer-proof") {
			t.Fatalf("private label in harness user id %q", userID)
		}
		for _, pair := range store {
			if strings.Contains(pair.PairID, "private-") || strings.Contains(pair.SessionID, "origin") {
				t.Fatalf("private label in harness store: %#v", pair)
			}
		}
	}
}

func TestExecutorRejectsTrustedJudgeFailureWithoutPartialEvidence(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, _, judge, _ := validExecutor(profile)
	judge.err = errFixture
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err == nil {
		t.Fatal("failed judge produced evidence")
	}
	if !reflect.DeepEqual(result, ExecutionResult{}) {
		t.Fatalf("failure returned partial evidence: %#v", result)
	}
}

func TestExecutorScoresOneReceivedUnjudgeableRunAsIncorrectAndContinues(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &oneCaseFailureHarness{Harness: base, failAt: 4, operation: "run", typed: true}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err != nil {
		t.Fatal(err)
	}
	if result.Evidence.Score.CaseCount != 12 || result.Evidence.Score.LongMemMean != 0.916667 {
		t.Fatalf("score=%#v", result.Evidence.Score)
	}
	if len(judge.inputs) != 11 || base.runs != 12 {
		t.Fatalf("runs/judges=%d/%d, want 12/11", base.runs, len(judge.inputs))
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encoded, []byte("private-failure")) || bytes.Contains(encoded, []byte("opaque case")) {
		t.Fatalf("private harness failure leaked into evidence: %s", encoded)
	}
	if err := result.Validate(profile); err != nil {
		t.Fatalf("case-local miss produced invalid evidence: %v", err)
	}
}

func TestExecutorRejectsUnjudgeableRunWithFailedProviderAttempt(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &providerFailedCaseHarness{Harness: base, meter: meter, failAt: 4}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err == nil || !strings.Contains(err.Error(), "lacks complete provider receipts") {
		t.Fatalf("failed provider attempt was scored as an incorrect case: result=%#v err=%v", result, err)
	}
	if !reflect.DeepEqual(result, ExecutionResult{}) || base.runs != 4 || len(judge.inputs) != 3 {
		t.Fatalf("provider failure returned partial evidence: result=%#v runs=%d judges=%d", result, base.runs, len(judge.inputs))
	}
}

func TestExecutorRejectsUnjudgeableRunWithoutCaseLocalProviderReceipt(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &noProviderAttemptCaseHarness{Harness: base, failAt: 4}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err == nil || !strings.Contains(err.Error(), "lacks complete provider receipts") {
		t.Fatalf("zero-delta harness failure was scored as an incorrect case: result=%#v err=%v", result, err)
	}
	if !reflect.DeepEqual(result, ExecutionResult{}) || base.runs != 3 || len(judge.inputs) != 3 {
		t.Fatalf("earlier successful cases masked zero-delta failure: result=%#v runs=%d judges=%d", result, base.runs, len(judge.inputs))
	}
}

func TestExecutorKeepsAllUnjudgeableRunsFailClosedAtEvidenceBoundary(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &allRunFailureHarness{Harness: base}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err == nil || !strings.Contains(err.Error(), `lane "judge"`) {
		t.Fatalf("zero-judge provider evidence was accepted: result=%#v err=%v", result, err)
	}
	if !reflect.DeepEqual(result, ExecutionResult{}) || base.runs != 12 || len(judge.inputs) != 0 {
		t.Fatalf("all-unjudgeable boundary returned partial evidence: result=%#v runs=%d judges=%d", result, base.runs, len(judge.inputs))
	}
}

func TestExecutorKeepsSeedAndNonResponseRunFailuresFailClosed(t *testing.T) {
	for _, testCase := range []struct {
		name      string
		operation string
		typed     bool
	}{
		{name: "received seed failure", operation: "seed", typed: true},
		{name: "transport run failure", operation: "run", typed: false},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			profile, raw, _ := runtimeFixture(t)
			meter := newRecordingMeter(profile)
			base := newStarterHarness(meter)
			harness := &oneCaseFailureHarness{Harness: base, failAt: 1, operation: testCase.operation, typed: testCase.typed}
			executor := Executor{Harness: harness, Judge: &exactJudge{meter: meter}, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
			result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
			if err == nil || !reflect.DeepEqual(result, ExecutionResult{}) {
				t.Fatalf("failure returned result=%#v err=%v", result, err)
			}
		})
	}
}

func TestExecutorRejectsIncompleteSeedAcknowledgementFromAnyHarnessAdapter(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &seedResponseHarness{Harness: base}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	_, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "incomplete seed payload")
	if base.runs != 0 || len(judge.inputs) != 0 {
		t.Fatal("executor continued after incomplete seed acknowledgement")
	}
}

func TestExecutorEnforcesWallClockBudget(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, harness, _, _ := validExecutor(profile)
	harness.blockRun = make(chan struct{})
	executor.Limits.MaxElapsed = 20 * time.Millisecond
	started := time.Now()
	_, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err == nil || !strings.Contains(err.Error(), context.DeadlineExceeded.Error()) {
		t.Fatalf("deadline error=%v", err)
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("bounded executor returned after %s", elapsed)
	}
}

func TestExecutorHonorsEarlierCallerDeadline(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, harness, _, _ := validExecutor(profile)
	harness.blockRun = make(chan struct{})
	executor.Limits.MaxElapsed = time.Hour
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := executor.Execute(ctx, bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey); err == nil {
		t.Fatal("caller deadline ignored")
	}
}

func TestExecutorRefusesNonZeroInitialProviderSession(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, _, _, meter := validExecutor(profile)
	meter.add(ReaderLane, 1, 1, 1, true)
	_, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "did not start at zero")
}

func TestExecutorRejectsProviderDriftFallbackAndMalformedReceiptsImmediately(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	tests := map[string]func(*recordingMeter){
		"fallback": func(m *recordingMeter) { m.mutate(ReaderLane, func(v *ProviderEvidence) { v.FallbackUsed = true }) },
		"provider": func(m *recordingMeter) { m.mutate(ReaderLane, func(v *ProviderEvidence) { v.Provider += "-fallback" }) },
		"model":    func(m *recordingMeter) { m.mutate(ReaderLane, func(v *ProviderEvidence) { v.Model += "-fallback" }) },
		"profile": func(m *recordingMeter) {
			m.mutate(ReaderLane, func(v *ProviderEvidence) { v.ProfileRevision += "-fallback" })
		},
		"missing receipt": func(m *recordingMeter) { m.mutate(ReaderLane, func(v *ProviderEvidence) { v.ReceiptedRequests-- }) },
		"estimated cost": func(m *recordingMeter) {
			m.mutate(ReaderLane, func(v *ProviderEvidence) { v.CostSource = "list_price" })
		},
		"wrong currency": func(m *recordingMeter) { m.mutate(ReaderLane, func(v *ProviderEvidence) { v.Currency = "EUR" }) },
		"bad receipt digest": func(m *recordingMeter) {
			m.mutate(ReaderLane, func(v *ProviderEvidence) { v.ReceiptSetSHA256 = "bad" })
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			meter := newRecordingMeter(profile)
			base := newStarterHarness(meter)
			harness := &hookHarness{Harness: base, afterRun: func() { mutate(meter) }}
			judge := &exactJudge{meter: meter}
			executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
			if _, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey); err == nil {
				t.Fatal("provider accounting violation accepted")
			}
			if len(judge.inputs) != 0 {
				t.Fatal("judge ran after reader accounting violation")
			}
		})
	}
}

func TestExecutorRejectsEveryRuntimeBudgetOverrun(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	tests := map[string]func(*Profile){
		"reader requests":   func(p *Profile) { providerPolicy(p, ReaderLane).MaxRequests = 1 },
		"reader prompt":     func(p *Profile) { providerPolicy(p, ReaderLane).MaxPromptTokens = 9 },
		"reader completion": func(p *Profile) { providerPolicy(p, ReaderLane).MaxCompletionTokens = 1 },
		"reader total": func(p *Profile) {
			policy := providerPolicy(p, ReaderLane)
			policy.MaxPromptTokens = 11
			policy.MaxCompletionTokens = 11
			policy.MaxTotalTokens = 11
		},
		"reader cost":      func(p *Profile) { providerPolicy(p, ReaderLane).MaxCostUSDmicros = 4 },
		"judge requests":   func(p *Profile) { providerPolicy(p, JudgeLane).MaxRequests = 1 },
		"judge prompt":     func(p *Profile) { providerPolicy(p, JudgeLane).MaxPromptTokens = 2 },
		"judge completion": func(p *Profile) { providerPolicy(p, JudgeLane).MaxCompletionTokens = 1 },
		"judge total": func(p *Profile) {
			policy := providerPolicy(p, JudgeLane)
			policy.MaxPromptTokens = 4
			policy.MaxCompletionTokens = 4
			policy.MaxTotalTokens = 4
		},
		"judge cost": func(p *Profile) { providerPolicy(p, JudgeLane).MaxCostUSDmicros = 6 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := profile
			changed.Providers = append([]ProviderPolicy(nil), profile.Providers...)
			mutate(&changed)
			executor, harness, judge, _ := validExecutor(changed)
			if _, err := executor.Execute(context.Background(), bytes.NewReader(raw), changed, artifactDigestA, fixtureProjectionKey); err == nil {
				t.Fatal("budget overrun accepted")
			}
			if strings.HasPrefix(name, "reader") && harness.runs > 1 {
				t.Fatalf("reader overrun allowed %d runs", harness.runs)
			}
			if strings.HasPrefix(name, "reader") && len(judge.inputs) > 1 {
				t.Fatalf("reader overrun allowed %d judges", len(judge.inputs))
			}
		})
	}
}

func TestExecutorStopsBeforeCallingLaneWithExhaustedRequestBudget(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	profile.Providers = append([]ProviderPolicy(nil), profile.Providers...)
	providerPolicy(&profile, ReaderLane).MaxRequests = 1
	executor, harness, judge, _ := validExecutor(profile)
	_, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "no request budget remaining")
	if harness.runs != 1 || len(judge.inputs) != 1 {
		t.Fatalf("runs=%d judges=%d after request cap", harness.runs, len(judge.inputs))
	}
}

func TestExecutorRejectsUnusedProviderLaneAtFinalization(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	harness := newStarterHarness(nil)
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	_, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	requireErrorContains(t, err, "final provider evidence")
}

func TestExecutorIgnoresHarnessSelfReportedUsage(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	meter := newRecordingMeter(profile)
	base := newStarterHarness(meter)
	harness := &hookHarness{Harness: base, mutateRun: func(response *protocol.RunResponse) {
		response.PromptTokens = math.MaxInt64
		response.OutputTokens = math.MaxInt64
	}}
	judge := &exactJudge{meter: meter}
	executor := Executor{Harness: harness, Judge: judge, Meter: meter, Limits: ExecutionLimits{MaxElapsed: time.Second, SeedBatchPairs: 64}}
	result, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey)
	if err != nil {
		t.Fatal(err)
	}
	for _, provider := range result.Evidence.ProviderEvidence {
		if provider.PromptTokens >= uint64(math.MaxInt64) {
			t.Fatal("untrusted harness token claim entered evidence")
		}
	}
}

func TestExecutorRejectsBadConfigurationAndMeterFailure(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	executor, _, _, meter := validExecutor(profile)
	for name, mutate := range map[string]func(*Executor, *[]byte){
		"nil harness": func(e *Executor, _ *[]byte) { e.Harness = nil },
		"nil judge":   func(e *Executor, _ *[]byte) { e.Judge = nil },
		"nil meter":   func(e *Executor, _ *[]byte) { e.Meter = nil },
		"zero time":   func(e *Executor, _ *[]byte) { e.Limits.MaxElapsed = 0 },
		"zero batch":  func(e *Executor, _ *[]byte) { e.Limits.SeedBatchPairs = 0 },
		"weak key":    func(_ *Executor, key *[]byte) { *key = []byte("weak") },
	} {
		t.Run(name, func(t *testing.T) {
			changed := executor
			key := append([]byte(nil), fixtureProjectionKey...)
			mutate(&changed, &key)
			if _, err := changed.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, key); err == nil {
				t.Fatal("invalid executor configuration accepted")
			}
		})
	}
	meter.snapshotErr = errors.New("meter unavailable")
	if _, err := executor.Execute(context.Background(), bytes.NewReader(raw), profile, artifactDigestA, fixtureProjectionKey); err == nil {
		t.Fatal("meter failure accepted")
	}
}

type hookHarness struct {
	Harness
	afterRun  func()
	mutateRun func(*protocol.RunResponse)
}

type oneCaseFailureHarness struct {
	Harness
	failAt    int
	operation string
	typed     bool
	seedCalls int
	runCalls  int
}

type allRunFailureHarness struct{ Harness }

func (h *allRunFailureHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	if _, err := h.Harness.Run(ctx, request); err != nil {
		return protocol.RunResponse{}, err
	}
	return protocol.RunResponse{}, &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusServiceUnavailable}
}

type providerFailedCaseHarness struct {
	Harness
	meter    *recordingMeter
	failAt   int
	runCalls int
}

type noProviderAttemptCaseHarness struct {
	Harness
	failAt   int
	runCalls int
}

func (h *noProviderAttemptCaseHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	h.runCalls++
	if h.runCalls == h.failAt {
		return protocol.RunResponse{}, &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusServiceUnavailable}
	}
	return h.Harness.Run(ctx, request)
}

func (h *providerFailedCaseHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	h.runCalls++
	response, err := h.Harness.Run(ctx, request)
	if err == nil && h.runCalls == h.failAt {
		h.meter.add(ReaderLane, 1, 0, 1, false)
		return protocol.RunResponse{}, &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusServiceUnavailable}
	}
	return response, err
}

func (h *oneCaseFailureHarness) Seed(ctx context.Context, request protocol.SeedRequest) (protocol.SeedResponse, error) {
	h.seedCalls++
	response, err := h.Harness.Seed(ctx, request)
	if err == nil && h.operation == "seed" && h.seedCalls == h.failAt {
		if h.typed {
			return protocol.SeedResponse{}, &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusInternalServerError}
		}
		return protocol.SeedResponse{}, errors.New("private-failure seed detail")
	}
	return response, err
}

func (h *oneCaseFailureHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	h.runCalls++
	response, err := h.Harness.Run(ctx, request)
	if err == nil && h.operation == "run" && h.runCalls == h.failAt {
		if h.typed {
			return protocol.RunResponse{}, &HarnessCaseFailure{Kind: "http_status", StatusCode: http.StatusInternalServerError}
		}
		return protocol.RunResponse{}, errors.New("private-failure run detail")
	}
	return response, err
}

type seedResponseHarness struct{ Harness }

func (h *seedResponseHarness) Seed(ctx context.Context, request protocol.SeedRequest) (protocol.SeedResponse, error) {
	_, err := h.Harness.Seed(ctx, request)
	return protocol.SeedResponse{}, err
}

func (h *hookHarness) Run(ctx context.Context, request protocol.RunRequest) (protocol.RunResponse, error) {
	response, err := h.Harness.Run(ctx, request)
	if h.afterRun != nil {
		h.afterRun()
	}
	if h.mutateRun != nil {
		h.mutateRun(&response)
	}
	return response, err
}

func providerPolicy(profile *Profile, lane string) *ProviderPolicy {
	for index := range profile.Providers {
		if profile.Providers[index].Lane == lane {
			return &profile.Providers[index]
		}
	}
	panic("missing provider policy " + lane)
}
