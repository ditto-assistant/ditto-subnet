package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testCalibrationManifestSHA256 = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

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
			Intervention: string(ablation.InterventionInference), ContractVersion: ablation.ContractVersion,
			ThresholdMicros: 200_000,
			Budget:          confirmationAblationBudget{MaxChatRequests: 10, MaxChatInputBytes: 10_000},
		},
		EmbeddingAblation: confirmationAblationProfile{
			Intervention: string(ablation.InterventionEmbedding), ContractVersion: ablation.ContractVersion,
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
	if profile.Checksum != "38d9334acd0f17e257d151d66e3feb6d5c0a200f0c5091f8bd29f90e387fd3f1" {
		t.Fatalf("cross-language outer profile checksum = %s", profile.Checksum)
	}
	raw, err := json.Marshal(profile)
	if err != nil {
		t.Fatal(err)
	}
	return profile, raw
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
	request.PerBundleRequestCap = 20
	request.PerBundleTokenCap = 400
	return request
}

func installTrustedExecutor(
	t *testing.T,
	raw json.RawMessage,
	factory confirmationRuntimeFactory,
) *trustedConfirmationExecutor {
	t.Helper()
	executor, err := newTrustedConfirmationExecutor(confirmationProfileInstallation{
		ExecutionProfile: raw, CalibrationManifestSHA256: testCalibrationManifestSHA256,
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
		{"nil runtime factory", confirmationProfileInstallation{ExecutionProfile: raw, CalibrationManifestSHA256: testCalibrationManifestSHA256}, nil, "runtime factory"},
		{"missing calibration approval", confirmationProfileInstallation{ExecutionProfile: raw}, validFactory, "calibrated launch approval"},
		{"invalid calibration approval", confirmationProfileInstallation{ExecutionProfile: raw, CalibrationManifestSHA256: "not-a-digest"}, validFactory, "calibrated launch approval"},
		{"missing profile", confirmationProfileInstallation{CalibrationManifestSHA256: testCalibrationManifestSHA256}, validFactory, "decode confirmation"},
		{"unvalidated runtime dependencies", confirmationProfileInstallation{ExecutionProfile: raw, CalibrationManifestSHA256: testCalibrationManifestSHA256}, rejectedConfirmationRuntimeFactory{err: errors.New("judge is nil")}, "installation is not ready"},
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
			needle: "projection key checksum",
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
				ExecutionProfile: test.raw(), CalibrationManifestSHA256: testCalibrationManifestSHA256,
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
	runtime.Close = func() error { closeCalls++; return errors.New("release screened image") }
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
	if _, err := executor.Execute(ctx, request); err == nil || !strings.Contains(err.Error(), "close confirmation runtime") {
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
	_, err := executeTrustedConfirmationDimensions(ctx, request, profile, validConfirmationRuntime())
	if err == nil || !strings.Contains(err.Error(), "cannot fund both frozen dimensions") {
		t.Fatalf("deadline reservation error = %v", err)
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
		{"longmem projection key drift", func(value *confirmationRuntime) { value.LongMemProjectionKey[0] ^= 0xff }},
		{"ablation selection key", func(value *confirmationRuntime) { value.AblationSelectionKey = nil }},
		{"ablation projection key", func(value *confirmationRuntime) { value.AblationProjectionKey = nil }},
		{"selection key drift", func(value *confirmationRuntime) { value.AblationSelectionKey[0] ^= 0xff }},
		{"projection key drift", func(value *confirmationRuntime) { value.AblationProjectionKey[0] ^= 0xff }},
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
