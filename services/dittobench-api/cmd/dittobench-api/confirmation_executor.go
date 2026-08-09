package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"reflect"
	"sort"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

const confirmationProfileSchemaVersion = 1

// confirmationProfileInstallation is deliberately not populated from
// environment variables. A future activation change must install one exact,
// calibrated profile and the digest of the calibration manifest that approved
// it. main leaves the registry absent, so production remains fail-closed.
type confirmationProfileInstallation struct {
	ExecutionProfile          json.RawMessage
	CalibrationManifestSHA256 string
}

type confirmationProviderLaneProfile struct {
	Lane                string `json:"lane"`
	Provider            string `json:"provider"`
	ProfileRevision     string `json:"profile_revision"`
	Model               string `json:"model"`
	MaxRequests         uint64 `json:"max_requests"`
	MaxPromptTokens     uint64 `json:"max_prompt_tokens"`
	MaxCompletionTokens uint64 `json:"max_completion_tokens"`
	MaxTotalTokens      uint64 `json:"max_total_tokens"`
	MaxCostUSDmicros    uint64 `json:"max_cost_usd_micros"`
}

type confirmationAblationBudget struct {
	MaxChatRequests        uint64 `json:"max_chat_requests"`
	MaxChatInputBytes      uint64 `json:"max_chat_input_bytes"`
	MaxEmbeddingRequests   uint64 `json:"max_embedding_requests"`
	MaxEmbeddingInputs     uint64 `json:"max_embedding_inputs"`
	MaxEmbeddingInputBytes uint64 `json:"max_embedding_input_bytes"`
}

type confirmationAblationProfile struct {
	Intervention    string                     `json:"intervention"`
	ContractVersion string                     `json:"contract_version"`
	ThresholdMicros uint64                     `json:"threshold_micros"`
	Budget          confirmationAblationBudget `json:"budget"`
}

type confirmationCompositeProfile struct {
	SchemaVersion    int    `json:"schema_version"`
	Revision         string `json:"revision"`
	FormulaRevision  string `json:"formula_revision"`
	BaseWeightBPS    uint64 `json:"base_weight_bps"`
	LongMemWeightBPS uint64 `json:"longmem_weight_bps"`
	Checksum         string `json:"checksum"`
}

type confirmationExecutionProfileWire struct {
	SchemaVersion                   int                               `json:"schema_version"`
	Revision                        string                            `json:"revision"`
	Checksum                        string                            `json:"checksum"`
	LongMemProfileRevision          string                            `json:"longmem_profile_revision"`
	LongMemProfileChecksum          string                            `json:"longmem_profile_checksum"`
	LongMemDatasetRevision          string                            `json:"longmem_dataset_revision"`
	LongMemDatasetSHA256            string                            `json:"longmem_dataset_sha256"`
	LongMemSelectorRevision         string                            `json:"longmem_selector_revision"`
	LongMemSelectionSeed            uint64                            `json:"longmem_selection_seed"`
	LongMemCasesPerCapability       int                               `json:"longmem_cases_per_capability"`
	LongMemSeedBatchPairs           int                               `json:"longmem_seed_batch_pairs"`
	LongMemProjectionKeySHA256      string                            `json:"longmem_projection_key_sha256"`
	ProviderLanes                   []confirmationProviderLaneProfile `json:"provider_lanes"`
	AblationProfileRevision         string                            `json:"ablation_profile_revision"`
	AblationProfileChecksum         string                            `json:"ablation_profile_checksum"`
	AblationDatasetSHA256           string                            `json:"ablation_dataset_sha256"`
	AblationThresholdManifestSHA256 string                            `json:"ablation_threshold_manifest_sha256"`
	AblationSelectionKeySHA256      string                            `json:"ablation_selection_key_sha256"`
	AblationProjectionKeySHA256     string                            `json:"ablation_projection_key_sha256"`
	AblationCoordinatorPolicy       ablation.CoordinatorPolicy        `json:"ablation_coordinator_policy"`
	InferenceAblation               confirmationAblationProfile       `json:"inference_ablation"`
	EmbeddingAblation               confirmationAblationProfile       `json:"embedding_ablation"`
	Composite                       confirmationCompositeProfile      `json:"composite"`
}

// confirmationExecutionProfilePayload mirrors Platform's checksummed profile
// payload. Checksum is transport metadata and is intentionally absent here.
type confirmationExecutionProfilePayload struct {
	SchemaVersion                   int                               `json:"schema_version"`
	Revision                        string                            `json:"revision"`
	LongMemProfileRevision          string                            `json:"longmem_profile_revision"`
	LongMemProfileChecksum          string                            `json:"longmem_profile_checksum"`
	LongMemDatasetRevision          string                            `json:"longmem_dataset_revision"`
	LongMemDatasetSHA256            string                            `json:"longmem_dataset_sha256"`
	LongMemSelectorRevision         string                            `json:"longmem_selector_revision"`
	LongMemSelectionSeed            uint64                            `json:"longmem_selection_seed"`
	LongMemCasesPerCapability       int                               `json:"longmem_cases_per_capability"`
	LongMemSeedBatchPairs           int                               `json:"longmem_seed_batch_pairs"`
	LongMemProjectionKeySHA256      string                            `json:"longmem_projection_key_sha256"`
	ProviderLanes                   []confirmationProviderLaneProfile `json:"provider_lanes"`
	AblationProfileRevision         string                            `json:"ablation_profile_revision"`
	AblationProfileChecksum         string                            `json:"ablation_profile_checksum"`
	AblationDatasetSHA256           string                            `json:"ablation_dataset_sha256"`
	AblationThresholdManifestSHA256 string                            `json:"ablation_threshold_manifest_sha256"`
	AblationSelectionKeySHA256      string                            `json:"ablation_selection_key_sha256"`
	AblationProjectionKeySHA256     string                            `json:"ablation_projection_key_sha256"`
	AblationCoordinatorPolicy       ablation.CoordinatorPolicy        `json:"ablation_coordinator_policy"`
	InferenceAblation               confirmationAblationProfile       `json:"inference_ablation"`
	EmbeddingAblation               confirmationAblationProfile       `json:"embedding_ablation"`
	Composite                       confirmationCompositeProfile      `json:"composite"`
}

func (p confirmationExecutionProfileWire) payload() confirmationExecutionProfilePayload {
	return confirmationExecutionProfilePayload{
		SchemaVersion: p.SchemaVersion, Revision: p.Revision,
		LongMemProfileRevision: p.LongMemProfileRevision, LongMemProfileChecksum: p.LongMemProfileChecksum,
		LongMemDatasetRevision: p.LongMemDatasetRevision, LongMemDatasetSHA256: p.LongMemDatasetSHA256,
		LongMemSelectorRevision: p.LongMemSelectorRevision, LongMemSelectionSeed: p.LongMemSelectionSeed,
		LongMemCasesPerCapability: p.LongMemCasesPerCapability, LongMemSeedBatchPairs: p.LongMemSeedBatchPairs,
		LongMemProjectionKeySHA256: p.LongMemProjectionKeySHA256,
		ProviderLanes:              append([]confirmationProviderLaneProfile(nil), p.ProviderLanes...),
		AblationProfileRevision:    p.AblationProfileRevision, AblationProfileChecksum: p.AblationProfileChecksum,
		AblationDatasetSHA256:           p.AblationDatasetSHA256,
		AblationThresholdManifestSHA256: p.AblationThresholdManifestSHA256,
		AblationSelectionKeySHA256:      p.AblationSelectionKeySHA256,
		AblationProjectionKeySHA256:     p.AblationProjectionKeySHA256,
		AblationCoordinatorPolicy:       p.AblationCoordinatorPolicy,
		InferenceAblation:               p.InferenceAblation, EmbeddingAblation: p.EmbeddingAblation,
		Composite: p.Composite,
	}
}

func (p confirmationExecutionProfileWire) longMemProfile() longmemeval.Profile {
	providers := make([]longmemeval.ProviderPolicy, len(p.ProviderLanes))
	for index, lane := range p.ProviderLanes {
		providers[index] = longmemeval.ProviderPolicy{
			Lane: lane.Lane, Provider: lane.Provider, ProfileRevision: lane.ProfileRevision, Model: lane.Model,
			MaxRequests: lane.MaxRequests, MaxPromptTokens: lane.MaxPromptTokens,
			MaxCompletionTokens: lane.MaxCompletionTokens, MaxTotalTokens: lane.MaxTotalTokens,
			MaxCostUSDmicros: lane.MaxCostUSDmicros,
		}
	}
	return longmemeval.Profile{
		SchemaVersion: longmemeval.ProfileSchemaVersion, Revision: p.LongMemProfileRevision,
		BenchVersion: confirmationBenchVersion, DatasetRevision: p.LongMemDatasetRevision,
		DatasetSHA256: p.LongMemDatasetSHA256, SelectorRevision: p.LongMemSelectorRevision,
		SelectionSeed: p.LongMemSelectionSeed, CasesPerCapability: p.LongMemCasesPerCapability,
		Providers: providers,
	}
}

func (p confirmationExecutionProfileWire) ablationProfile() ablation.FrozenProfile {
	return ablation.FrozenProfile{
		ContractVersion: ablation.ProfileContractVersion, Revision: p.AblationProfileRevision,
		BenchVersion: confirmationBenchVersion, DatasetSHA256: p.AblationDatasetSHA256,
		ThresholdManifestSHA256: p.AblationThresholdManifestSHA256,
		SelectionKeySHA256:      p.AblationSelectionKeySHA256,
		ProjectionKeySHA256:     p.AblationProjectionKeySHA256,
		CoordinatorPolicy:       p.AblationCoordinatorPolicy,
		InferenceBudget:         p.InferenceAblation.Budget.ablationBudget(),
		EmbeddingBudget:         p.EmbeddingAblation.Budget.ablationBudget(),
		InferenceThreshold:      microsToScore(p.InferenceAblation.ThresholdMicros),
		EmbeddingThreshold:      microsToScore(p.EmbeddingAblation.ThresholdMicros),
	}
}

func (b confirmationAblationBudget) ablationBudget() ablation.Budget {
	return ablation.Budget{
		MaxChatRequests: b.MaxChatRequests, MaxChatInputBytes: b.MaxChatInputBytes,
		MaxEmbeddingRequests: b.MaxEmbeddingRequests, MaxEmbeddingInputs: b.MaxEmbeddingInputs,
		MaxEmbeddingInputBytes: b.MaxEmbeddingInputBytes,
	}
}

func microsToScore(value uint64) float64 { return float64(value) / 1_000_000 }

type confirmationRuntimeIdentity struct {
	BundleID       string
	TicketID       string
	AgentID        string
	SlotID         string
	ArtifactSHA256 string
	Deadline       time.Time
	Source         sandbox.Source
}

// confirmationRuntimeFactory is the only integration seam. Its production
// implementation must acquire Source.ScreenedImage* through LocalDocker. It
// must never build Source.TarballURL. The source URL and digest remain present
// solely to bind artifact provenance to the already screened image.
type confirmationRuntimeFactory interface {
	ValidateInstallation(confirmationExecutionProfileWire) error
	Acquire(context.Context, confirmationRuntimeIdentity) (*confirmationRuntime, error)
}

// confirmationRuntime contains no provider endpoints or credentials. Real
// Judge, ProviderMeter, CaseRunner, dataset, and artifact lifecycle adapters
// remain required injected bindings and are intentionally not implemented here.
type confirmationRuntime struct {
	LongMemSource         io.ReadSeekCloser
	LongMemHarness        longmemeval.Harness
	LongMemJudge          longmemeval.Judge
	LongMemMeter          longmemeval.ProviderMeter
	LongMemProjectionKey  []byte
	AblationPopulation    ablation.EligiblePopulation
	AblationCaseRunner    ablation.CaseRunner
	AblationSelectionKey  []byte
	AblationProjectionKey []byte
	Close                 func() error
}

func (runtime *confirmationRuntime) validate(profile confirmationExecutionProfileWire) error {
	if runtime == nil || nilInterface(runtime.LongMemSource) || nilInterface(runtime.LongMemHarness) ||
		nilInterface(runtime.LongMemJudge) || nilInterface(runtime.LongMemMeter) ||
		nilInterface(runtime.AblationCaseRunner) || runtime.Close == nil {
		return errors.New("confirmation runtime dependencies are incomplete")
	}
	if len(runtime.LongMemProjectionKey) < 32 || len(runtime.AblationSelectionKey) < 32 ||
		len(runtime.AblationProjectionKey) < 32 {
		return errors.New("confirmation runtime keys are incomplete")
	}
	if digestBytes(runtime.LongMemProjectionKey) != profile.LongMemProjectionKeySHA256 ||
		digestBytes(runtime.AblationSelectionKey) != profile.AblationSelectionKeySHA256 ||
		digestBytes(runtime.AblationProjectionKey) != profile.AblationProjectionKeySHA256 {
		return errors.New("confirmation runtime keys do not match the installed profile")
	}
	if runtime.AblationPopulation.BenchVersion != confirmationBenchVersion || !runtime.AblationPopulation.Confirmation {
		return errors.New("confirmation ablation population is not a v9 confirmation population")
	}
	return nil
}

func nilInterface(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	return (reflected.Kind() == reflect.Pointer || reflected.Kind() == reflect.Interface ||
		reflected.Kind() == reflect.Map || reflected.Kind() == reflect.Slice || reflected.Kind() == reflect.Func) &&
		reflected.IsNil()
}

type trustedConfirmationExecutor struct {
	profile        confirmationExecutionProfileWire
	profileRaw     json.RawMessage
	runtimeFactory confirmationRuntimeFactory
	now            func() time.Time
	coordinate     func(context.Context, confirmationExecutionRequest, confirmationExecutionProfileWire, *confirmationRuntime) (confirmationExecutionResult, error)
}

func newTrustedConfirmationExecutor(
	installation confirmationProfileInstallation,
	runtimeFactory confirmationRuntimeFactory,
) (*trustedConfirmationExecutor, error) {
	if nilInterface(runtimeFactory) {
		return nil, errors.New("confirmation runtime factory is required")
	}
	if !canonicalConfirmationSHA256(installation.CalibrationManifestSHA256) {
		return nil, errors.New("confirmation profile lacks a calibrated launch approval")
	}
	profile, raw, err := decodeAndValidateConfirmationProfile(installation.ExecutionProfile)
	if err != nil {
		return nil, err
	}
	if err := runtimeFactory.ValidateInstallation(profile); err != nil {
		return nil, fmt.Errorf("confirmation runtime installation is not ready: %w", err)
	}
	return &trustedConfirmationExecutor{
		profile: profile, profileRaw: raw, runtimeFactory: runtimeFactory, now: time.Now,
		coordinate: executeTrustedConfirmationDimensions,
	}, nil
}

func (executor *trustedConfirmationExecutor) Readiness() confirmationReadiness {
	if executor == nil || nilInterface(executor.runtimeFactory) || executor.now == nil || executor.coordinate == nil ||
		executor.profile.Revision == "" || !canonicalConfirmationSHA256(executor.profile.Checksum) {
		return confirmationReadiness{Ready: false}
	}
	if err := executor.runtimeFactory.ValidateInstallation(executor.profile); err != nil {
		return confirmationReadiness{Ready: false}
	}
	return confirmationReadiness{
		Ready: true, ProfileRevision: executor.profile.Revision, ProfileChecksum: executor.profile.Checksum,
	}
}

func (executor *trustedConfirmationExecutor) Execute(
	ctx context.Context,
	request confirmationExecutionRequest,
) (confirmationExecutionResult, error) {
	if executor == nil || !executor.Readiness().Ready {
		return confirmationExecutionResult{}, errors.New("confirmation executor is not ready")
	}
	if ctx == nil {
		return confirmationExecutionResult{}, errors.New("confirmation execution context is required")
	}
	if err := request.validate(executor.now(), executor.Readiness()); err != nil {
		return confirmationExecutionResult{}, err
	}
	requested, _, err := decodeAndValidateConfirmationProfile(request.ExecutionProfile)
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("confirmation request profile: %w", err)
	}
	if !reflect.DeepEqual(requested, executor.profile) || request.ProfileRevision != executor.profile.Revision ||
		request.ProfileChecksum != executor.profile.Checksum {
		return confirmationExecutionResult{}, errors.New("confirmation request does not match the installed launch profile")
	}
	deadline, ok := ctx.Deadline()
	if !ok || deadline.After(request.Deadline) || !deadline.After(executor.now()) {
		return confirmationExecutionResult{}, errors.New("confirmation execution context lacks the exact live ticket deadline")
	}
	if err := validateConfirmationAggregateCaps(executor.profile, request); err != nil {
		return confirmationExecutionResult{}, err
	}
	identity := confirmationRuntimeIdentity{
		BundleID: request.BundleID, TicketID: request.TicketID, AgentID: request.AgentID, SlotID: request.SlotID,
		ArtifactSHA256: request.ArtifactSHA256, Deadline: request.Deadline,
		Source: sandbox.Source{
			TarballURL: request.ArtifactURL, TarballSHA256: request.ArtifactSHA256,
			ScreenedImageURL: request.ScreenedImageURL, ScreenedImageSHA256: request.ScreenedImageSHA256,
			ScreenedImageSize: request.ScreenedImageSizeBytes, ScreenedImageID: request.ScreenedImageID,
			ScreenedImageRef: request.ScreenedImageRef,
		},
	}
	runtime, err := executor.runtimeFactory.Acquire(ctx, identity)
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("acquire confirmation runtime: %w", err)
	}
	if runtime == nil {
		return confirmationExecutionResult{}, errors.New("confirmation runtime factory returned nil")
	}
	closed := false
	if runtime.Close != nil {
		defer func() {
			if !closed {
				_ = runtime.Close()
			}
		}()
	}
	if err := runtime.validate(executor.profile); err != nil {
		return confirmationExecutionResult{}, err
	}
	result, err := executor.coordinate(ctx, request, executor.profile, runtime)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	closeErr := runtime.Close()
	closed = true
	if closeErr != nil {
		return confirmationExecutionResult{}, fmt.Errorf("close confirmation runtime: %w", closeErr)
	}
	return result, nil
}

func validateConfirmationAggregateCaps(profile confirmationExecutionProfileWire, request confirmationExecutionRequest) error {
	var requests, tokens uint64
	for _, lane := range profile.ProviderLanes {
		var ok bool
		requests, ok = checkedAdd(requests, lane.MaxRequests)
		if !ok {
			return errors.New("confirmation provider request caps overflow")
		}
		tokens, ok = checkedAdd(tokens, lane.MaxTotalTokens)
		if !ok {
			return errors.New("confirmation provider token caps overflow")
		}
	}
	if requests > request.PerBundleRequestCap || tokens > request.PerBundleTokenCap {
		return errors.New("confirmation installed profile exceeds the ticket aggregate caps")
	}
	return nil
}

func executeTrustedConfirmationDimensions(
	ctx context.Context,
	request confirmationExecutionRequest,
	profile confirmationExecutionProfileWire,
	runtime *confirmationRuntime,
) (confirmationExecutionResult, error) {
	mode, err := ablation.ParseMode(request.Mode)
	if err != nil || mode == ablation.ModeOff {
		return confirmationExecutionResult{}, errors.New("confirmation ablation mode is not active")
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		return confirmationExecutionResult{}, errors.New("confirmation execution deadline is missing")
	}
	coordinatorBudget := time.Duration(profile.AblationCoordinatorPolicy.TotalTimeoutMilliseconds) * time.Millisecond
	longMemBudget := time.Until(deadline) - coordinatorBudget
	if longMemBudget <= 0 {
		return confirmationExecutionResult{}, errors.New("confirmation ticket cannot fund both frozen dimensions")
	}
	longMemProfile := profile.longMemProfile()
	longMemResult, err := (longmemeval.Executor{
		Harness: runtime.LongMemHarness, Judge: runtime.LongMemJudge, Meter: runtime.LongMemMeter,
		Limits: longmemeval.ExecutionLimits{MaxElapsed: longMemBudget, SeedBatchPairs: profile.LongMemSeedBatchPairs},
	}).Execute(ctx, runtime.LongMemSource, longMemProfile, request.ArtifactSHA256, runtime.LongMemProjectionKey)
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("execute LongMemEval: %w", err)
	}
	if err := longMemResult.Validate(longMemProfile); err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("validate LongMemEval evidence: %w", err)
	}
	longMemDigest, err := longMemResult.Digest(longMemProfile)
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("digest LongMemEval evidence: %w", err)
	}
	longMemRaw, err := marshalConfirmationDimension(longMemResult.Evidence, longMemDigest, longMemResult.Evidence.LatencyMS)
	if err != nil {
		return confirmationExecutionResult{}, err
	}

	ablationProfile := profile.ablationProfile()
	coordinator, err := ablation.NewCoordinator(ablation.CoordinatorConfig{
		SampleSize:     profile.AblationCoordinatorPolicy.SampleSize,
		MaxAttempts:    profile.AblationCoordinatorPolicy.MaxAttempts,
		MaxRequests:    profile.AblationCoordinatorPolicy.MaxRequests,
		RequestTimeout: time.Duration(profile.AblationCoordinatorPolicy.RequestTimeoutMilliseconds) * time.Millisecond,
		TotalTimeout:   coordinatorBudget, ArtifactSHA256: request.ArtifactSHA256,
		SelectionKey: runtime.AblationSelectionKey, ProjectionKey: runtime.AblationProjectionKey,
		FrozenProfile: ablationProfile, ProfileSHA256: profile.AblationProfileChecksum,
	})
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("construct ablation coordinator: %w", err)
	}
	inferenceResponder, err := ablation.NewResponder(ablation.InterventionInference, ablationProfile.InferenceBudget)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	embeddingResponder, err := ablation.NewResponder(ablation.InterventionEmbedding, ablationProfile.EmbeddingBudget)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	coordinatorStarted := time.Now()
	report, err := coordinator.Coordinate(
		ctx, runtime.AblationPopulation, runtime.AblationCaseRunner, inferenceResponder, embeddingResponder,
	)
	coordinatorLatency := elapsedMilliseconds(coordinatorStarted, time.Now())
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf("coordinate ablations: %w", err)
	}
	if err := requireZeroAblationUpstream(report); err != nil {
		return confirmationExecutionResult{}, err
	}
	inference, err := evaluateConfirmationAblation(
		request.ArtifactSHA256, mode, ablation.InterventionInference, profile, ablationProfile, report,
	)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	embedding, err := evaluateConfirmationAblation(
		request.ArtifactSHA256, mode, ablation.InterventionEmbedding, profile, ablationProfile, report,
	)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	inferenceRaw, err := marshalConfirmationDimension(inference.Evidence, inference.SHA256, coordinatorLatency)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	embeddingRaw, err := marshalConfirmationDimension(embedding.Evidence, embedding.SHA256, coordinatorLatency)
	if err != nil {
		return confirmationExecutionResult{}, err
	}
	result := confirmationExecutionResult{
		LongMemEval: longMemRaw, InferenceAblation: inferenceRaw, EmbeddingAblation: embeddingRaw,
		AblationCoordinatorLatencyMS: coordinatorLatency,
	}
	wireDigest, err := confirmationWireSHA256(result)
	if err != nil {
		return confirmationExecutionResult{}, fmt.Errorf(
			"digest confirmation native wire: %w", err,
		)
	}
	result.EvidenceSHA256 = wireDigest
	return result, nil
}

func evaluateConfirmationAblation(
	artifactSHA256 string,
	mode ablation.Mode,
	intervention ablation.Intervention,
	profile confirmationExecutionProfileWire,
	frozen ablation.FrozenProfile,
	report ablation.CoordinationReport,
) (ablation.Evaluation, error) {
	population, populationErr := report.GatePopulation(intervention)
	if populationErr != nil && !errors.Is(populationErr, ablation.ErrUnavailablePopulation) {
		return ablation.Evaluation{}, fmt.Errorf("ablation %s population: %w", intervention, populationErr)
	}
	threshold := frozen.InferenceThreshold
	if intervention == ablation.InterventionEmbedding {
		threshold = frozen.EmbeddingThreshold
	}
	evaluation, err := ablation.Evaluate(ablation.EvaluateInput{
		BenchVersion: confirmationBenchVersion, ArtifactSHA256: artifactSHA256,
		Intervention: intervention, Mode: mode, ProfileRevision: frozen.Revision,
		ThresholdManifestSHA256: frozen.ThresholdManifestSHA256, DatasetSHA256: frozen.DatasetSHA256,
		Threshold: threshold, Baseline: population.Baseline, Ablated: population.Ablated,
		FrozenProfile:         frozen,
		AblationProfileSHA256: profile.AblationProfileChecksum, Coordinator: report,
	})
	if err != nil {
		return ablation.Evaluation{}, fmt.Errorf("evaluate %s ablation: %w", intervention, err)
	}
	return evaluation, nil
}

func requireZeroAblationUpstream(report ablation.CoordinationReport) error {
	for _, lane := range []ablation.LaneReport{report.InferenceIntervention, report.EmbeddingIntervention} {
		usage := lane.SyntheticUsage
		if usage.UpstreamRequests != 0 || usage.UpstreamInputTokens != 0 || usage.UpstreamOutputTokens != 0 ||
			usage.UpstreamProviderCostMicroUSD != 0 {
			return errors.New("confirmation synthetic ablation telemetry contains upstream usage")
		}
	}
	return nil
}

type confirmationDimensionWire struct {
	GoEvidenceSHA256 string          `json:"go_evidence_sha256"`
	LatencyMS        uint64          `json:"latency_ms"`
	Evidence         json.RawMessage `json:"evidence"`
}

func marshalConfirmationDimension(evidence any, digest string, latencyMS uint64) (json.RawMessage, error) {
	if !canonicalConfirmationSHA256(digest) || latencyMS == 0 {
		return nil, errors.New("confirmation native dimension metadata is invalid")
	}
	evidenceRaw, err := json.Marshal(evidence)
	if err != nil {
		return nil, err
	}
	wrapper := confirmationDimensionWire{GoEvidenceSHA256: digest, LatencyMS: latencyMS, Evidence: evidenceRaw}
	raw, err := json.Marshal(wrapper)
	if err != nil {
		return nil, err
	}
	var checked confirmationDimensionWire
	if err := decodeStrictConfirmationJSON(raw, &checked); err != nil {
		return nil, err
	}
	if checked.GoEvidenceSHA256 != digest || checked.LatencyMS != latencyMS || !bytes.Equal(checked.Evidence, evidenceRaw) {
		return nil, errors.New("confirmation native dimension wrapper did not round trip exactly")
	}
	return raw, nil
}

func confirmationWireSHA256(result confirmationExecutionResult) (string, error) {
	if result.AblationCoordinatorLatencyMS == 0 {
		return "", errors.New("confirmation coordinator latency is missing")
	}
	dimensionTerms := func(name string, raw json.RawMessage) (string, error) {
		var wrapper confirmationDimensionWire
		if err := decodeStrictConfirmationJSON(raw, &wrapper); err != nil {
			return "", fmt.Errorf("decode %s native wrapper: %w", name, err)
		}
		if !canonicalConfirmationSHA256(wrapper.GoEvidenceSHA256) {
			return "", fmt.Errorf("%s native digest is invalid", name)
		}
		return fmt.Sprintf("%s:%d", wrapper.GoEvidenceSHA256, wrapper.LatencyMS), nil
	}
	longMem, err := dimensionTerms("longmemeval", result.LongMemEval)
	if err != nil {
		return "", err
	}
	inference, err := dimensionTerms("inference", result.InferenceAblation)
	if err != nil {
		return "", err
	}
	embedding, err := dimensionTerms("embedding", result.EmbeddingAblation)
	if err != nil {
		return "", err
	}
	payload := fmt.Sprintf(
		"validator-v9-confirmation-native-wire:v1:%d:%s:%s:%s",
		result.AblationCoordinatorLatencyMS,
		longMem,
		inference,
		embedding,
	)
	return digestBytes([]byte(payload)), nil
}

func decodeAndValidateConfirmationProfile(raw json.RawMessage) (confirmationExecutionProfileWire, json.RawMessage, error) {
	var profile confirmationExecutionProfileWire
	if err := decodeStrictConfirmationJSON(raw, &profile); err != nil {
		return profile, nil, fmt.Errorf("decode confirmation execution profile: %w", err)
	}
	if err := profile.validate(); err != nil {
		return profile, nil, err
	}
	canonical, err := json.Marshal(profile)
	if err != nil {
		return profile, nil, err
	}
	return profile, canonical, nil
}

func (p confirmationExecutionProfileWire) validate() error {
	if p.SchemaVersion != confirmationProfileSchemaVersion || !validRevision(p.Revision) ||
		!canonicalConfirmationSHA256(p.Checksum) {
		return errors.New("confirmation profile identity is invalid")
	}
	if len(p.ProviderLanes) != 2 || !sort.SliceIsSorted(p.ProviderLanes, func(i, j int) bool {
		return p.ProviderLanes[i].Lane < p.ProviderLanes[j].Lane
	}) {
		return errors.New("confirmation profile must contain exactly two canonical provider lanes")
	}
	if p.ProviderLanes[0].Lane != longmemeval.JudgeLane || p.ProviderLanes[1].Lane != longmemeval.ReaderLane {
		return errors.New("confirmation profile must contain exactly the reader and judge provider lanes")
	}
	longMem := p.longMemProfile()
	longMemChecksum, err := longMem.Checksum()
	if err != nil || longMemChecksum != p.LongMemProfileChecksum {
		return errors.New("confirmation LongMem profile checksum mismatch")
	}
	if !longmemeval.IsOfficialCleanedDataset(longMem) {
		return errors.New("confirmation LongMem profile is not the pinned official cleaned dataset")
	}
	if p.LongMemSeedBatchPairs <= 0 {
		return errors.New("confirmation LongMem seed batch size must be positive")
	}
	if !canonicalConfirmationSHA256(p.LongMemProjectionKeySHA256) {
		return errors.New("confirmation LongMem projection key checksum is invalid")
	}
	if p.InferenceAblation.Intervention != string(ablation.InterventionInference) ||
		p.EmbeddingAblation.Intervention != string(ablation.InterventionEmbedding) ||
		p.InferenceAblation.ContractVersion != ablation.ContractVersion ||
		p.EmbeddingAblation.ContractVersion != ablation.ContractVersion {
		return errors.New("confirmation ablation lane contracts are invalid")
	}
	ablationChecksum, err := ablation.FrozenProfileSHA256(p.ablationProfile())
	if err != nil || ablationChecksum != p.AblationProfileChecksum {
		return errors.New("confirmation ablation profile checksum mismatch")
	}
	if err := p.Composite.validate(); err != nil {
		return err
	}
	payloadRaw, err := json.Marshal(p.payload())
	if err != nil || digestBytes(payloadRaw) != p.Checksum {
		return errors.New("confirmation outer profile checksum mismatch")
	}
	return nil
}

func (p confirmationCompositeProfile) validate() error {
	if p.SchemaVersion != confirmationProfileSchemaVersion || !validRevision(p.Revision) ||
		!validRevision(p.FormulaRevision) || p.BaseWeightBPS == 0 || p.LongMemWeightBPS == 0 ||
		p.BaseWeightBPS > math.MaxUint64-p.LongMemWeightBPS || p.BaseWeightBPS+p.LongMemWeightBPS != 10_000 ||
		!canonicalConfirmationSHA256(p.Checksum) {
		return errors.New("confirmation composite profile is invalid")
	}
	withoutChecksum := struct {
		SchemaVersion    int    `json:"schema_version"`
		Revision         string `json:"revision"`
		FormulaRevision  string `json:"formula_revision"`
		BaseWeightBPS    uint64 `json:"base_weight_bps"`
		LongMemWeightBPS uint64 `json:"longmem_weight_bps"`
	}{p.SchemaVersion, p.Revision, p.FormulaRevision, p.BaseWeightBPS, p.LongMemWeightBPS}
	raw, err := json.Marshal(withoutChecksum)
	if err != nil || digestBytes(raw) != p.Checksum {
		return errors.New("confirmation composite profile checksum mismatch")
	}
	return nil
}

func validRevision(value string) bool {
	return value != "" && len(value) <= 128 && strings.TrimSpace(value) == value
}

func checkedAdd(left, right uint64) (uint64, bool) {
	if math.MaxUint64-left < right {
		return 0, false
	}
	return left + right, true
}

func elapsedMilliseconds(start, end time.Time) uint64 {
	if !end.After(start) {
		return 1
	}
	milliseconds := end.Sub(start).Milliseconds()
	if milliseconds < 1 {
		return 1
	}
	return uint64(milliseconds)
}

func digestBytes(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func decodeStrictConfirmationJSON(raw []byte, destination any) error {
	if len(raw) == 0 || len(raw) > confirmationBodyLimit {
		return errors.New("confirmation JSON is outside the size bound")
	}
	if err := rejectDuplicateConfirmationJSONFields(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return errors.New("confirmation JSON contains trailing content")
	}
	return nil
}

func rejectDuplicateConfirmationJSONFields(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := scanConfirmationJSONValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return errors.New("confirmation JSON contains trailing content")
	}
	return nil
}

func scanConfirmationJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("confirmation JSON object key is not a string")
			}
			if _, exists := seen[key]; exists {
				return fmt.Errorf("confirmation JSON contains duplicate field %q", key)
			}
			seen[key] = struct{}{}
			if err := scanConfirmationJSONValue(decoder); err != nil {
				return err
			}
		}
		_, err = decoder.Token()
		return err
	case '[':
		for decoder.More() {
			if err := scanConfirmationJSONValue(decoder); err != nil {
				return err
			}
		}
		_, err = decoder.Token()
		return err
	default:
		return errors.New("confirmation JSON begins with an invalid delimiter")
	}
}
