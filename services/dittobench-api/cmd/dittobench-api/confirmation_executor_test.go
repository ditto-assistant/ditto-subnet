package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testLaunchManifestSHA256 = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

var (
	testAblationSelectionKey  = bytes.Repeat([]byte{0x41}, 32)
	testAblationProjectionKey = bytes.Repeat([]byte{0x42}, 32)
)

type confirmationRuntimeFactoryFunc func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error)

func (confirmationRuntimeFactoryFunc) ValidateInstallation(confirmationExecutionProfileWire) error {
	return nil
}

func (function confirmationRuntimeFactoryFunc) Acquire(
	ctx context.Context,
	identity confirmationRuntimeIdentity,
) (*confirmationRuntime, error) {
	return function(ctx, identity)
}

type rejectedConfirmationRuntimeFactory struct{ err error }

func (factory rejectedConfirmationRuntimeFactory) ValidateInstallation(confirmationExecutionProfileWire) error {
	return factory.err
}

func (rejectedConfirmationRuntimeFactory) Acquire(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
	panic("unvalidated runtime factory must never acquire")
}

type readSeekCloser struct{ *bytes.Reader }

func (readSeekCloser) Close() error { return nil }

type inertLongMemHarness struct{}

func (inertLongMemHarness) Seed(context.Context, protocol.SeedRequest) (protocol.SeedResponse, error) {
	return protocol.SeedResponse{}, errors.New("not invoked by orchestrator seam test")
}

func (inertLongMemHarness) Run(context.Context, protocol.RunRequest) (protocol.RunResponse, error) {
	return protocol.RunResponse{}, errors.New("not invoked by orchestrator seam test")
}

type inertLongMemJudge struct{}

func (inertLongMemJudge) Judge(context.Context, longmemeval.JudgeInput) (bool, error) {
	return false, errors.New("not invoked by orchestrator seam test")
}

type inertLongMemMeter struct{}

func (inertLongMemMeter) Snapshot(context.Context) ([]longmemeval.ProviderEvidence, error) {
	return nil, errors.New("not invoked by orchestrator seam test")
}

type inertAblationRunner struct{}

func (inertAblationRunner) RunCase(context.Context, ablation.RunRequest) (ablation.CaseRunResult, error) {
	return ablation.CaseRunResult{}, errors.New("not invoked by orchestrator seam test")
}

type noReaderLongMemHarness struct{ runs int }

func (noReaderLongMemHarness) Seed(_ context.Context, request protocol.SeedRequest) (protocol.SeedResponse, error) {
	return protocol.SeedResponse{
		Pairs: len(request.Pairs), Subjects: len(request.Subjects), Links: len(request.Links),
	}, nil
}

func (h *noReaderLongMemHarness) Run(context.Context, protocol.RunRequest) (protocol.RunResponse, error) {
	h.runs++
	return protocol.RunResponse{FinalText: "canned-answer"}, nil
}

type judgeOnlyLongMemMeter struct {
	rows map[string]longmemeval.ProviderEvidence
}

func newJudgeOnlyLongMemMeter(profile longmemeval.Profile) *judgeOnlyLongMemMeter {
	rows := make(map[string]longmemeval.ProviderEvidence, len(profile.Providers))
	for _, policy := range profile.Providers {
		rows[policy.Lane] = longmemeval.ProviderEvidence{
			Lane: policy.Lane, CostSource: longmemeval.AuthoritativeCostV1, Currency: "USD",
			Provider: policy.Provider, ProfileRevision: policy.ProfileRevision, Model: policy.Model,
		}
	}
	return &judgeOnlyLongMemMeter{rows: rows}
}

func (m *judgeOnlyLongMemMeter) Snapshot(context.Context) ([]longmemeval.ProviderEvidence, error) {
	rows := make([]longmemeval.ProviderEvidence, 0, len(m.rows))
	for _, row := range m.rows {
		rows = append(rows, row)
	}
	return rows, nil
}

func (m *judgeOnlyLongMemMeter) addJudge() {
	row := m.rows[longmemeval.JudgeLane]
	row.Requests++
	row.Successes++
	row.ReceiptedRequests++
	row.PromptTokens++
	row.CompletionTokens++
	row.TotalTokens += 2
	row.CostUSDmicros++
	row.ReceiptSetSHA256 = digestBytes([]byte(fmt.Sprintf("judge-receipts-%d", row.Requests)))
	m.rows[longmemeval.JudgeLane] = row
}

type alwaysCorrectLongMemJudge struct{ meter *judgeOnlyLongMemMeter }

func (j alwaysCorrectLongMemJudge) Judge(context.Context, longmemeval.JudgeInput) (bool, error) {
	j.meter.addJudge()
	return true, nil
}

type successfulAblationRunner struct{ calls int }

func (r *successfulAblationRunner) RunCase(_ context.Context, request ablation.RunRequest) (ablation.CaseRunResult, error) {
	r.calls++
	switch request.Lane {
	case ablation.LaneOrdinary:
		if request.Responder != nil {
			return ablation.CaseRunResult{}, errors.New("ordinary lane received a responder")
		}
		return ablation.CaseRunResult{Score: 0.9}, nil
	case ablation.LaneInference:
		if request.Responder == nil {
			return ablation.CaseRunResult{}, errors.New("inference lane lacks responder")
		}
		if _, err := request.Responder.Chat("locked-model", 1); err != nil {
			return ablation.CaseRunResult{}, err
		}
		if _, err := request.Responder.Chat("locked-model", 1); err != nil {
			return ablation.CaseRunResult{}, err
		}
		return ablation.CaseRunResult{Score: 0.1}, nil
	case ablation.LaneEmbedding:
		if request.Responder == nil {
			return ablation.CaseRunResult{}, errors.New("embedding lane lacks responder")
		}
		if _, err := request.Responder.Embeddings([]string{"first"}); err != nil {
			return ablation.CaseRunResult{}, err
		}
		if _, err := request.Responder.Embeddings([]string{"second"}); err != nil {
			return ablation.CaseRunResult{}, err
		}
		return ablation.CaseRunResult{Score: 0.1}, nil
	default:
		return ablation.CaseRunResult{}, errors.New("unexpected ablation lane")
	}
}

func validInstalledConfirmationProfile(t *testing.T) (confirmationExecutionProfileWire, json.RawMessage) {
	t.Helper()
	profile := confirmationExecutionProfileWire{
		SchemaVersion:              confirmationProfileSchemaVersion,
		Revision:                   "v9-launch-calibrated-2026-08-08",
		LongMemProfileRevision:     "longmemeval-launch-v1",
		LongMemDatasetRevision:     longmemeval.CleanedDatasetRevision,
		LongMemDatasetSHA256:       longmemeval.CleanedDatasetSHA256,
		LongMemProjectionKeySHA256: digestBytes(bytes.Repeat([]byte{0x43}, 32)),
		LongMemSelectorRevision:    longmemeval.SelectorRevisionV1,
		LongMemSelectionSeed:       17, LongMemCasesPerCapability: 2, LongMemSeedBatchPairs: 2,
		ProviderLanes: []confirmationProviderLaneProfile{
			{
				Lane: longmemeval.JudgeLane, Provider: "pinned-provider", RouteProvider: "openai", ReceiptProvider: "OpenAI", ProfileRevision: "provider-launch-v1",
				Model: "pinned-model", MaxRequests: 10, MaxPromptTokens: 100,
				MaxCompletionTokens: 100, MaxTotalTokens: 200, MaxCostUSDmicros: 10_000,
			},
			{
				Lane: longmemeval.ReaderLane, Provider: "pinned-provider", RouteProvider: "openai", ReceiptProvider: "OpenAI", ProfileRevision: "provider-launch-v1",
				Model: "pinned-model", MaxRequests: 10, MaxPromptTokens: 100,
				MaxCompletionTokens: 100, MaxTotalTokens: 200, MaxCostUSDmicros: 10_000,
			},
		},
		EmbeddingLane: confirmationEmbeddingLaneProfile{
			Lane: "embedding", Provider: "pinned-provider", ProfileRevision: "embedding-launch-v1",
			Model: "pinned-embedding-model", Dimensions: 768, MaxRequests: 1000,
			MaxInputTokens: 1_000_000, MaxCostUSDmicros: 100_000,
		},
		AblationProfileRevision:         "ablation-launch-v1",
		AblationDatasetSHA256:           strings.Repeat("a", 64),
		AblationThresholdManifestSHA256: strings.Repeat("b", 64),
		AblationSelectionKeySHA256:      digestBytes(testAblationSelectionKey),
		AblationProjectionKeySHA256:     digestBytes(testAblationProjectionKey),
		AblationCoordinatorPolicy: ablation.CoordinatorPolicy{
			SampleSize: 2, MaxAttempts: 1, MaxRequests: 6,
			RequestTimeoutMilliseconds: 50, TotalTimeoutMilliseconds: 100,
		},
		InferenceAblation: confirmationAblationProfile{
			Intervention: string(ablation.InterventionInference), ContractVersion: ablation.ProfileContractVersion,
			ThresholdMicros: 200_000,
			Budget:          confirmationAblationBudget{MaxChatRequests: 10, MaxChatInputBytes: 10_000},
		},
		EmbeddingAblation: confirmationAblationProfile{
			Intervention: string(ablation.InterventionEmbedding), ContractVersion: ablation.ProfileContractVersion,
			ThresholdMicros: 200_000,
			Budget: confirmationAblationBudget{
				MaxEmbeddingRequests: 10, MaxEmbeddingInputs: 10, MaxEmbeddingInputBytes: 10_000,
			},
		},
		Composite: confirmationCompositeProfile{
			SchemaVersion: 1, Revision: "composite-launch-v1", FormulaRevision: "weighted-quality-gates-v1",
			BaseWeightBPS: 7_000, LongMemWeightBPS: 3_000,
		},
	}
	refreshConfirmationProfileChecksums(t, &profile)
	if profile.Checksum != "3de4aed6dd7011e86dfb28cec11c043f2b9a0328a498613c94e04524e563f75a" {
		t.Fatalf("cross-language outer profile checksum = %s", profile.Checksum)
	}
	raw, err := json.Marshal(profile)
	if err != nil {
		t.Fatal(err)
	}
	return profile, raw
}

func TestConfirmationProfileV9DefaultsBenchVersion(t *testing.T) {
	profile, _ := validInstalledConfirmationProfile(t)
	if profile.BenchVersion != 0 {
		t.Fatalf("v9 profile must omit the bench_version field, got %d", profile.BenchVersion)
	}
	if got := profile.benchVersion(); got != confirmationBenchVersion {
		t.Fatalf("v9 profile benchVersion() = %d, want %d", got, confirmationBenchVersion)
	}
	if got := profile.longMemProfile().BenchVersion; got != 9 {
		t.Fatalf("v9 longmem profile bench version = %d, want 9", got)
	}
	if got := profile.ablationProfile().BenchVersion; got != 9 {
		t.Fatalf("v9 ablation profile bench version = %d, want 9", got)
	}
	if err := profile.validate(); err != nil {
		t.Fatalf("v9 profile must validate: %v", err)
	}
}

func TestConfirmationProfileV12BindsBenchVersionAndDeepHistoryFloors(t *testing.T) {
	profile, _ := validInstalledConfirmationProfile(t)
	// Promote the v9 launch profile to a v12 confirmation profile: declare the
	// version and the bench v10+ deep-history floors the longmem contract
	// requires. The dataset stays the official cleaned condition; the selection
	// floors carve out the deep-history subset.
	profile.BenchVersion = 12
	profile.LongMemCasesPerCapability = longmemeval.V10MinCasesPerCapability
	profile.LongMemMinHistorySessions = longmemeval.V10MinHistorySessions
	profile.LongMemMinHistoryBytes = longmemeval.V10MinHistoryBytes
	refreshConfirmationProfileChecksums(t, &profile)

	if got := profile.benchVersion(); got != 12 {
		t.Fatalf("v12 profile benchVersion() = %d, want 12", got)
	}
	if err := profile.validate(); err != nil {
		t.Fatalf("v12 confirmation profile must validate: %v", err)
	}
	longMem := profile.longMemProfile()
	if longMem.BenchVersion != 12 || longMem.MinHistorySessions != longmemeval.V10MinHistorySessions ||
		longMem.MinHistoryBytes != longmemeval.V10MinHistoryBytes {
		t.Fatalf("v12 longmem profile did not carry the threaded version and floors: %+v", longMem)
	}
	if err := longMem.Validate(); err != nil {
		t.Fatalf("v12 longmem profile must satisfy the deep-history floors: %v", err)
	}
	if got := profile.ablationProfile().BenchVersion; got != 12 {
		t.Fatalf("v12 ablation profile bench version = %d, want 12", got)
	}
	if !longmemeval.IsOfficialCleanedDataset(longMem) {
		t.Fatal("v12 longmem profile must remain the official cleaned dataset condition")
	}
}

func TestConfirmationExecutionProfileCrossLanguageFixture(t *testing.T) {
	raw, err := os.ReadFile("testdata/confirmation_execution_profile_v9.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		FixtureSchema    string                              `json:"fixture_schema"`
		ExpectedChecksum string                              `json:"expected_checksum"`
		Profile          confirmationExecutionProfilePayload `json:"profile"`
	}
	if err := decodeStrictConfirmationJSON(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(fixture.Profile)
	if err != nil {
		t.Fatal(err)
	}
	if fixture.FixtureSchema != "dittobench-v9-confirmation-execution-profile-v1" ||
		digestBytes(payload) != fixture.ExpectedChecksum ||
		fixture.ExpectedChecksum != "38d9334acd0f17e257d151d66e3feb6d5c0a200f0c5091f8bd29f90e387fd3f1" {
		t.Fatalf("cross-language execution profile fixture drift: schema=%s digest=%s expected=%s",
			fixture.FixtureSchema, digestBytes(payload), fixture.ExpectedChecksum)
	}
}

func refreshConfirmationProfileChecksums(t *testing.T, profile *confirmationExecutionProfileWire) {
	t.Helper()
	longMemChecksum, err := profile.longMemProfile().Checksum()
	if err != nil {
		t.Fatal(err)
	}
	profile.LongMemProfileChecksum = longMemChecksum
	ablationChecksum, err := ablation.FrozenProfileSHA256(profile.ablationProfile())
	if err != nil {
		t.Fatal(err)
	}
	profile.AblationProfileChecksum = ablationChecksum
	compositePayload := struct {
		SchemaVersion    int    `json:"schema_version"`
		Revision         string `json:"revision"`
		FormulaRevision  string `json:"formula_revision"`
		BaseWeightBPS    uint64 `json:"base_weight_bps"`
		LongMemWeightBPS uint64 `json:"longmem_weight_bps"`
	}{
		profile.Composite.SchemaVersion, profile.Composite.Revision, profile.Composite.FormulaRevision,
		profile.Composite.BaseWeightBPS, profile.Composite.LongMemWeightBPS,
	}
	raw, err := json.Marshal(compositePayload)
	if err != nil {
		t.Fatal(err)
	}
	profile.Composite.Checksum = digestBytes(raw)
	raw, err = json.Marshal(profile.payload())
	if err != nil {
		t.Fatal(err)
	}
	profile.Checksum = digestBytes(raw)
}

func validConfirmationRuntime() *confirmationRuntime {
	return &confirmationRuntime{
		LongMemSource:        readSeekCloser{bytes.NewReader([]byte("not-consumed"))},
		LongMemHarness:       inertLongMemHarness{},
		LongMemJudge:         inertLongMemJudge{},
		LongMemMeter:         inertLongMemMeter{},
		LongMemProjectionKey: bytes.Repeat([]byte{0x43}, 32),
		AblationPopulation: ablation.EligiblePopulation{
			BenchVersion: 9, Confirmation: true,
			Cases: []ablation.EligibleCase{{CaseID: "case-a", UserID: "user-a"}, {CaseID: "case-b", UserID: "user-b"}},
		},
		AblationCaseRunner:    inertAblationRunner{},
		AblationSelectionKey:  append([]byte(nil), testAblationSelectionKey...),
		AblationProjectionKey: append([]byte(nil), testAblationProjectionKey...),
		Close:                 func() error { return nil },
	}
}

func validTrustedConfirmationRequest(t *testing.T, profileRaw json.RawMessage, profile confirmationExecutionProfileWire) confirmationExecutionRequest {
	t.Helper()
	request := validConfirmationRequest()
	request.AgentID = "agent-00000001"
	request.SlotID = "longmem-3"
	request.Mode = "shadow"
	request.ProfileRevision = profile.Revision
	request.ProfileChecksum = profile.Checksum
	request.ExecutionProfile = append(json.RawMessage(nil), profileRaw...)
	request.PerBundleRequestCap = 1_020
	request.PerBundleTokenCap = 1_000_400
	return request
}

func installTrustedExecutor(
	t *testing.T,
	raw json.RawMessage,
	factory confirmationRuntimeFactory,
) *trustedConfirmationExecutor {
	t.Helper()
	executor, err := newTrustedConfirmationExecutor(confirmationProfileInstallation{
		ExecutionProfile: raw, LaunchManifestSHA256: testLaunchManifestSHA256,
	}, factory)
	if err != nil {
		t.Fatal(err)
	}
	return executor
}

func TestTrustedConfirmationConstructorFailsClosed(t *testing.T) {
	t.Parallel()
	_, raw := validInstalledConfirmationProfile(t)
	validFactory := confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		return validConfirmationRuntime(), nil
	})
	tests := []struct {
		name         string
		installation confirmationProfileInstallation
		factory      confirmationRuntimeFactory
		want         string
	}{
		{"nil runtime factory", confirmationProfileInstallation{ExecutionProfile: raw, LaunchManifestSHA256: testLaunchManifestSHA256}, nil, "runtime factory"},
		{"missing launch approval", confirmationProfileInstallation{ExecutionProfile: raw}, validFactory, "exact launch approval"},
		{"invalid launch approval", confirmationProfileInstallation{ExecutionProfile: raw, LaunchManifestSHA256: "not-a-digest"}, validFactory, "exact launch approval"},
		{"missing profile", confirmationProfileInstallation{LaunchManifestSHA256: testLaunchManifestSHA256}, validFactory, "decode confirmation"},
		{"unvalidated runtime dependencies", confirmationProfileInstallation{ExecutionProfile: raw, LaunchManifestSHA256: testLaunchManifestSHA256}, rejectedConfirmationRuntimeFactory{err: errors.New("judge is nil")}, "installation is not ready"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := newTrustedConfirmationExecutor(test.installation, test.factory)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestConfirmationExecutionFailurePreservesFirstReviewedStageAndCause(t *testing.T) {
	t.Parallel()
	cause := errors.New("private provider response")
	inner := wrapConfirmationExecutionFailure("provider_session", cause)
	outer := wrapConfirmationExecutionFailure("runtime_acquire", inner)
	if outer != inner {
		t.Fatal("outer stage replaced the more specific inner stage")
	}
	if got := confirmationExecutionStage(outer); got != "provider_session" {
		t.Fatalf("stage = %q, want provider_session", got)
	}
	if !errors.Is(outer, cause) {
		t.Fatal("wrapped failure no longer exposes its cause to local cleanup/tests")
	}
	if strings.Contains(outer.Error(), cause.Error()) {
		t.Fatalf("public error leaked cause: %q", outer.Error())
	}
}

func TestConfirmationExecutionDiagnosticDoesNotDeriveFromPrivateErrorText(t *testing.T) {
	err := wrapConfirmationExecutionFailure(
		"dimension_execution",
		errors.New("private submitted response credential-material"),
	)
	class, status := confirmationExecutionDiagnostic(err)
	if class != "unclassified" || status != 0 {
		t.Fatalf("diagnostic = %q, %d", class, status)
	}
}

func TestConfirmationExecutionDiagnosticPreservesOnlySafeHarnessClass(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		writer.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = writer.Write([]byte(`{"error":"private submitted response credential-material"}`))
	}))
	defer server.Close()
	harness, err := longmemeval.NewHTTPHarness(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	_, err = harness.Seed(context.Background(), protocol.SeedRequest{
		UserID: "opaque-user", Pairs: []protocol.MemoryPair{},
		Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
	})
	err = wrapConfirmationExecutionFailure("dimension_execution", err)
	class, status := confirmationExecutionDiagnostic(err)
	if class != "longmem_seed_http_status" || status != http.StatusUnprocessableEntity {
		t.Fatalf("diagnostic = %q, %d", class, status)
	}
	if strings.Contains(err.Error(), "credential-material") {
		t.Fatalf("private submitted response leaked through error: %v", err)
	}
}

func TestTrustedConfirmationProfileStrictAndChecksumBound(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	factory := confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		return validConfirmationRuntime(), nil
	})
	tests := []struct {
		name   string
		raw    func() json.RawMessage
		needle string
	}{
		{
			name: "unknown field",
			raw: func() json.RawMessage {
				return bytes.Replace(raw, []byte(`"schema_version":1`), []byte(`"schema_version":1,"unknown":true`), 1)
			},
			needle: "unknown field",
		},
		{
			name: "duplicate field",
			raw: func() json.RawMessage {
				return bytes.Replace(raw, []byte(`"schema_version":1`), []byte(`"schema_version":1,"schema_version":1`), 1)
			},
			needle: "duplicate field",
		},
		{
			name:   "trailing value",
			raw:    func() json.RawMessage { return append(append(json.RawMessage(nil), raw...), []byte(` {}`)...) },
			needle: "trailing",
		},
		{
			name: "outer checksum",
			raw: func() json.RawMessage {
				changed := profile
				changed.Revision = "other-launch"
				value, _ := json.Marshal(changed)
				return value
			},
			needle: "outer profile checksum",
		},
		{
			name: "official dataset",
			raw: func() json.RawMessage {
				changed := profile
				changed.LongMemDatasetRevision = "fixture-only"
				refreshConfirmationProfileChecksums(t, &changed)
				value, _ := json.Marshal(changed)
				return value
			},
			needle: "pinned official cleaned dataset",
		},
		{
			name: "longmem projection key checksum",
			raw: func() json.RawMessage {
				changed := profile
				changed.LongMemProjectionKeySHA256 = "invalid"
				payload, _ := json.Marshal(changed.payload())
				changed.Checksum = digestBytes(payload)
				value, _ := json.Marshal(changed)
				return value
			},
			needle: "derivation salts",
		},
		{
			name: "wrong provider lane set",
			raw: func() json.RawMessage {
				changed := profile
				changed.ProviderLanes = append([]confirmationProviderLaneProfile(nil), profile.ProviderLanes...)
				changed.ProviderLanes[0].Lane = "grader"
				refreshConfirmationProfileChecksums(t, &changed)
				value, _ := json.Marshal(changed)
				return value
			},
			needle: "reader and judge",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := newTrustedConfirmationExecutor(confirmationProfileInstallation{
				ExecutionProfile: test.raw(), LaunchManifestSHA256: testLaunchManifestSHA256,
			}, factory)
			if err == nil || !strings.Contains(err.Error(), test.needle) {
				t.Fatalf("error = %v, want substring %q", err, test.needle)
			}
		})
	}
}

func TestTrustedConfirmationReadinessPublishesOnlyValidatedInstallation(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	executor := installTrustedExecutor(t, raw, confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		return validConfirmationRuntime(), nil
	}))
	want := confirmationReadiness{Ready: true, ProfileRevision: profile.Revision, ProfileChecksum: profile.Checksum}
	if got := executor.Readiness(); got != want {
		t.Fatalf("readiness = %+v, want %+v", got, want)
	}
	executor.runtimeFactory = nil
	if got := executor.Readiness(); got.Ready || got.ProfileRevision != "" || got.ProfileChecksum != "" {
		t.Fatalf("broken executor leaked ready profile: %+v", got)
	}
}

func TestConfirmationSubjectEpochAllowListTracksEvidenceStack(t *testing.T) {
	t.Parallel()
	for version, want := range map[int]bool{
		8: false, 9: true, 10: true, 11: true, 12: true, 13: false, 0: false,
	} {
		if got := confirmationSubjectEpochSupported(version); got != want {
			t.Fatalf("confirmationSubjectEpochSupported(%d) = %v, want %v", version, got, want)
		}
	}
	// The instrument allow-list stays {9, 12}. A live v11 *subject* must not
	// require a v11 *profile*.
	if confirmationBenchVersionSupported(11) {
		t.Fatal("instrument allow-list must not treat bench 11 as an installable profile")
	}
}

func TestTrustedConfirmationExecuteAcceptsV11SubjectAgainstV9Instrument(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	request := validTrustedConfirmationRequest(t, raw, profile)
	request.BenchVersion = 11
	acquired := 0
	executor := installTrustedExecutor(t, raw, confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		acquired++
		return validConfirmationRuntime(), nil
	}))
	executor.coordinate = func(
		context.Context, confirmationExecutionRequest, confirmationExecutionProfileWire, *confirmationRuntime,
	) (confirmationExecutionResult, error) {
		return confirmationExecutionResult{
			LongMemEval:                  json.RawMessage(`{"ok":"longmem"}`),
			InferenceAblation:            json.RawMessage(`{"ok":"inference"}`),
			EmbeddingAblation:            json.RawMessage(`{"ok":"embedding"}`),
			AblationCoordinatorLatencyMS: 1,
		}, nil
	}
	ctx, cancel := context.WithDeadline(context.Background(), request.Deadline)
	defer cancel()
	if _, err := executor.Execute(ctx, request); err != nil {
		t.Fatalf("v9 instrument rejected a v11 subject: %v", err)
	}
	if acquired != 1 {
		t.Fatalf("acquisitions = %d, want 1", acquired)
	}
}

func TestTrustedConfirmationExecuteBindsScreenedSourceAndCleansUp(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	request := validTrustedConfirmationRequest(t, raw, profile)
	var acquired confirmationRuntimeIdentity
	closed := false
	runtime := validConfirmationRuntime()
	runtime.Close = func() error { closed = true; return nil }
	executor := installTrustedExecutor(t, raw, confirmationRuntimeFactoryFunc(func(_ context.Context, identity confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		acquired = identity
		return runtime, nil
	}))
	executor.coordinate = func(
		_ context.Context,
		gotRequest confirmationExecutionRequest,
		gotProfile confirmationExecutionProfileWire,
		gotRuntime *confirmationRuntime,
	) (confirmationExecutionResult, error) {
		if gotRequest.ArtifactSHA256 != request.ArtifactSHA256 || !reflect.DeepEqual(gotProfile, profile) || gotRuntime != runtime {
			t.Fatal("orchestrator lost exact request, profile, or runtime identity")
		}
		return confirmationExecutionResult{
			LongMemEval:                  json.RawMessage(`{"ok":"longmem"}`),
			InferenceAblation:            json.RawMessage(`{"ok":"inference"}`),
			EmbeddingAblation:            json.RawMessage(`{"ok":"embedding"}`),
			AblationCoordinatorLatencyMS: 1,
		}, nil
	}
	ctx, cancel := context.WithDeadline(context.Background(), request.Deadline)
	defer cancel()
	if _, err := executor.Execute(ctx, request); err != nil {
		t.Fatal(err)
	}
	if !closed {
		t.Fatal("successful confirmation runtime was not closed")
	}
	if acquired.BundleID != request.BundleID || acquired.TicketID != request.TicketID ||
		acquired.AgentID != request.AgentID || acquired.SlotID != request.SlotID ||
		acquired.ArtifactSHA256 != request.ArtifactSHA256 || acquired.Deadline != request.Deadline {
		t.Fatalf("acquired identity = %+v", acquired)
	}
	if acquired.Source.GitURL != "" || acquired.Source.GitRef != "" || acquired.Source.GitSubdir != "" ||
		acquired.Source.TarballURL != request.ArtifactURL || acquired.Source.TarballSHA256 != request.ArtifactSHA256 ||
		acquired.Source.ScreenedImageURL != request.ScreenedImageURL ||
		acquired.Source.ScreenedImageSHA256 != request.ScreenedImageSHA256 ||
		acquired.Source.ScreenedImageSize != request.ScreenedImageSizeBytes ||
		acquired.Source.ScreenedImageID != request.ScreenedImageID ||
		acquired.Source.ScreenedImageRef != request.ScreenedImageRef {
		t.Fatalf("runtime source lost screened-image identity or gained a source-build path: %+v", acquired.Source)
	}
}

func TestTrustedConfirmationExecuteTreatsCleanupFailureAsBundleFailure(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	request := validTrustedConfirmationRequest(t, raw, profile)
	runtime := validConfirmationRuntime()
	closeCalls := 0
	closeFailure := errors.New("release screened image")
	runtime.Close = func() error { closeCalls++; return closeFailure }
	executor := installTrustedExecutor(t, raw, confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		return runtime, nil
	}))
	executor.coordinate = func(
		context.Context,
		confirmationExecutionRequest,
		confirmationExecutionProfileWire,
		*confirmationRuntime,
	) (confirmationExecutionResult, error) {
		return confirmationExecutionResult{
			LongMemEval:                  json.RawMessage(`{"ok":"longmem"}`),
			InferenceAblation:            json.RawMessage(`{"ok":"inference"}`),
			EmbeddingAblation:            json.RawMessage(`{"ok":"embedding"}`),
			AblationCoordinatorLatencyMS: 1,
			EvidenceSHA256:               strings.Repeat("f", 64),
		}, nil
	}
	ctx, cancel := context.WithDeadline(context.Background(), request.Deadline)
	defer cancel()
	if _, err := executor.Execute(ctx, request); err == nil || confirmationExecutionStage(err) != "runtime_close" ||
		!errors.Is(err, closeFailure) {
		t.Fatalf("cleanup error = %v", err)
	}
	if closeCalls != 1 {
		t.Fatalf("cleanup calls = %d, want exactly 1", closeCalls)
	}
}

func TestTrustedConfirmationDimensionDeadlineReservesCoordinatorBudget(t *testing.T) {
	t.Parallel()
	profile, _ := validInstalledConfirmationProfile(t)
	request := validConfirmationRequest()
	request.Mode = "shadow"
	request.ArtifactSHA256 = strings.Repeat("c", 64)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	_, err := executeTrustedConfirmationDimensions(ctx, request, profile, validConfirmationRuntime(), nil)
	if err == nil || !strings.Contains(err.Error(), "cannot fund both frozen dimensions") {
		t.Fatalf("deadline reservation error = %v", err)
	}
}

func TestTrustedConfirmationUnusedReaderZeroContinuesThroughAblationsAndRetainsJudgeSpend(t *testing.T) {
	profile, _ := validInstalledConfirmationProfile(t)
	capabilities := []struct {
		prefix       string
		questionType string
		abstention   bool
	}{
		{"extract", "single-session-user", false},
		{"multi", "multi-session", false},
		{"temporal", "temporal-reasoning", false},
		{"update", "knowledge-update", false},
		{"preference", "single-session-preference", false},
		{"abstain", "multi-session", true},
	}
	cases := make([]longmemeval.DatasetCase, 0, 12)
	for typeIndex, capability := range capabilities {
		for index := 0; index < 2; index++ {
			questionID := fmt.Sprintf("private-%s-%02d", capability.prefix, index)
			if capability.abstention {
				questionID += "_abs"
			}
			cases = append(cases, longmemeval.DatasetCase{
				QuestionID: questionID, QuestionType: capability.questionType,
				Question: "What passphrase did I ask you to remember?", Answer: fmt.Sprintf("answer-%d-%d", typeIndex, index),
				QuestionDate:       "2026/01/02 (Fri) 12:00",
				HaystackSessionIDs: []string{fmt.Sprintf("session-%d-%d", typeIndex, index)},
				HaystackDates:      []string{"2025/01/01 (Wed) 10:00"},
				HaystackSessions: [][]longmemeval.DatasetTurn{{
					{Role: "user", Content: "Remember the passphrase.", HasAnswer: true},
					{Role: "assistant", Content: "Stored.", HasAnswer: true},
				}},
				AnswerSessionIDs: []string{fmt.Sprintf("answer-%d-%d", typeIndex, index)},
			})
		}
	}
	longMemRaw, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	profile.LongMemDatasetRevision = "unused-reader-integration-v1"
	profile.LongMemDatasetSHA256 = digestBytes(longMemRaw)
	for index := range profile.ProviderLanes {
		if profile.ProviderLanes[index].Lane == longmemeval.JudgeLane {
			profile.ProviderLanes[index].MaxRequests = 12
		}
	}
	refreshConfirmationProfileChecksums(t, &profile)

	meter := newJudgeOnlyLongMemMeter(profile.longMemProfile())
	harness := &noReaderLongMemHarness{}
	runner := &successfulAblationRunner{}
	runtime := validConfirmationRuntime()
	runtime.LongMemSource = readSeekCloser{bytes.NewReader(longMemRaw)}
	runtime.LongMemHarness = harness
	runtime.LongMemJudge = alwaysCorrectLongMemJudge{meter: meter}
	runtime.LongMemMeter = meter
	runtime.AblationCaseRunner = runner
	request := validConfirmationRequest()
	request.Mode = "shadow"
	request.ArtifactSHA256 = strings.Repeat("c", 64)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := executeTrustedConfirmationDimensions(ctx, request, profile, runtime, nil)
	if err != nil {
		t.Fatal(err)
	}
	if harness.runs != 12 || runner.calls != 6 {
		t.Fatalf("longmem runs=%d ablation calls=%d", harness.runs, runner.calls)
	}
	if len(result.InferenceAblation) == 0 || len(result.EmbeddingAblation) == 0 ||
		!canonicalConfirmationSHA256(result.EvidenceSHA256) {
		t.Fatalf("confirmation did not reach both ablations/report digest: %#v", result)
	}
	var wrapper confirmationDimensionWire
	if err := decodeStrictConfirmationJSON(result.LongMemEval, &wrapper); err != nil {
		t.Fatal(err)
	}
	var evidence longmemeval.Evidence
	if err := decodeStrictConfirmationJSON(wrapper.Evidence, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence.Score.LongMemMean != 0 || evidence.Score.LongMemStdErr != 0 {
		t.Fatalf("unused reader retained score: %#v", evidence.Score)
	}
	for _, row := range evidence.ProviderEvidence {
		switch row.Lane {
		case longmemeval.ReaderLane:
			if row.Requests != 0 || row.CostUSDmicros != 0 || row.ReceiptSetSHA256 != "" {
				t.Fatalf("reader row=%#v", row)
			}
		case longmemeval.JudgeLane:
			if row.Requests != 12 || row.Successes != 12 || row.ReceiptedRequests != 12 || row.CostUSDmicros != 12 {
				t.Fatalf("judge spend was not retained: %#v", row)
			}
		default:
			t.Fatalf("unexpected provider lane %q", row.Lane)
		}
	}
}

func TestTrustedConfirmationExecuteRejectsBeforeAcquisition(t *testing.T) {
	t.Parallel()
	profile, raw := validInstalledConfirmationProfile(t)
	request := validTrustedConfirmationRequest(t, raw, profile)
	acquisitions := 0
	executor := installTrustedExecutor(t, raw, confirmationRuntimeFactoryFunc(func(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error) {
		acquisitions++
		return validConfirmationRuntime(), nil
	}))
	tests := []struct {
		name   string
		mutate func(*confirmationExecutionRequest)
		ctx    func(confirmationExecutionRequest) (context.Context, context.CancelFunc)
		want   string
	}{
		{
			name: "profile substitution",
			mutate: func(value *confirmationExecutionRequest) {
				changed := profile
				changed.Revision = "calibrated-but-not-installed"
				refreshConfirmationProfileChecksums(t, &changed)
				value.ProfileRevision, value.ProfileChecksum = changed.Revision, changed.Checksum
				value.ExecutionProfile, _ = json.Marshal(changed)
			},
			want: "not exactly ready",
		},
		{
			name:   "request cap smaller than frozen maximum",
			mutate: func(value *confirmationExecutionRequest) { value.PerBundleRequestCap-- },
			want:   "aggregate caps",
		},
		{
			name:   "token cap smaller than frozen maximum",
			mutate: func(value *confirmationExecutionRequest) { value.PerBundleTokenCap-- },
			want:   "aggregate caps",
		},
		{
			name:   "screened image missing",
			mutate: func(value *confirmationExecutionRequest) { value.ScreenedImageURL = "" },
			want:   "screened image identity",
		},
		{
			name: "context deadline extends ticket",
			ctx: func(value confirmationExecutionRequest) (context.Context, context.CancelFunc) {
				return context.WithDeadline(context.Background(), value.Deadline.Add(time.Second))
			},
			want: "exact live ticket deadline",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			candidate := request
			candidate.ExecutionProfile = append(json.RawMessage(nil), request.ExecutionProfile...)
			if test.mutate != nil {
				test.mutate(&candidate)
			}
			ctxFactory := test.ctx
			if ctxFactory == nil {
				ctxFactory = func(value confirmationExecutionRequest) (context.Context, context.CancelFunc) {
					return context.WithDeadline(context.Background(), value.Deadline)
				}
			}
			ctx, cancel := ctxFactory(candidate)
			defer cancel()
			before := acquisitions
			_, err := executor.Execute(ctx, candidate)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("error = %v, want substring %q", err, test.want)
			}
			if acquisitions != before {
				t.Fatalf("unsafe request acquired a runtime: %d -> %d", before, acquisitions)
			}
		})
	}
}

func TestConfirmationRuntimeValidationRejectsEveryRequiredBinding(t *testing.T) {
	t.Parallel()
	profile, _ := validInstalledConfirmationProfile(t)
	tests := []struct {
		name   string
		mutate func(*confirmationRuntime)
	}{
		{"source", func(value *confirmationRuntime) { value.LongMemSource = nil }},
		{"harness", func(value *confirmationRuntime) { value.LongMemHarness = nil }},
		{"judge", func(value *confirmationRuntime) { value.LongMemJudge = nil }},
		{"meter", func(value *confirmationRuntime) { value.LongMemMeter = nil }},
		{"case runner", func(value *confirmationRuntime) { value.AblationCaseRunner = nil }},
		{"closer", func(value *confirmationRuntime) { value.Close = nil }},
		{"longmem projection key", func(value *confirmationRuntime) { value.LongMemProjectionKey = nil }},
		{"ablation selection key", func(value *confirmationRuntime) { value.AblationSelectionKey = nil }},
		{"ablation projection key", func(value *confirmationRuntime) { value.AblationProjectionKey = nil }},
		{"ordinary population", func(value *confirmationRuntime) { value.AblationPopulation.Confirmation = false }},
		{"v8 population", func(value *confirmationRuntime) { value.AblationPopulation.BenchVersion = 8 }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			runtime := validConfirmationRuntime()
			test.mutate(runtime)
			if err := runtime.validate(profile); err == nil {
				t.Fatal("invalid runtime was accepted")
			}
		})
	}
}

func TestConfirmationRuntimeMaterialIsLeaseDerivedAndDomainSeparated(t *testing.T) {
	t.Parallel()
	profile, _ := validInstalledConfirmationProfile(t)
	identity := confirmationRuntimeIdentity{
		BundleID: "bundle-a", TicketID: "ticket-a", AgentID: "agent-a",
		ArtifactSHA256: strings.Repeat("a", 64), ProfileChecksum: profile.Checksum,
		SettingsChecksum: strings.Repeat("b", 64), SettingsRevision: 7, RetestGeneration: 2,
	}
	longMem, selection, projection, err := deriveConfirmationRuntimeMaterial(profile, identity)
	if err != nil {
		t.Fatal(err)
	}
	againLongMem, againSelection, againProjection, err := deriveConfirmationRuntimeMaterial(profile, identity)
	if err != nil {
		t.Fatal(err)
	}
	for name, pair := range map[string][2][]byte{
		"longmem": {longMem, againLongMem}, "selection": {selection, againSelection},
		"projection": {projection, againProjection},
	} {
		if len(pair[0]) != sha256.Size || !bytes.Equal(pair[0], pair[1]) {
			t.Fatalf("%s derivation is not deterministic 32-byte material", name)
		}
	}
	if bytes.Equal(longMem, selection) || bytes.Equal(longMem, projection) || bytes.Equal(selection, projection) {
		t.Fatal("confirmation runtime derivation domains collided")
	}
	for name, mutate := range map[string]func(*confirmationRuntimeIdentity){
		"bundle":            func(value *confirmationRuntimeIdentity) { value.BundleID += "-changed" },
		"agent":             func(value *confirmationRuntimeIdentity) { value.AgentID += "-changed" },
		"artifact":          func(value *confirmationRuntimeIdentity) { value.ArtifactSHA256 = strings.Repeat("c", 64) },
		"profile":           func(value *confirmationRuntimeIdentity) { value.ProfileChecksum = strings.Repeat("d", 64) },
		"settings":          func(value *confirmationRuntimeIdentity) { value.SettingsChecksum = strings.Repeat("e", 64) },
		"settings revision": func(value *confirmationRuntimeIdentity) { value.SettingsRevision++ },
		"generation":        func(value *confirmationRuntimeIdentity) { value.RetestGeneration++ },
	} {
		t.Run(name, func(t *testing.T) {
			changed := identity
			mutate(&changed)
			changedLongMem, changedSelection, changedProjection, changedErr :=
				deriveConfirmationRuntimeMaterial(profile, changed)
			if name == "profile" {
				if changedErr == nil {
					t.Fatal("profile checksum drift was accepted")
				}
				return
			}
			if changedErr != nil {
				t.Fatal(changedErr)
			}
			if bytes.Equal(longMem, changedLongMem) || bytes.Equal(selection, changedSelection) ||
				bytes.Equal(projection, changedProjection) {
				t.Fatal("changed signed lease input did not move every runtime domain")
			}
		})
	}
	retry := identity
	retry.TicketID = "ticket-retry"
	retry.SlotID = "slot-retry"
	retry.InferenceSessionID = "session-retry"
	retry.Deadline = time.Now().Add(time.Hour)
	retryLongMem, retrySelection, retryProjection, err := deriveConfirmationRuntimeMaterial(profile, retry)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(longMem, retryLongMem) || !bytes.Equal(selection, retrySelection) ||
		!bytes.Equal(projection, retryProjection) {
		t.Fatal("transport-only confirmation retry changed stable runtime material")
	}
}

func TestConfirmationDimensionWrapperAndWireDigestAreStrict(t *testing.T) {
	t.Parallel()
	type evidence struct {
		Value int `json:"value"`
	}
	digest := digestBytes([]byte(`{"value":7}`))
	raw, err := marshalConfirmationDimension(evidence{Value: 7}, digest, 19)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `{"go_evidence_sha256":"`+digest+`","latency_ms":19,"evidence":{"value":7}}` {
		t.Fatalf("wrapper = %s", raw)
	}
	result := confirmationExecutionResult{
		LongMemEval: raw, InferenceAblation: raw, EmbeddingAblation: raw,
		AblationCoordinatorLatencyMS: 23,
	}
	got, err := confirmationWireSHA256(result)
	if err != nil {
		t.Fatal(err)
	}
	const want = "36599e33c74c75d8f426df4682a5a2b8d7d0d1796f3d12467315f9955acb88b5"
	if got != want {
		t.Fatalf("wire digest = %s, want %s", got, want)
	}
	for _, hostile := range []string{
		`{"go_evidence_sha256":"` + digest + `","latency_ms":19,"evidence":{"value":7},"extra":true}`,
		`{"go_evidence_sha256":"` + digest + `","latency_ms":19,"latency_ms":20,"evidence":{"value":7}}`,
		`{"go_evidence_sha256":"` + digest + `","latency_ms":19,"evidence":{"value":7}} {}`,
	} {
		var wrapper confirmationDimensionWire
		if err := decodeStrictConfirmationJSON([]byte(hostile), &wrapper); err == nil {
			t.Fatalf("hostile wrapper accepted: %s", hostile)
		}
	}
}

func TestConfirmationWireDigestMatchesPythonFixtureWithoutJSONFloatCoupling(t *testing.T) {
	t.Parallel()
	result := confirmationExecutionResult{
		LongMemEval:                  json.RawMessage(`{"go_evidence_sha256":"` + strings.Repeat("91", 32) + `","latency_ms":101,"evidence":{"float":1e-7}}`),
		InferenceAblation:            json.RawMessage(`{"go_evidence_sha256":"` + strings.Repeat("92", 32) + `","latency_ms":102,"evidence":{"float":1e-07}}`),
		EmbeddingAblation:            json.RawMessage(`{"go_evidence_sha256":"` + strings.Repeat("93", 32) + `","latency_ms":103,"evidence":{"float":0.0000001}}`),
		AblationCoordinatorLatencyMS: 37,
	}
	got, err := confirmationWireSHA256(result)
	if err != nil {
		t.Fatal(err)
	}
	// Pinned by the validator's Python transport test with the same native
	// producer digests and latencies. Evidence JSON formatting is irrelevant.
	const want = "8273b64aa8408f9e98b68c18b0e8f70b87f3d864f45e1485cae7990cce107df6"
	if got != want {
		t.Fatalf("cross-language wire digest = %s, want %s", got, want)
	}
}

func TestConfirmationAblationUpstreamAccountingMustStayExactlyZero(t *testing.T) {
	t.Parallel()
	report := ablation.CoordinationReport{
		InferenceIntervention: ablation.LaneReport{SyntheticUsage: ablation.Usage{}},
		EmbeddingIntervention: ablation.LaneReport{SyntheticUsage: ablation.Usage{}},
	}
	if err := requireZeroAblationUpstream(report); err != nil {
		t.Fatal(err)
	}
	mutations := []func(*ablation.Usage){
		func(value *ablation.Usage) { value.UpstreamRequests = 1 },
		func(value *ablation.Usage) { value.UpstreamInputTokens = 1 },
		func(value *ablation.Usage) { value.UpstreamOutputTokens = 1 },
		func(value *ablation.Usage) { value.UpstreamProviderCostMicroUSD = 1 },
	}
	for index, mutate := range mutations {
		changed := report
		mutate(&changed.InferenceIntervention.SyntheticUsage)
		if err := requireZeroAblationUpstream(changed); err == nil {
			t.Fatalf("upstream telemetry mutation %d was accepted", index)
		}
	}
}

func TestConfirmationElapsedMillisecondsIsPositiveAndMeasuredOnce(t *testing.T) {
	t.Parallel()
	start := time.Unix(0, 0)
	if got := elapsedMilliseconds(start, start); got != 1 {
		t.Fatalf("zero duration = %d, want 1", got)
	}
	if got := elapsedMilliseconds(start, start.Add(19*time.Millisecond+999*time.Microsecond)); got != 19 {
		t.Fatalf("measured duration = %d, want 19", got)
	}
}

var _ io.ReadSeekCloser = readSeekCloser{}
