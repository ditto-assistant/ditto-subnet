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
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/longmemeval"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

const (
	confirmationAblationDatasetSchemaVersion = 1
	maximumConfirmationAblationDatasetBytes  = 16 << 20
	confirmationRuntimeCleanupTimeout        = 30 * time.Second
	maximumConfirmationInstallationBytes     = 2 << 20
)

const (
	confirmationInstallationPathEnv = "DITTOBENCH_V9_CONFIRMATION_INSTALLATION_FILE"
	confirmationInstallationSHAEnv  = "DITTOBENCH_V9_CONFIRMATION_INSTALLATION_SHA256"
)

type confirmationSecretReferenceWire struct {
	ProjectID string `json:"project_id"`
	SecretID  string `json:"secret_id"`
	Version   string `json:"version"`
}

func (reference confirmationSecretReferenceWire) native() longmemeval.SecretManagerReference {
	return longmemeval.SecretManagerReference{
		ProjectID: reference.ProjectID, SecretID: reference.SecretID, Version: reference.Version,
	}
}

type confirmationProviderRuntimeWire struct {
	Lane                string                          `json:"lane"`
	UpstreamURL         string                          `json:"upstream_url"`
	RouteProvider       string                          `json:"route_provider"`
	ReceiptProvider     string                          `json:"receipt_provider"`
	CredentialReference confirmationSecretReferenceWire `json:"credential_reference"`
	RequestTimeoutMS    int64                           `json:"request_timeout_ms"`
}

type confirmationActivationFile struct {
	SchemaVersion                    int                               `json:"schema_version"`
	ExecutionProfile                 json.RawMessage                   `json:"execution_profile"`
	CalibrationManifestSHA256        string                            `json:"calibration_manifest_sha256"`
	CalibrationManifestPath          string                            `json:"calibration_manifest_path"`
	LongMemDatasetPath               string                            `json:"longmem_dataset_path"`
	AblationDatasetPath              string                            `json:"ablation_dataset_path"`
	LongMemProjectionKeyReference    confirmationSecretReferenceWire   `json:"longmem_projection_key_reference"`
	AblationSelectionKeyReference    confirmationSecretReferenceWire   `json:"ablation_selection_key_reference"`
	AblationProjectionKeyReference   confirmationSecretReferenceWire   `json:"ablation_projection_key_reference"`
	ProviderLanes                    []confirmationProviderRuntimeWire `json:"provider_lanes"`
	SandboxHealthTimeoutMilliseconds int64                             `json:"sandbox_health_timeout_ms"`
}

func readConfirmationActivationFile(path, expectedSHA256 string) (confirmationActivationFile, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || !canonicalConfirmationSHA256(expectedSHA256) {
		return confirmationActivationFile{}, errors.New("confirmation installation identity is invalid")
	}
	raw, err := readImmutableConfirmationFile(path, maximumConfirmationInstallationBytes)
	if err != nil || digestBytes(raw) != expectedSHA256 {
		return confirmationActivationFile{}, errors.New("confirmation installation checksum mismatch")
	}
	if err := rejectDuplicateConfirmationJSONFields(raw); err != nil {
		return confirmationActivationFile{}, errors.New("confirmation installation contains duplicate JSON fields")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var installation confirmationActivationFile
	if err := decoder.Decode(&installation); err != nil {
		return confirmationActivationFile{}, errors.New("confirmation installation is malformed")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF || installation.SchemaVersion != 1 {
		return confirmationActivationFile{}, errors.New("confirmation installation schema is invalid")
	}
	return installation, nil
}

// confirmationExecutorFromEnvironment is the sole production activation door.
// Absence is disabled, while any partially configured opt-in is fatal. The
// environment carries only a path and content digest; key/provider credential
// values remain in Secret Manager and are never read during startup.
func confirmationExecutorFromEnvironment(
	getenv func(string) string,
	sandboxRuntime *sandbox.LocalDocker,
	broker *inferenceBroker,
) (confirmationExecutor, error) {
	path := strings.TrimSpace(getenv(confirmationInstallationPathEnv))
	expectedSHA := strings.TrimSpace(getenv(confirmationInstallationSHAEnv))
	if path == "" && expectedSHA == "" {
		return nil, nil
	}
	if path == "" || expectedSHA == "" {
		return nil, errors.New("confirmation installation opt-in is incomplete")
	}
	installation, err := readConfirmationActivationFile(path, expectedSHA)
	if err != nil {
		return nil, err
	}
	profile, _, err := decodeAndValidateConfirmationProfile(installation.ExecutionProfile)
	if err != nil {
		return nil, fmt.Errorf("confirmation installation profile: %w", err)
	}
	calibrationFile, _, err := verifyImmutableConfirmationFile(
		installation.CalibrationManifestPath, maximumConfirmationInstallationBytes,
	)
	if err != nil || calibrationFile.sha256 != installation.CalibrationManifestSHA256 {
		return nil, errors.New("confirmation calibration manifest does not match launch approval")
	}
	authorizer, err := longmemeval.NewGCPSecretManagerAuthorizer(longmemeval.GCPSecretManagerAuthorizerConfig{})
	if err != nil {
		return nil, errors.New("confirmation Secret Manager integration is unavailable")
	}
	lanes := make([]longmemeval.ProviderLaneRuntimeConfig, len(installation.ProviderLanes))
	for index, lane := range installation.ProviderLanes {
		if lane.RequestTimeoutMS <= 0 || lane.RequestTimeoutMS > int64((30*time.Minute)/time.Millisecond) {
			return nil, errors.New("confirmation provider timeout is invalid")
		}
		lanes[index] = longmemeval.ProviderLaneRuntimeConfig{
			Lane: lane.Lane, UpstreamURL: lane.UpstreamURL, RouteProvider: lane.RouteProvider,
			ReceiptProvider: lane.ReceiptProvider, CredentialReference: lane.CredentialReference.native(),
			RequestTimeout: time.Duration(lane.RequestTimeoutMS) * time.Millisecond,
		}
	}
	if installation.SandboxHealthTimeoutMilliseconds <= 0 ||
		installation.SandboxHealthTimeoutMilliseconds > int64((10*time.Minute)/time.Millisecond) {
		return nil, errors.New("confirmation sandbox health timeout is invalid")
	}
	factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
		Profile: profile, Sandbox: sandboxRuntime, Broker: broker,
		CalibrationManifestPath:        installation.CalibrationManifestPath,
		CalibrationManifestSHA256:      installation.CalibrationManifestSHA256,
		LongMemDatasetPath:             installation.LongMemDatasetPath,
		LongMemProjectionKeyReference:  installation.LongMemProjectionKeyReference.native(),
		AblationDatasetPath:            installation.AblationDatasetPath,
		AblationSelectionKeyReference:  installation.AblationSelectionKeyReference.native(),
		AblationProjectionKeyReference: installation.AblationProjectionKeyReference.native(),
		SecretResolver:                 authorizer,
		ProviderRuntime:                longmemeval.ProviderRuntimeConfig{Lanes: lanes, Authorizer: authorizer},
		SandboxHealthTimeout:           time.Duration(installation.SandboxHealthTimeoutMilliseconds) * time.Millisecond,
	})
	if err != nil {
		return nil, err
	}
	return newTrustedConfirmationExecutor(confirmationProfileInstallation{
		ExecutionProfile:          installation.ExecutionProfile,
		CalibrationManifestSHA256: installation.CalibrationManifestSHA256,
	}, factory)
}

// screenedConfirmationRuntimeFactoryConfig is server-owned immutable
// configuration. ProviderRuntime contains Secret Manager references and a
// trusted authorizer, never credential values.
type screenedConfirmationRuntimeFactoryConfig struct {
	Profile                        confirmationExecutionProfileWire
	Sandbox                        sandbox.Sandbox
	Broker                         *inferenceBroker
	CalibrationManifestPath        string
	CalibrationManifestSHA256      string
	LongMemDatasetPath             string
	LongMemProjectionKeyReference  longmemeval.SecretManagerReference
	AblationDatasetPath            string
	AblationSelectionKeyReference  longmemeval.SecretManagerReference
	AblationProjectionKeyReference longmemeval.SecretManagerReference
	SecretResolver                 longmemeval.SecretBytesResolver
	ProviderRuntime                longmemeval.ProviderRuntimeConfig
	SandboxHealthTimeout           time.Duration
}

type screenedConfirmationRuntimeFactory struct {
	profile                        confirmationExecutionProfileWire
	sandbox                        sandbox.Sandbox
	broker                         *inferenceBroker
	calibrationManifest            verifiedConfirmationFile
	longMemDataset                 verifiedConfirmationFile
	longMemProjectionKeyReference  longmemeval.SecretManagerReference
	ablationFile                   verifiedConfirmationFile
	ablationDataset                confirmationAblationDataset
	ablationSelectionKeyReference  longmemeval.SecretManagerReference
	ablationProjectionKeyReference longmemeval.SecretManagerReference
	secretResolver                 longmemeval.SecretBytesResolver
	providerRuntime                longmemeval.ProviderRuntimeConfig
	healthTimeout                  time.Duration
}

type verifiedConfirmationFile struct {
	path   string
	sha256 string
}

func verifyConfirmationFile(path string, maximum int64) (verifiedConfirmationFile, []byte, error) {
	if strings.TrimSpace(path) == "" {
		return verifiedConfirmationFile{}, nil, errors.New("confirmation dataset path is required")
	}
	file, err := os.Open(path)
	if err != nil {
		return verifiedConfirmationFile{}, nil, errors.New("confirmation dataset is unavailable")
	}
	defer file.Close()
	limited := io.LimitReader(file, maximum+1)
	raw, err := io.ReadAll(limited)
	if err != nil || len(raw) == 0 || int64(len(raw)) > maximum {
		return verifiedConfirmationFile{}, nil, errors.New("confirmation dataset has an invalid size")
	}
	digest := sha256.Sum256(raw)
	return verifiedConfirmationFile{path: path, sha256: hex.EncodeToString(digest[:])}, raw, nil
}

func verifyImmutableConfirmationFile(path string, maximum int64) (verifiedConfirmationFile, []byte, error) {
	raw, err := readImmutableConfirmationFile(path, maximum)
	if err != nil {
		return verifiedConfirmationFile{}, nil, err
	}
	digest := sha256.Sum256(raw)
	return verifiedConfirmationFile{path: path, sha256: hex.EncodeToString(digest[:])}, raw, nil
}

func readImmutableConfirmationFile(path string, maximum int64) ([]byte, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || maximum <= 0 {
		return nil, errors.New("confirmation immutable file path is invalid")
	}
	identity, err := os.Lstat(path)
	if err != nil || !identity.Mode().IsRegular() || identity.Mode()&os.ModeSymlink != 0 ||
		identity.Size() <= 0 || identity.Size() > maximum {
		return nil, errors.New("confirmation immutable file is unavailable")
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, errors.New("confirmation immutable file is unavailable")
	}
	defer file.Close()
	openedIdentity, err := file.Stat()
	if err != nil || !openedIdentity.Mode().IsRegular() || !os.SameFile(identity, openedIdentity) ||
		openedIdentity.Size() != identity.Size() {
		return nil, errors.New("confirmation immutable file identity changed")
	}
	raw, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || len(raw) == 0 || int64(len(raw)) != openedIdentity.Size() || int64(len(raw)) > maximum {
		return nil, errors.New("confirmation immutable file changed while reading")
	}
	return raw, nil
}

func (file verifiedConfirmationFile) open() (io.ReadSeekCloser, error) {
	opened, err := os.Open(file.path)
	if err != nil {
		return nil, errors.New("confirmation dataset is unavailable")
	}
	return opened, nil
}

func (file verifiedConfirmationFile) openVerified(maximum int64) (io.ReadSeekCloser, error) {
	opened, err := os.Open(file.path)
	if err != nil {
		return nil, errors.New("confirmation dataset is unavailable")
	}
	limited := &hardLimitReadSeeker{reader: opened, remaining: maximum + 1}
	digest := sha256.New()
	written, copyErr := io.Copy(digest, limited)
	if copyErr != nil || written == 0 || written > maximum || hex.EncodeToString(digest.Sum(nil)) != file.sha256 {
		_ = opened.Close()
		return nil, errors.New("confirmation dataset changed after installation")
	}
	if _, err := opened.Seek(0, io.SeekStart); err != nil {
		_ = opened.Close()
		return nil, errors.New("confirmation dataset cannot be rewound")
	}
	return opened, nil
}

type hardLimitReadSeeker struct {
	reader    io.Reader
	remaining int64
}

func (reader *hardLimitReadSeeker) Read(destination []byte) (int, error) {
	if reader.remaining <= 0 {
		return 0, io.EOF
	}
	if int64(len(destination)) > reader.remaining {
		destination = destination[:reader.remaining]
	}
	read, err := reader.reader.Read(destination)
	reader.remaining -= int64(read)
	return read, err
}

// confirmationAblationDataset is a frozen, content-addressed set of ordinary
// memory cases. All grading fields remain trusted-side only.
type confirmationAblationDataset struct {
	SchemaVersion int                        `json:"schema_version"`
	Revision      string                     `json:"revision"`
	Cases         []confirmationAblationCase `json:"cases"`
	byID          map[string]confirmationAblationCase
}

type confirmationAblationCase struct {
	CaseID            string                  `json:"case_id"`
	UserID            string                  `json:"user_id"`
	SystemPrompt      string                  `json:"system_prompt"`
	Question          string                  `json:"question"`
	QuestionType      string                  `json:"question_type"`
	ExpectedAnswer    string                  `json:"expected_answer"`
	AnswerKind        string                  `json:"answer_kind,omitempty"`
	AcceptAny         []string                `json:"accept_any,omitempty"`
	DistractorAnswers []string                `json:"distractor_answers,omitempty"`
	ForbiddenAnswer   string                  `json:"forbidden_answer,omitempty"`
	SeedBatches       [][]protocol.MemoryPair `json:"seed_batches"`
}

func decodeConfirmationAblationDataset(raw []byte) (confirmationAblationDataset, error) {
	if err := rejectDuplicateConfirmationJSONFields(raw); err != nil {
		return confirmationAblationDataset{}, errors.New("confirmation ablation dataset contains duplicate JSON fields")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var dataset confirmationAblationDataset
	if err := decoder.Decode(&dataset); err != nil {
		return confirmationAblationDataset{}, errors.New("confirmation ablation dataset is malformed")
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return confirmationAblationDataset{}, errors.New("confirmation ablation dataset contains trailing JSON")
	}
	if dataset.SchemaVersion != confirmationAblationDatasetSchemaVersion || strings.TrimSpace(dataset.Revision) == "" || len(dataset.Cases) == 0 {
		return confirmationAblationDataset{}, errors.New("confirmation ablation dataset identity is incomplete")
	}
	dataset.byID = make(map[string]confirmationAblationCase, len(dataset.Cases))
	for _, item := range dataset.Cases {
		if strings.TrimSpace(item.CaseID) == "" || strings.TrimSpace(item.CaseID) != item.CaseID ||
			strings.TrimSpace(item.UserID) == "" || strings.TrimSpace(item.UserID) != item.UserID ||
			strings.TrimSpace(item.SystemPrompt) == "" || strings.TrimSpace(item.Question) == "" ||
			strings.TrimSpace(item.QuestionType) == "" || strings.TrimSpace(item.ExpectedAnswer) == "" || len(item.SeedBatches) == 0 {
			return confirmationAblationDataset{}, errors.New("confirmation ablation case is incomplete")
		}
		if _, duplicate := dataset.byID[item.CaseID]; duplicate {
			return confirmationAblationDataset{}, errors.New("confirmation ablation case identity is duplicated")
		}
		pairs := 0
		for _, batch := range item.SeedBatches {
			if len(batch) == 0 {
				return confirmationAblationDataset{}, errors.New("confirmation ablation seed batch is empty")
			}
			for _, pair := range batch {
				if strings.TrimSpace(pair.PairID) == "" || strings.TrimSpace(pair.SessionID) == "" ||
					strings.TrimSpace(pair.Timestamp) == "" || (strings.TrimSpace(pair.Prompt) == "" && strings.TrimSpace(pair.Response) == "") {
					return confirmationAblationDataset{}, errors.New("confirmation ablation seed pair is incomplete")
				}
				if _, err := time.Parse(time.RFC3339, pair.Timestamp); err != nil {
					return confirmationAblationDataset{}, errors.New("confirmation ablation seed timestamp is invalid")
				}
				pairs++
			}
		}
		if pairs == 0 {
			return confirmationAblationDataset{}, errors.New("confirmation ablation case has no seed pairs")
		}
		dataset.byID[item.CaseID] = item
	}
	return dataset, nil
}

func newScreenedConfirmationRuntimeFactory(config screenedConfirmationRuntimeFactoryConfig) (*screenedConfirmationRuntimeFactory, error) {
	if nilInterface(config.Sandbox) || config.Broker == nil {
		return nil, errors.New("confirmation sandbox and broker are required")
	}
	localDocker, concrete := config.Sandbox.(*sandbox.LocalDocker)
	if !concrete || localDocker == nil || !localDocker.Harden || !localDocker.RequireRootless ||
		!validConfirmationNetworkName(localDocker.EgressNetwork) || !validConfirmationEgressProxy(localDocker.EgressProxy) {
		return nil, errors.New("confirmation sandbox lacks production isolation")
	}
	if nilInterface(config.SecretResolver) {
		return nil, errors.New("confirmation Secret Manager resolver is required")
	}
	if config.SandboxHealthTimeout <= 0 {
		return nil, errors.New("confirmation sandbox health timeout must be positive")
	}
	calibrationFile, _, err := verifyImmutableConfirmationFile(config.CalibrationManifestPath, maximumConfirmationInstallationBytes)
	if err != nil || calibrationFile.sha256 != config.CalibrationManifestSHA256 {
		return nil, errors.New("confirmation calibration manifest does not match launch approval")
	}
	longMemFile, _, err := verifyConfirmationFile(config.LongMemDatasetPath, 384<<20)
	if err != nil {
		return nil, err
	}
	ablationFile, ablationRaw, err := verifyConfirmationFile(config.AblationDatasetPath, maximumConfirmationAblationDatasetBytes)
	if err != nil {
		return nil, err
	}
	dataset, err := decodeConfirmationAblationDataset(ablationRaw)
	if err != nil {
		return nil, err
	}
	factory := &screenedConfirmationRuntimeFactory{
		profile: config.Profile, sandbox: config.Sandbox, broker: config.Broker,
		calibrationManifest: calibrationFile,
		longMemDataset:      longMemFile, longMemProjectionKeyReference: config.LongMemProjectionKeyReference,
		ablationFile: ablationFile, ablationDataset: dataset,
		ablationSelectionKeyReference:  config.AblationSelectionKeyReference,
		ablationProjectionKeyReference: config.AblationProjectionKeyReference,
		secretResolver:                 config.SecretResolver, providerRuntime: config.ProviderRuntime,
		healthTimeout: config.SandboxHealthTimeout,
	}
	if err := factory.ValidateInstallation(config.Profile); err != nil {
		return nil, err
	}
	// Construction validates every route, model, provider, cap, immutable
	// Secret Manager reference, and official judge identity without retrieving a
	// credential or making an upstream request.
	if err := longmemeval.ValidateProviderRuntimeConfig(config.Profile.longMemProfile(), config.ProviderRuntime); err != nil {
		return nil, fmt.Errorf("confirmation provider runtime is invalid: %w", err)
	}
	return factory, nil
}

func validConfirmationNetworkName(value string) bool {
	return value != "" && strings.TrimSpace(value) == value && len(value) <= 128 && !strings.ContainsAny(value, "\r\n\x00")
}

func validConfirmationEgressProxy(raw string) bool {
	parsed, err := url.Parse(raw)
	return err == nil && (parsed.Scheme == "http" || parsed.Scheme == "https") && parsed.Host != "" &&
		parsed.User == nil && parsed.RawQuery == "" && parsed.Fragment == ""
}

func (factory *screenedConfirmationRuntimeFactory) ValidateInstallation(profile confirmationExecutionProfileWire) error {
	if factory == nil || nilInterface(factory.sandbox) || factory.broker == nil || factory.healthTimeout <= 0 {
		return errors.New("confirmation runtime factory is incomplete")
	}
	if !confirmationEmbeddingRouteIsLocal(factory.broker.embeddingURL) {
		return errors.New("confirmation local embedding route is unavailable")
	}
	if file, _, fileErr := verifyImmutableConfirmationFile(factory.calibrationManifest.path, maximumConfirmationInstallationBytes); fileErr != nil ||
		file.sha256 != factory.calibrationManifest.sha256 {
		return errors.New("confirmation calibration manifest drift")
	}
	if !reflect.DeepEqual(profile, factory.profile) {
		return errors.New("confirmation runtime profile drift")
	}
	longMem := profile.longMemProfile()
	checksum, err := longMem.Checksum()
	if err != nil || checksum != profile.LongMemProfileChecksum ||
		factory.longMemDataset.sha256 != profile.LongMemDatasetSHA256 {
		return errors.New("confirmation LongMemEval installation drift")
	}
	if file, _, fileErr := verifyConfirmationFile(factory.longMemDataset.path, 384<<20); fileErr != nil ||
		file.sha256 != profile.LongMemDatasetSHA256 {
		return errors.New("confirmation LongMemEval dataset drift")
	}
	if !longmemeval.IsOfficialCleanedDataset(longMem) {
		return errors.New("confirmation LongMemEval dataset is not the official cleaned condition")
	}
	if factory.ablationDataset.byID == nil || factory.longMemDataset.path == "" {
		return errors.New("confirmation dataset installation is incomplete")
	}
	for _, reference := range []longmemeval.SecretManagerReference{
		factory.longMemProjectionKeyReference,
		factory.ablationSelectionKeyReference,
		factory.ablationProjectionKeyReference,
	} {
		if err := longmemeval.ValidateSecretManagerReference(reference); err != nil {
			return errors.New("confirmation key must use an immutable Secret Manager reference")
		}
	}
	if file, _, fileErr := verifyConfirmationFile(factory.ablationFile.path, maximumConfirmationAblationDatasetBytes); fileErr != nil ||
		file.sha256 != profile.AblationDatasetSHA256 {
		return errors.New("confirmation ablation dataset drift")
	}
	if len(factory.ablationDataset.Cases) < profile.AblationCoordinatorPolicy.SampleSize {
		return errors.New("confirmation ablation population is undersized")
	}
	ablationChecksum, err := ablation.FrozenProfileSHA256(profile.ablationProfile())
	if err != nil || ablationChecksum != profile.AblationProfileChecksum {
		return errors.New("confirmation ablation profile drift")
	}
	if err := longmemeval.ValidateProviderRuntimeConfig(longMem, factory.providerRuntime); err != nil {
		return fmt.Errorf("confirmation provider runtime drift: %w", err)
	}
	return nil
}

func confirmationEmbeddingRouteIsLocal(raw string) bool {
	configured := configuredEmbeddingURL(raw)
	parsed, err := url.Parse(configured)
	if err != nil || configured == "" || parsed.Scheme != "http" {
		return false
	}
	host := parsed.Hostname()
	if strings.EqualFold(host, "localhost") || strings.EqualFold(host, "host.docker.internal") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func resolveConfirmationSecret(
	ctx context.Context,
	resolver longmemeval.SecretBytesResolver,
	reference longmemeval.SecretManagerReference,
) ([]byte, error) {
	resolved, err := resolver.Resolve(ctx, reference)
	if err != nil {
		return nil, errors.New("confirmation key is unavailable from Secret Manager")
	}
	defer longmemeval.ZeroSecretBytes(resolved)
	if len(resolved) < 32 || len(resolved) > 1024 {
		return nil, errors.New("confirmation key has an invalid size")
	}
	return append([]byte(nil), resolved...), nil
}

func validateScreenedConfirmationSource(identity confirmationRuntimeIdentity) error {
	source := identity.Source
	if !canonicalConfirmationSHA256(identity.ArtifactSHA256) || identity.ArtifactSHA256 != source.TarballSHA256 ||
		strings.TrimSpace(source.TarballURL) == "" || source.GitURL != "" || source.GitRef != "" || source.GitSubdir != "" {
		return errors.New("confirmation source provenance is incomplete")
	}
	if !canonicalConfirmationSHA256(source.ScreenedImageSHA256) || strings.TrimSpace(source.ScreenedImageURL) == "" ||
		strings.TrimSpace(source.ScreenedImageID) == "" || strings.TrimSpace(source.ScreenedImageRef) == "" || source.ScreenedImageSize <= 0 {
		return errors.New("confirmation screened image identity is incomplete")
	}
	for _, raw := range []string{source.TarballURL, source.ScreenedImageURL} {
		parsed, err := url.Parse(raw)
		if err != nil || parsed.Scheme != "https" || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
			return errors.New("confirmation artifact URL is invalid")
		}
	}
	if identity.BundleID == "" || identity.TicketID == "" || identity.AgentID == "" || identity.SlotID == "" ||
		identity.Deadline.IsZero() || !identity.Deadline.After(time.Now()) {
		return errors.New("confirmation runtime identity is incomplete")
	}
	return nil
}

func (factory *screenedConfirmationRuntimeFactory) installBrokerSession(
	identity confirmationRuntimeIdentity,
	provider *longmemeval.ProviderSession,
) (string, string, error) {
	sessionID, err := randomToken(18)
	if err != nil {
		return "", "", errors.New("confirmation broker session identity is unavailable")
	}
	runID := uuid.NewString()
	readerPolicy := factory.profile.longMemProfile().Providers[0]
	for _, lane := range factory.profile.longMemProfile().Providers {
		if lane.Lane == longmemeval.ReaderLane {
			readerPolicy = lane
		}
	}
	session := &brokerSession{
		id: sessionID, trustedChatHandler: provider.ReaderHandler(), expiresAt: identity.Deadline,
		provider: readerPolicy.Provider, model: readerPolicy.Model, requestModel: readerPolicy.Model,
		profileRevision: readerPolicy.ProfileRevision, boundRunID: runID, benchVersion: confirmationBenchVersion,
		confirmationSession: true, embeddingConcurrency: 1,
		embeddingCalls: make(map[chan struct{}]context.CancelFunc), cancels: make(map[string]context.CancelFunc),
	}
	factory.broker.mu.Lock()
	if len(factory.broker.sessions) >= factory.broker.maxSessions {
		factory.broker.mu.Unlock()
		return "", "", errors.New("confirmation broker is at capacity")
	}
	factory.broker.sessions[sessionID] = session
	factory.broker.mu.Unlock()
	return sessionID, runID, nil
}

func (factory *screenedConfirmationRuntimeFactory) Acquire(
	ctx context.Context,
	identity confirmationRuntimeIdentity,
) (*confirmationRuntime, error) {
	if ctx == nil {
		return nil, errors.New("confirmation runtime context is required")
	}
	if err := factory.ValidateInstallation(factory.profile); err != nil {
		return nil, err
	}
	return factory.acquireAfterInstallationValidation(ctx, identity)
}

// acquireAfterInstallationValidation owns the stateful runtime phase after the
// caller has established the immutable installation. Production must enter via
// Acquire; hermetic tests use this phase boundary with synthetic datasets.
func (factory *screenedConfirmationRuntimeFactory) acquireAfterInstallationValidation(
	ctx context.Context,
	identity confirmationRuntimeIdentity,
) (*confirmationRuntime, error) {
	if ctx == nil {
		return nil, errors.New("confirmation runtime context is required")
	}
	if err := validateScreenedConfirmationSource(identity); err != nil {
		return nil, err
	}
	if err := factory.sandbox.Available(ctx); err != nil {
		return nil, errors.New("confirmation sandbox is unavailable")
	}
	longMemSource, err := factory.longMemDataset.openVerified(384 << 20)
	if err != nil {
		return nil, err
	}
	_, ablationRaw, err := verifyConfirmationFile(factory.ablationFile.path, maximumConfirmationAblationDatasetBytes)
	if err != nil || digestBytes(ablationRaw) != factory.profile.AblationDatasetSHA256 {
		_ = longMemSource.Close()
		return nil, errors.New("confirmation ablation dataset changed after installation")
	}
	ablationDataset, err := decodeConfirmationAblationDataset(ablationRaw)
	if err != nil || ablationDataset.Revision != factory.ablationDataset.Revision {
		_ = longMemSource.Close()
		return nil, errors.New("confirmation ablation dataset identity drift")
	}
	longMemProjectionKey, err := resolveConfirmationSecret(ctx, factory.secretResolver, factory.longMemProjectionKeyReference)
	if err != nil {
		_ = longMemSource.Close()
		return nil, err
	}
	zeroKeys := true
	ablationSelectionKey, err := resolveConfirmationSecret(ctx, factory.secretResolver, factory.ablationSelectionKeyReference)
	if err != nil {
		longmemeval.ZeroSecretBytes(longMemProjectionKey)
		_ = longMemSource.Close()
		return nil, err
	}
	ablationProjectionKey, err := resolveConfirmationSecret(ctx, factory.secretResolver, factory.ablationProjectionKeyReference)
	if err != nil {
		longmemeval.ZeroSecretBytes(longMemProjectionKey)
		longmemeval.ZeroSecretBytes(ablationSelectionKey)
		_ = longMemSource.Close()
		return nil, err
	}
	defer func() {
		if zeroKeys {
			longmemeval.ZeroSecretBytes(longMemProjectionKey)
			longmemeval.ZeroSecretBytes(ablationSelectionKey)
			longmemeval.ZeroSecretBytes(ablationProjectionKey)
		}
	}()
	if digestBytes(longMemProjectionKey) != factory.profile.LongMemProjectionKeySHA256 ||
		digestBytes(ablationSelectionKey) != factory.profile.AblationSelectionKeySHA256 ||
		digestBytes(ablationProjectionKey) != factory.profile.AblationProjectionKeySHA256 {
		_ = longMemSource.Close()
		return nil, errors.New("confirmation key does not match the installed profile")
	}
	provider, err := longmemeval.NewProviderSession(ctx, factory.profile.longMemProfile(), factory.providerRuntime)
	if err != nil {
		_ = longMemSource.Close()
		return nil, errors.New("confirmation provider session is unavailable")
	}
	image, _, _, err := factory.sandbox.Build(ctx, identity.Source)
	if err != nil || strings.TrimSpace(image) == "" {
		_ = provider.Close()
		_ = longMemSource.Close()
		return nil, errors.New("confirmation screened image is unavailable")
	}
	cleanupImage := true
	defer func() {
		if cleanupImage {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
			factory.sandbox.Release(cleanupCtx, image)
			cancel()
		}
	}()
	sessionID, runID, err := factory.installBrokerSession(identity, provider)
	if err != nil {
		_ = provider.Close()
		_ = longMemSource.Close()
		return nil, err
	}
	cleanupSession := true
	defer func() {
		if cleanupSession {
			factory.broker.removeRun(sessionID, runID)
			_ = provider.Close()
			_ = longMemSource.Close()
		}
	}()
	env := harnessSandboxEnv(nil, confirmationBenchVersion, sessionID)
	handle, err := factory.sandbox.Run(ctx, image, env)
	if err != nil || handle == nil || net.ParseIP(handle.SourceIP) == nil || strings.TrimSpace(handle.BaseURL) == "" {
		if handle != nil {
			factory.stopHandle(handle)
		}
		return nil, errors.New("confirmation LongMemEval sandbox could not start")
	}
	if !factory.broker.bindSource(sessionID, runID, handle.SourceIP) || !factory.broker.beginEmbeddingPhase(sessionID, runID) {
		factory.stopHandle(handle)
		return nil, errors.New("confirmation sandbox source binding failed")
	}
	healthCtx := runner.TrustSandbox(ctx)
	if err := runner.WaitHealthy(healthCtx, handle.BaseURL, factory.healthTimeout); err != nil {
		factory.stopHandle(handle)
		return nil, errors.New("confirmation LongMemEval harness did not become healthy")
	}
	harnessClient := &http.Client{
		Transport:     http.DefaultTransport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	harness, err := longmemeval.NewHTTPHarness(handle.BaseURL, harnessClient)
	if err != nil {
		factory.stopHandle(handle)
		return nil, err
	}
	caseRunner := &screenedAblationCaseRunner{
		sandbox: factory.sandbox, broker: factory.broker, image: image, sessionID: sessionID, runID: runID,
		healthTimeout: factory.healthTimeout, dataset: ablationDataset, current: handle,
		boundSourceIP: handle.SourceIP,
	}
	closer := &confirmationRuntimeCloser{
		sandbox: factory.sandbox, broker: factory.broker, image: image, sessionID: sessionID, runID: runID,
		provider: provider, source: longMemSource, runner: caseRunner,
		secretBuffers: [][]byte{longMemProjectionKey, ablationSelectionKey, ablationProjectionKey},
	}
	population := make([]ablation.EligibleCase, len(ablationDataset.Cases))
	for index, item := range ablationDataset.Cases {
		population[index] = ablation.EligibleCase{CaseID: item.CaseID, UserID: item.UserID}
	}
	cleanupImage = false
	cleanupSession = false
	zeroKeys = false
	return &confirmationRuntime{
		LongMemSource: longMemSource, LongMemHarness: harness, LongMemJudge: provider.Judge(), LongMemMeter: provider,
		LongMemProjectionKey:  longMemProjectionKey,
		AblationPopulation:    ablation.EligiblePopulation{BenchVersion: confirmationBenchVersion, Confirmation: true, Cases: population},
		AblationCaseRunner:    caseRunner,
		AblationSelectionKey:  ablationSelectionKey,
		AblationProjectionKey: ablationProjectionKey,
		Close:                 closer.Close,
	}, nil
}

func (factory *screenedConfirmationRuntimeFactory) stopHandle(handle *sandbox.Handle) {
	if handle == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	factory.sandbox.Stop(ctx, handle)
	cancel()
}

type screenedAblationCaseRunner struct {
	mu            sync.Mutex
	sandbox       sandbox.Sandbox
	broker        *inferenceBroker
	image         string
	sessionID     string
	runID         string
	healthTimeout time.Duration
	dataset       confirmationAblationDataset
	current       *sandbox.Handle
	boundSourceIP string
	closed        bool
}

func confirmationOpaqueUUID(domain string, values ...string) string {
	hash := sha256.New()
	_, _ = hash.Write([]byte("ditto-v9-confirmation-ablation-runtime-v1"))
	_, _ = hash.Write([]byte{0})
	_, _ = hash.Write([]byte(domain))
	for _, value := range values {
		_, _ = hash.Write([]byte{0})
		_, _ = hash.Write([]byte(value))
	}
	raw := hash.Sum(nil)[:16]
	raw[6] = (raw[6] & 0x0f) | 0x40
	raw[8] = (raw[8] & 0x3f) | 0x80
	return uuid.Must(uuid.FromBytes(raw)).String()
}

func projectConfirmationAblationSeeds(item confirmationAblationCase, namespace string) []protocol.SeedRequest {
	userID := confirmationOpaqueUUID("user", namespace)
	result := make([]protocol.SeedRequest, len(item.SeedBatches))
	for batchIndex, batch := range item.SeedBatches {
		pairs := make([]protocol.MemoryPair, len(batch))
		for pairIndex, pair := range batch {
			// Pair/session identities stay equal across the three fresh containers
			// so replay sees the same selected content. Isolation comes from the
			// fresh database and lane-derived user namespace, not shared storage.
			pair.PairID = confirmationOpaqueUUID("pair", item.CaseID, pair.PairID)
			pair.SessionID = confirmationOpaqueUUID("session", item.CaseID, pair.SessionID)
			pairs[pairIndex] = pair
		}
		result[batchIndex] = protocol.SeedRequest{
			UserID: userID, Pairs: pairs, Subjects: []protocol.Subject{}, Links: []protocol.SubjectLink{},
		}
	}
	return result
}

func (runnerAdapter *screenedAblationCaseRunner) stopCurrentLocked() {
	if runnerAdapter.current == nil {
		return
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	runnerAdapter.sandbox.Stop(cleanupCtx, runnerAdapter.current)
	cancel()
	runnerAdapter.current = nil
}

func (runnerAdapter *screenedAblationCaseRunner) RunCase(
	ctx context.Context,
	request ablation.RunRequest,
) (ablation.CaseRunResult, error) {
	if ctx == nil {
		return ablation.CaseRunResult{}, errors.New("ablation case context is required")
	}
	runnerAdapter.mu.Lock()
	defer runnerAdapter.mu.Unlock()
	if runnerAdapter.closed || nilInterface(runnerAdapter.sandbox) || runnerAdapter.broker == nil {
		return ablation.CaseRunResult{}, errors.New("confirmation ablation runner is closed")
	}
	item, ok := runnerAdapter.dataset.byID[request.CaseID]
	if !ok || strings.TrimSpace(request.OpaqueUserNamespace) == "" {
		return ablation.CaseRunResult{}, errors.New("confirmation ablation case is unavailable")
	}
	// The stable LongMem container, or the previous attempt container, is fully
	// stopped before a new responder capability is leased.
	runnerAdapter.stopCurrentLocked()
	handle, err := runnerAdapter.sandbox.Run(ctx, runnerAdapter.image,
		harnessSandboxEnv(nil, confirmationBenchVersion, runnerAdapter.sessionID))
	if err != nil || handle == nil || net.ParseIP(handle.SourceIP) == nil || strings.TrimSpace(handle.BaseURL) == "" {
		if handle != nil {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
			runnerAdapter.sandbox.Stop(cleanupCtx, handle)
			cancel()
		}
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation sandbox could not start"))
	}
	// Lease before admitting the new source. A hostile image can boot, but every
	// broker request is rejected until the exact scope is already installed.
	lease, err := runnerAdapter.broker.beginAblationCase(runnerAdapter.sessionID, runnerAdapter.runID, request)
	if err != nil {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
		runnerAdapter.sandbox.Stop(cleanupCtx, handle)
		cancel()
		return ablation.CaseRunResult{}, err
	}
	finish := func() {
		// Stop synchronously while the capability is still installed. This
		// prevents a background request from falling through after revocation.
		cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
		runnerAdapter.sandbox.Stop(cleanupCtx, handle)
		cancel()
		lease.Close()
	}
	if !runnerAdapter.broker.replaceBoundSource(
		runnerAdapter.sessionID, runnerAdapter.runID, runnerAdapter.boundSourceIP, handle.SourceIP,
	) {
		finish()
		return ablation.CaseRunResult{}, errors.New("confirmation ablation source binding failed")
	}
	runnerAdapter.boundSourceIP = handle.SourceIP
	runnerAdapter.current = handle
	healthCtx := runner.TrustSandbox(ctx)
	if err := runner.WaitHealthy(healthCtx, handle.BaseURL, runnerAdapter.healthTimeout); err != nil {
		finish()
		runnerAdapter.current = nil
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation harness did not become healthy"))
	}
	harness, err := longmemeval.NewHTTPHarness(handle.BaseURL, &http.Client{
		Transport:     http.DefaultTransport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	})
	if err != nil {
		finish()
		runnerAdapter.current = nil
		return ablation.CaseRunResult{}, err
	}
	seeds := projectConfirmationAblationSeeds(item, request.OpaqueUserNamespace)
	for _, seed := range seeds {
		if _, err := harness.Seed(ctx, seed); err != nil {
			finish()
			runnerAdapter.current = nil
			return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation seed failed"))
		}
	}
	userID := confirmationOpaqueUUID("user", request.OpaqueUserNamespace)
	caseID := confirmationOpaqueUUID("case", item.CaseID)
	response, err := harness.Run(ctx, protocol.RunRequest{
		CaseID: caseID, SystemPrompt: item.SystemPrompt, UserInput: item.Question,
		Tools: longmemeval.NativeMemoryTools(), BenchVersion: confirmationBenchVersion, UserID: userID,
	})
	finish()
	runnerAdapter.current = nil
	if err != nil {
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation case failed"))
	}
	score := scorer.GradeMemory(protocol.MemoryCase{
		BenchVersion: confirmationBenchVersion, ID: caseID, QuestionID: item.CaseID,
		QuestionType: item.QuestionType, Question: item.Question, ExpectedAnswer: item.ExpectedAnswer,
		AnswerKind: item.AnswerKind, AcceptAny: append([]string(nil), item.AcceptAny...),
		DistractorAnswers: append([]string(nil), item.DistractorAnswers...), ForbiddenAnswer: item.ForbiddenAnswer,
	}, response)
	return ablation.CaseRunResult{Score: score.Score}, nil
}

type confirmationRuntimeCloser struct {
	once          sync.Once
	err           error
	sandbox       sandbox.Sandbox
	broker        *inferenceBroker
	image         string
	sessionID     string
	runID         string
	provider      *longmemeval.ProviderSession
	source        io.Closer
	runner        *screenedAblationCaseRunner
	secretBuffers [][]byte
}

func (closer *confirmationRuntimeCloser) Close() error {
	closer.once.Do(func() {
		if closer.runner != nil {
			closer.runner.mu.Lock()
			closer.runner.closed = true
			closer.runner.stopCurrentLocked()
			closer.runner.mu.Unlock()
		}
		closer.broker.endEmbeddingPhase(closer.sessionID, closer.runID)
		closer.broker.removeRun(closer.sessionID, closer.runID)
		var failures []error
		if err := closer.provider.Close(); err != nil {
			failures = append(failures, errors.New("close confirmation provider session"))
		}
		if err := closer.source.Close(); err != nil {
			failures = append(failures, errors.New("close confirmation dataset"))
		}
		cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
		closer.sandbox.Release(cleanupCtx, closer.image)
		cancel()
		for _, value := range closer.secretBuffers {
			longmemeval.ZeroSecretBytes(value)
		}
		closer.err = errors.Join(failures...)
	})
	return closer.err
}

var _ confirmationRuntimeFactory = (*screenedConfirmationRuntimeFactory)(nil)
var _ ablation.CaseRunner = (*screenedAblationCaseRunner)(nil)
