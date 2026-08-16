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

type confirmationActivationFile struct {
	SchemaVersion                    int             `json:"schema_version"`
	ExecutionProfile                 json.RawMessage `json:"execution_profile"`
	LaunchManifestSHA256             string          `json:"launch_manifest_sha256"`
	LaunchManifestPath               string          `json:"launch_manifest_path"`
	LongMemDatasetPath               string          `json:"longmem_dataset_path"`
	AblationDatasetPath              string          `json:"ablation_dataset_path"`
	SandboxHealthTimeoutMilliseconds int64           `json:"sandbox_health_timeout_ms"`
}

type confirmationSourceBinding struct {
	mu         sync.Mutex
	sourceIP   string
	capability string
	revoked    bool
}

func (binding *confirmationSourceBinding) bind(
	broker *inferenceBroker,
	sessionID string,
	runID string,
	newSourceIP string,
	capability string,
) bool {
	if binding == nil {
		return false
	}
	binding.mu.Lock()
	defer binding.mu.Unlock()
	if binding.sourceIP != "" || binding.capability != "" || binding.revoked ||
		!broker.bindSourceCapability(sessionID, runID, newSourceIP, capability) {
		return false
	}
	binding.sourceIP = newSourceIP
	binding.capability = capability
	binding.revoked = false
	return true
}

func (binding *confirmationSourceBinding) revoke(
	broker *inferenceBroker,
	sessionID string,
	runID string,
) bool {
	if binding == nil {
		return false
	}
	binding.mu.Lock()
	defer binding.mu.Unlock()
	if binding.sourceIP == "" {
		return false
	}
	if binding.revoked && binding.capability == "" {
		return true
	}
	if binding.revoked || binding.capability == "" ||
		!broker.revokeSourceCapability(sessionID, runID, binding.capability) {
		return false
	}
	binding.capability = ""
	binding.revoked = true
	return true
}

func (binding *confirmationSourceBinding) unbind(
	broker *inferenceBroker,
	sessionID string,
	runID string,
	sourceIP string,
) bool {
	if binding == nil {
		return false
	}
	binding.mu.Lock()
	defer binding.mu.Unlock()
	if binding.sourceIP != sourceIP || binding.capability != "" || !binding.revoked ||
		!broker.unbindSource(sessionID, runID, sourceIP) {
		return false
	}
	binding.sourceIP = ""
	binding.revoked = false
	return true
}

// confirmationLongMemHarness runs every selected LongMem case in a fresh
// submitted container. All of that case's seed waves and its final /run share
// one source-bound broker generation; the process is synchronously stopped
// before that generation is closed. This process boundary prevents a delayed
// goroutine from case N waking after case N+1 has begun and borrowing its
// trusted provider activity.
type confirmationLongMemHarness struct {
	mu            sync.Mutex
	sandbox       sandbox.Sandbox
	broker        *inferenceBroker
	image         string
	sessionID     string
	runID         string
	healthTimeout time.Duration
	binding       *confirmationSourceBinding
	current       *sandbox.Handle
	harness       longmemeval.Harness
	generation    uint64
	// currentCapability is retained only while a started process has not yet
	// been source-bound. It lets Close retry a strict stop after a partial
	// Sandbox.Run/bind failure without reopening broker admission.
	currentCapability string
	activeUserID      string
	closed            bool
}

func (h *confirmationLongMemHarness) startCaseLocked(ctx context.Context) error {
	if h.closed || nilInterface(h.sandbox) || h.broker == nil || h.binding == nil {
		return errors.New("confirmation LongMem harness is closed")
	}
	if h.current != nil || h.harness != nil || h.generation != 0 {
		if h.current == nil || h.harness == nil || h.generation == 0 {
			return errors.New("confirmation LongMem case lifecycle is inconsistent")
		}
		return nil
	}
	generation, _, err := h.broker.beginCaseSnapshot(h.sessionID)
	if err != nil {
		return errors.New("confirmation LongMem case attribution unavailable")
	}
	capability, err := h.broker.installSourceCapability(h.sessionID, h.runID)
	if err != nil {
		_, _ = h.broker.endCaseSnapshot(h.sessionID, generation)
		return errors.New("confirmation LongMem source capability unavailable")
	}
	env, err := harnessSandboxEnvWithCapability(
		nil, confirmationBenchVersion, platformLockedProvider, h.sessionID, capability,
	)
	if err != nil {
		_ = h.broker.revokeSourceCapability(h.sessionID, h.runID, capability)
		_, _ = h.broker.endCaseSnapshot(h.sessionID, generation)
		return errors.New("confirmation LongMem source capability unavailable")
	}
	handle, err := h.sandbox.Run(ctx, h.image, env)
	if handle == nil {
		_ = h.broker.revokeSourceCapability(h.sessionID, h.runID, capability)
		_, _ = h.broker.endCaseSnapshot(h.sessionID, generation)
		return errors.New("confirmation LongMem sandbox could not start")
	}
	h.current, h.generation, h.currentCapability = handle, generation, capability
	if err != nil || net.ParseIP(handle.SourceIP) == nil || strings.TrimSpace(handle.BaseURL) == "" {
		if _, cleanupErr := h.finishUnboundCaseLocked(); cleanupErr != nil {
			return errors.New("confirmation LongMem sandbox isolation unavailable")
		}
		return errors.New("confirmation LongMem sandbox could not start")
	}
	if !h.binding.bind(h.broker, h.sessionID, h.runID, handle.SourceIP, capability) {
		if _, cleanupErr := h.finishUnboundCaseLocked(); cleanupErr != nil {
			return errors.New("confirmation LongMem sandbox isolation unavailable")
		}
		return errors.New("confirmation LongMem source binding failed")
	}
	h.currentCapability = ""
	healthCtx := runner.TrustSandbox(ctx)
	if err := runner.WaitHealthy(healthCtx, handle.BaseURL, h.healthTimeout); err != nil {
		_, _ = h.finishCaseLocked()
		return errors.New("confirmation LongMem harness did not become healthy")
	}
	harness, err := longmemeval.NewHTTPHarness(handle.BaseURL, &http.Client{
		Transport:     http.DefaultTransport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	})
	if err != nil {
		_, _ = h.finishCaseLocked()
		return errors.New("confirmation LongMem harness client unavailable")
	}
	h.harness = harness
	return nil
}

func (h *confirmationLongMemHarness) stopHandleLocked(handle *sandbox.Handle) error {
	if handle == nil {
		return errors.New("confirmation LongMem sandbox identity is unavailable")
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	err := h.sandbox.StopRetainingImage(cleanupCtx, handle)
	cancel()
	return err
}

func (h *confirmationLongMemHarness) finishCaseLocked() (brokerCaseSnapshot, error) {
	if h.current == nil || h.generation == 0 {
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	drainErr := h.broker.beginConfirmationCaseDrain(h.sessionID, h.generation)
	revokeOK := h.binding.revoke(h.broker, h.sessionID, h.runID)
	stopErr := h.stopHandleLocked(h.current)
	if drainErr != nil || !revokeOK || stopErr != nil {
		h.closed = true
		if stopErr != nil {
			return brokerCaseSnapshot{}, errors.New("confirmation LongMem sandbox isolation unavailable")
		}
		if !revokeOK {
			return brokerCaseSnapshot{}, errors.New("confirmation LongMem source capability revocation unavailable")
		}
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	sourceIP := h.current.SourceIP
	generation := h.generation
	drainCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	snapshot, err := h.broker.waitConfirmationCaseDrained(drainCtx, h.sessionID, generation)
	cancel()
	if err != nil {
		h.closed = true
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	if !h.binding.unbind(h.broker, h.sessionID, h.runID, sourceIP) {
		h.closed = true
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem source revocation unavailable")
	}
	closed, err := h.broker.endCaseSnapshot(h.sessionID, generation)
	if err != nil {
		h.closed = true
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	h.current, h.harness, h.generation, h.currentCapability, h.activeUserID = nil, nil, 0, "", ""
	snapshot = closed
	return snapshot, nil
}

func (h *confirmationLongMemHarness) finishUnboundCaseLocked() (brokerCaseSnapshot, error) {
	if h.current == nil || h.generation == 0 || h.binding.sourceIP != "" {
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem unbound case attribution unavailable")
	}
	drainErr := h.broker.beginConfirmationCaseDrain(h.sessionID, h.generation)
	revokeOK := true
	if h.currentCapability != "" {
		if !h.broker.revokeSourceCapability(h.sessionID, h.runID, h.currentCapability) {
			revokeOK = false
		} else {
			h.currentCapability = ""
		}
	}
	stopErr := h.stopHandleLocked(h.current)
	if drainErr != nil || !revokeOK || stopErr != nil {
		h.closed = true
		if stopErr != nil {
			return brokerCaseSnapshot{}, errors.New("confirmation LongMem sandbox isolation unavailable")
		}
		if !revokeOK {
			return brokerCaseSnapshot{}, errors.New("confirmation LongMem source capability revocation unavailable")
		}
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	generation := h.generation
	drainCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	snapshot, err := h.broker.waitConfirmationCaseDrained(drainCtx, h.sessionID, generation)
	cancel()
	if err != nil {
		h.closed = true
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	closed, err := h.broker.endCaseSnapshot(h.sessionID, generation)
	if err != nil {
		h.closed = true
		return brokerCaseSnapshot{}, errors.New("confirmation LongMem case attribution unavailable")
	}
	h.current, h.harness, h.generation, h.currentCapability, h.activeUserID = nil, nil, 0, "", ""
	snapshot = closed
	return snapshot, nil
}

func (h *confirmationLongMemHarness) Seed(
	ctx context.Context,
	request protocol.SeedRequest,
) (protocol.SeedResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if strings.TrimSpace(request.UserID) == "" || (h.activeUserID != "" && request.UserID != h.activeUserID) {
		return protocol.SeedResponse{}, errors.New("confirmation LongMem projected user binding mismatch")
	}
	if err := h.startCaseLocked(ctx); err != nil {
		return protocol.SeedResponse{}, err
	}
	if h.activeUserID == "" {
		h.activeUserID = request.UserID
	}
	response, err := h.harness.Seed(ctx, request)
	if err != nil {
		_, _ = h.finishCaseLocked()
	}
	return response, err
}

func (h *confirmationLongMemHarness) Run(
	ctx context.Context,
	request protocol.RunRequest,
) (protocol.RunResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if strings.TrimSpace(request.UserID) == "" || h.activeUserID == "" || request.UserID != h.activeUserID {
		return protocol.RunResponse{}, errors.New("confirmation LongMem projected user binding mismatch")
	}
	if err := h.startCaseLocked(ctx); err != nil {
		return protocol.RunResponse{}, err
	}
	response, runErr := h.harness.Run(ctx, request)
	snapshot, snapshotErr := h.finishCaseLocked()
	if snapshotErr != nil {
		return protocol.RunResponse{}, snapshotErr
	}
	if runErr == nil {
		return response, nil
	}
	return response, longmemeval.BindTrustedCaseInferenceActivity(runErr, longmemeval.TrustedCaseInferenceActivity{
		ReaderAttempts:         snapshot.ReaderAttempts,
		ReaderDispatches:       snapshot.ReaderDispatches,
		ReaderReceipted:        snapshot.ReaderReceipted,
		ReaderAgentRejections:  snapshot.ReaderAgentRejections,
		ReaderInFlight:         snapshot.ReaderInFlight,
		ReaderCancellations:    snapshot.ReaderCancellations,
		EmbeddingAttempts:      snapshot.EmbeddingAttempts,
		EmbeddingDispatches:    snapshot.EmbeddingDispatches,
		EmbeddingDelivered:     snapshot.EmbeddingDelivered,
		EmbeddingInFlight:      snapshot.EmbeddingInFlight,
		EmbeddingCancellations: snapshot.EmbeddingCancellations,
	})
}

func (h *confirmationLongMemHarness) Close() error {
	if h == nil {
		return nil
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	h.closed = true
	if h.current != nil && h.generation != 0 {
		if h.binding.sourceIP == "" {
			_, err := h.finishUnboundCaseLocked()
			return err
		} else {
			_, err := h.finishCaseLocked()
			return err
		}
	}
	return nil
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
// environment carries only a path and content digest. Selector/projection
// material is derived from the signed lease; provider authorization remains on
// Platform behind ticket-scoped grants.
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
	launchFile, _, err := verifyImmutableConfirmationFile(
		installation.LaunchManifestPath, maximumConfirmationInstallationBytes,
	)
	if err != nil || launchFile.sha256 != installation.LaunchManifestSHA256 {
		return nil, errors.New("confirmation manifest does not match launch approval")
	}
	if installation.SandboxHealthTimeoutMilliseconds <= 0 ||
		installation.SandboxHealthTimeoutMilliseconds > int64((10*time.Minute)/time.Millisecond) {
		return nil, errors.New("confirmation sandbox health timeout is invalid")
	}
	factory, err := newScreenedConfirmationRuntimeFactory(screenedConfirmationRuntimeFactoryConfig{
		Profile: profile, Sandbox: sandboxRuntime, Broker: broker,
		LaunchManifestPath:   installation.LaunchManifestPath,
		LaunchManifestSHA256: installation.LaunchManifestSHA256,
		LongMemDatasetPath:   installation.LongMemDatasetPath,
		AblationDatasetPath:  installation.AblationDatasetPath,
		SandboxHealthTimeout: time.Duration(installation.SandboxHealthTimeoutMilliseconds) * time.Millisecond,
	})
	if err != nil {
		return nil, err
	}
	return newTrustedConfirmationExecutor(confirmationProfileInstallation{
		ExecutionProfile:     installation.ExecutionProfile,
		LaunchManifestSHA256: installation.LaunchManifestSHA256,
	}, factory)
}

// screenedConfirmationRuntimeFactoryConfig is server-owned immutable
// configuration. Provider credentials are deliberately absent: they arrive
// only as ticket-scoped Platform grants.
type screenedConfirmationRuntimeFactoryConfig struct {
	Profile              confirmationExecutionProfileWire
	Sandbox              sandbox.Sandbox
	Broker               *inferenceBroker
	LaunchManifestPath   string
	LaunchManifestSHA256 string
	LongMemDatasetPath   string
	AblationDatasetPath  string
	SandboxHealthTimeout time.Duration
}

type screenedConfirmationRuntimeFactory struct {
	profile         confirmationExecutionProfileWire
	sandbox         sandbox.Sandbox
	broker          *inferenceBroker
	launchManifest  verifiedConfirmationFile
	longMemDataset  verifiedConfirmationFile
	ablationFile    verifiedConfirmationFile
	ablationDataset confirmationAblationDataset
	healthTimeout   time.Duration
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
	rootlessBoundary := concrete && localDocker != nil && localDocker.RequireRootless &&
		validConfirmationEgressProxy(localDocker.EgressProxy)
	managedDindBoundary := concrete && localDocker != nil && localDocker.RequireIsolatedDaemon &&
		localDocker.EgressNetwork == "ditto-sandbox" && localDocker.EgressProxy == ""
	if !concrete || localDocker == nil || !localDocker.Harden ||
		!validConfirmationNetworkName(localDocker.EgressNetwork) ||
		(!rootlessBoundary && !managedDindBoundary) {
		return nil, errors.New("confirmation sandbox lacks production isolation")
	}
	if config.SandboxHealthTimeout <= 0 {
		return nil, errors.New("confirmation sandbox health timeout must be positive")
	}
	launchFile, _, err := verifyImmutableConfirmationFile(config.LaunchManifestPath, maximumConfirmationInstallationBytes)
	if err != nil || launchFile.sha256 != config.LaunchManifestSHA256 {
		return nil, errors.New("confirmation manifest does not match launch approval")
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
		launchManifest: launchFile,
		longMemDataset: longMemFile,
		ablationFile:   ablationFile, ablationDataset: dataset,
		healthTimeout: config.SandboxHealthTimeout,
	}
	if err := factory.ValidateInstallation(config.Profile); err != nil {
		return nil, err
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
	if file, _, fileErr := verifyImmutableConfirmationFile(factory.launchManifest.path, maximumConfirmationInstallationBytes); fileErr != nil ||
		file.sha256 != factory.launchManifest.sha256 {
		return errors.New("confirmation launch manifest drift")
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
		identity.ProfileChecksum == "" || identity.SettingsChecksum == "" || identity.SettingsRevision < 1 ||
		identity.RetestGeneration < 0 || identity.Deadline.IsZero() || !identity.Deadline.After(time.Now()) {
		return errors.New("confirmation runtime identity is incomplete")
	}
	return nil
}

func (factory *screenedConfirmationRuntimeFactory) installBrokerSession(
	identity confirmationRuntimeIdentity,
	provider *longmemeval.ProviderSession,
) (string, string, error) {
	sessionID := identity.InferenceSessionID
	runID := uuid.NewString()
	readerPolicy := factory.profile.longMemProfile().Providers[0]
	for _, lane := range factory.profile.longMemProfile().Providers {
		if lane.Lane == longmemeval.ReaderLane {
			readerPolicy = lane
		}
	}
	factory.broker.mu.RLock()
	session := factory.broker.sessions[sessionID]
	factory.broker.mu.RUnlock()
	if session == nil {
		return "", "", errors.New("confirmation broker session is unavailable")
	}
	session.mu.Lock()
	defer session.mu.Unlock()
	if !session.confirmationSession || session.boundRunID != "" || !session.expiresAt.Equal(identity.Deadline) ||
		session.ticketAgentID != identity.AgentID || session.ticketSlotID != identity.SlotID || len(session.confirmationGrants) != 3 {
		return "", "", errors.New("confirmation broker session identity mismatch")
	}
	session.trustedChatHandler = provider.ReaderHandler()
	session.provider = readerPolicy.Provider
	session.model = readerPolicy.Model
	session.requestModel = readerPolicy.Model
	session.profileRevision = readerPolicy.ProfileRevision
	session.boundRunID = runID
	session.benchVersion = confirmationBenchVersion
	if session.embeddingCalls == nil {
		session.embeddingCalls = make(map[chan struct{}]context.CancelFunc)
	}
	if session.cancels == nil {
		session.cancels = make(map[string]context.CancelFunc)
	}
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
		return nil, wrapConfirmationExecutionFailure("installation_validation", err)
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
		return nil, wrapConfirmationExecutionFailure("source_validation", err)
	}
	if err := factory.sandbox.Available(ctx); err != nil {
		return nil, wrapConfirmationExecutionFailure(
			"sandbox_availability", errors.New("confirmation sandbox is unavailable"),
		)
	}
	longMemSource, err := factory.longMemDataset.openVerified(384 << 20)
	if err != nil {
		return nil, wrapConfirmationExecutionFailure("longmem_dataset", err)
	}
	_, ablationRaw, err := verifyConfirmationFile(factory.ablationFile.path, maximumConfirmationAblationDatasetBytes)
	if err != nil || digestBytes(ablationRaw) != factory.profile.AblationDatasetSHA256 {
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure(
			"ablation_dataset", errors.New("confirmation ablation dataset changed after installation"),
		)
	}
	ablationDataset, err := decodeConfirmationAblationDataset(ablationRaw)
	if err != nil || ablationDataset.Revision != factory.ablationDataset.Revision {
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure(
			"ablation_dataset", errors.New("confirmation ablation dataset identity drift"),
		)
	}
	longMemProjectionKey, ablationSelectionKey, ablationProjectionKey, err :=
		deriveConfirmationRuntimeMaterial(factory.profile, identity)
	if err != nil {
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure("runtime_material", err)
	}
	zeroKeys := true
	defer func() {
		if zeroKeys {
			zeroConfirmationBytes(longMemProjectionKey)
			zeroConfirmationBytes(ablationSelectionKey)
			zeroConfirmationBytes(ablationProjectionKey)
		}
	}()
	providerRuntime, err := factory.broker.confirmationProviderRuntime(
		identity.InferenceSessionID,
		factory.profile,
	)
	if err != nil {
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure(
			"provider_capabilities", errors.New("confirmation platform capabilities are unavailable"),
		)
	}
	provider, err := longmemeval.NewProviderSession(ctx, factory.profile.longMemProfile(), providerRuntime)
	if err != nil {
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure(
			"provider_session", errors.New("confirmation provider session is unavailable"),
		)
	}
	image, _, _, err := factory.sandbox.Build(ctx, identity.Source)
	if err != nil || strings.TrimSpace(image) == "" {
		_ = provider.Close()
		_ = longMemSource.Close()
		return nil, wrapConfirmationExecutionFailure(
			"screened_image", errors.New("confirmation screened image is unavailable"),
		)
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
		return nil, wrapConfirmationExecutionFailure("broker_session", err)
	}
	cleanupSession := true
	defer func() {
		if cleanupSession {
			factory.broker.removeRun(sessionID, runID)
			_ = provider.Close()
			_ = longMemSource.Close()
		}
	}()
	// Give the health-only process a unique compatibility hostname/key without
	// installing it in the broker. It can boot every supported client adapter,
	// but no queued request from this process can ever become authorized after a
	// later case reuses its source IP.
	preflightCapability, err := newBrokerSourceCapability()
	if err != nil {
		return nil, wrapConfirmationExecutionFailure(
			"sandbox_start", errors.New("confirmation health capability is unavailable"),
		)
	}
	env, err := harnessSandboxEnvWithCapability(
		nil, confirmationBenchVersion, platformLockedProvider, sessionID, preflightCapability,
	)
	if err != nil {
		return nil, wrapConfirmationExecutionFailure(
			"sandbox_start", errors.New("confirmation health capability is unavailable"),
		)
	}
	handle, err := factory.sandbox.Run(ctx, image, env)
	if err != nil || handle == nil || net.ParseIP(handle.SourceIP) == nil || strings.TrimSpace(handle.BaseURL) == "" {
		if handle != nil {
			if stopErr := factory.stopHandle(handle); stopErr != nil {
				cleanupImage = false
				return nil, wrapConfirmationExecutionFailure(
					"sandbox_stop", errors.New("confirmation health sandbox isolation failed"),
				)
			}
		}
		return nil, wrapConfirmationExecutionFailure(
			"sandbox_start", errors.New("confirmation LongMemEval sandbox could not start"),
		)
	}
	healthCtx := runner.TrustSandbox(ctx)
	if err := runner.WaitHealthy(healthCtx, handle.BaseURL, factory.healthTimeout); err != nil {
		if stopErr := factory.stopHandle(handle); stopErr != nil {
			cleanupImage = false
			return nil, wrapConfirmationExecutionFailure(
				"sandbox_stop", errors.New("confirmation health sandbox isolation failed"),
			)
		}
		return nil, wrapConfirmationExecutionFailure(
			"harness_health", errors.New("confirmation LongMemEval harness did not become healthy"),
		)
	}
	// This first container proves the screened image's health only. It is never
	// source-bound, and is strictly removed while retaining the image before the
	// first selected case receives a generation capability.
	cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	stopErr := factory.sandbox.StopRetainingImage(cleanupCtx, handle)
	cleanupCancel()
	if stopErr != nil {
		return nil, wrapConfirmationExecutionFailure(
			"sandbox_stop", errors.New("confirmation health sandbox isolation failed"),
		)
	}
	if !factory.broker.beginEmbeddingPhase(sessionID, runID) {
		return nil, wrapConfirmationExecutionFailure(
			"embedding_phase", errors.New("confirmation embedding phase unavailable"),
		)
	}
	binding := &confirmationSourceBinding{}
	longMemHarness := &confirmationLongMemHarness{
		sandbox: factory.sandbox, broker: factory.broker, image: image, sessionID: sessionID, runID: runID,
		healthTimeout: factory.healthTimeout, binding: binding,
	}
	caseRunner := &screenedAblationCaseRunner{
		sandbox: factory.sandbox, broker: factory.broker, image: image, sessionID: sessionID, runID: runID,
		healthTimeout: factory.healthTimeout, dataset: ablationDataset, binding: binding,
	}
	closer := &confirmationRuntimeCloser{
		sandbox: factory.sandbox, broker: factory.broker, image: image, sessionID: sessionID, runID: runID,
		provider: provider, source: longMemSource, longMem: longMemHarness, runner: caseRunner,
		derivedBuffers: [][]byte{longMemProjectionKey, ablationSelectionKey, ablationProjectionKey},
	}
	population := make([]ablation.EligibleCase, len(ablationDataset.Cases))
	for index, item := range ablationDataset.Cases {
		population[index] = ablation.EligibleCase{CaseID: item.CaseID, UserID: item.UserID}
	}
	cleanupImage = false
	cleanupSession = false
	zeroKeys = false
	return &confirmationRuntime{
		LongMemSource:  longMemSource,
		LongMemHarness: longMemHarness,
		LongMemJudge:   provider.Judge(), LongMemMeter: provider,
		LongMemProjectionKey:  longMemProjectionKey,
		AblationPopulation:    ablation.EligiblePopulation{BenchVersion: confirmationBenchVersion, Confirmation: true, Cases: population},
		AblationCaseRunner:    caseRunner,
		AblationSelectionKey:  ablationSelectionKey,
		AblationProjectionKey: ablationProjectionKey,
		Close:                 closer.Close,
	}, nil
}

func (factory *screenedConfirmationRuntimeFactory) stopHandle(handle *sandbox.Handle) error {
	if handle == nil {
		return errors.New("confirmation sandbox identity is unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	err := factory.sandbox.StopRetainingImage(ctx, handle)
	cancel()
	return err
}

type screenedAblationCaseRunner struct {
	mu                sync.Mutex
	sandbox           sandbox.Sandbox
	broker            *inferenceBroker
	image             string
	sessionID         string
	runID             string
	healthTimeout     time.Duration
	dataset           confirmationAblationDataset
	current           *sandbox.Handle
	currentLease      *brokerAblationLease
	currentCapability string
	currentBound      bool
	binding           *confirmationSourceBinding
	closed            bool
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

func (runnerAdapter *screenedAblationCaseRunner) stopCurrentLocked() error {
	if runnerAdapter.current == nil {
		if runnerAdapter.currentLease != nil || runnerAdapter.currentCapability != "" || runnerAdapter.currentBound {
			return errors.New("confirmation ablation lifecycle is inconsistent")
		}
		return nil
	}
	if runnerAdapter.currentLease == nil {
		return errors.New("confirmation ablation scope unavailable")
	}
	drainErr := runnerAdapter.currentLease.beginDrain()
	revokeOK := true
	if runnerAdapter.currentBound {
		if !runnerAdapter.binding.revoke(runnerAdapter.broker, runnerAdapter.sessionID, runnerAdapter.runID) {
			revokeOK = false
		} else {
			runnerAdapter.currentCapability = ""
		}
	} else if runnerAdapter.currentCapability != "" {
		if !runnerAdapter.broker.revokeSourceCapability(
			runnerAdapter.sessionID, runnerAdapter.runID, runnerAdapter.currentCapability,
		) {
			revokeOK = false
		} else {
			runnerAdapter.currentCapability = ""
		}
	}
	stopErr := runnerAdapter.stopCurrentContainerLocked()
	if drainErr != nil || !revokeOK || stopErr != nil {
		runnerAdapter.closed = true
		if stopErr != nil {
			return stopErr
		}
		if !revokeOK {
			return errors.New("confirmation ablation source capability revocation failed")
		}
		return errors.New("confirmation ablation drain unavailable")
	}
	drainCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	drainErr = runnerAdapter.currentLease.waitDrained(drainCtx)
	cancel()
	if drainErr != nil {
		runnerAdapter.closed = true
		return drainErr
	}
	if runnerAdapter.currentBound {
		if err := runnerAdapter.unbindCurrentLocked(); err != nil {
			runnerAdapter.closed = true
			return err
		}
	}
	runnerAdapter.currentLease.Close()
	runnerAdapter.current, runnerAdapter.currentLease = nil, nil
	runnerAdapter.currentCapability, runnerAdapter.currentBound = "", false
	return nil
}

func (runnerAdapter *screenedAblationCaseRunner) stopCurrentContainerLocked() error {
	if runnerAdapter.current == nil {
		return nil
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
	err := runnerAdapter.sandbox.StopRetainingImage(cleanupCtx, runnerAdapter.current)
	cancel()
	if err != nil {
		return errors.New("confirmation ablation sandbox isolation failed")
	}
	return nil
}

func (runnerAdapter *screenedAblationCaseRunner) unbindCurrentLocked() error {
	if runnerAdapter.current == nil {
		return nil
	}
	if !runnerAdapter.binding.unbind(
		runnerAdapter.broker, runnerAdapter.sessionID, runnerAdapter.runID, runnerAdapter.current.SourceIP,
	) {
		return errors.New("confirmation ablation source revocation failed")
	}
	runnerAdapter.current = nil
	runnerAdapter.currentBound = false
	return nil
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
	if runnerAdapter.closed || nilInterface(runnerAdapter.sandbox) || runnerAdapter.broker == nil || runnerAdapter.binding == nil {
		return ablation.CaseRunResult{}, errors.New("confirmation ablation runner is closed")
	}
	item, ok := runnerAdapter.dataset.byID[request.CaseID]
	if !ok || strings.TrimSpace(request.OpaqueUserNamespace) == "" {
		return ablation.CaseRunResult{}, errors.New("confirmation ablation case is unavailable")
	}
	// The stable LongMem container, or the previous attempt container, is fully
	// stopped before a new responder capability is leased.
	if err := runnerAdapter.stopCurrentLocked(); err != nil {
		return ablation.CaseRunResult{}, err
	}
	// Install the responder scope before starting an untrusted process. With no
	// source bound yet it cannot dispatch, and every request still needs the
	// fresh per-container capability installed below.
	lease, err := runnerAdapter.broker.beginAblationCase(runnerAdapter.sessionID, runnerAdapter.runID, request)
	if err != nil {
		return ablation.CaseRunResult{}, err
	}
	capability, err := runnerAdapter.broker.installSourceCapability(runnerAdapter.sessionID, runnerAdapter.runID)
	if err != nil {
		lease.Close()
		return ablation.CaseRunResult{}, errors.New("confirmation ablation source capability unavailable")
	}
	env, err := harnessSandboxEnvWithCapability(
		nil, confirmationBenchVersion, platformLockedProvider,
		runnerAdapter.sessionID, capability,
	)
	if err != nil {
		_ = runnerAdapter.broker.revokeSourceCapability(runnerAdapter.sessionID, runnerAdapter.runID, capability)
		lease.Close()
		return ablation.CaseRunResult{}, errors.New("confirmation ablation source capability unavailable")
	}
	handle, err := runnerAdapter.sandbox.Run(ctx, runnerAdapter.image, env)
	if handle == nil {
		_ = runnerAdapter.broker.revokeSourceCapability(runnerAdapter.sessionID, runnerAdapter.runID, capability)
		lease.Close()
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation sandbox could not start"))
	}
	runnerAdapter.current, runnerAdapter.currentLease = handle, lease
	runnerAdapter.currentCapability = capability
	if err != nil || net.ParseIP(handle.SourceIP) == nil || strings.TrimSpace(handle.BaseURL) == "" {
		if cleanupErr := runnerAdapter.stopCurrentLocked(); cleanupErr != nil {
			return ablation.CaseRunResult{}, cleanupErr
		}
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation sandbox could not start"))
	}
	if !runnerAdapter.binding.bind(
		runnerAdapter.broker, runnerAdapter.sessionID, runnerAdapter.runID, handle.SourceIP, capability,
	) {
		if cleanupErr := runnerAdapter.stopCurrentLocked(); cleanupErr != nil {
			return ablation.CaseRunResult{}, cleanupErr
		}
		return ablation.CaseRunResult{}, errors.New("confirmation ablation source binding failed")
	}
	runnerAdapter.currentBound = true
	healthCtx := runner.TrustSandbox(ctx)
	if err := runner.WaitHealthy(healthCtx, handle.BaseURL, runnerAdapter.healthTimeout); err != nil {
		if cleanupErr := runnerAdapter.stopCurrentLocked(); cleanupErr != nil {
			return ablation.CaseRunResult{}, cleanupErr
		}
		return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation harness did not become healthy"))
	}
	harness, err := longmemeval.NewHTTPHarness(handle.BaseURL, &http.Client{
		Transport:     http.DefaultTransport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	})
	if err != nil {
		if cleanupErr := runnerAdapter.stopCurrentLocked(); cleanupErr != nil {
			return ablation.CaseRunResult{}, cleanupErr
		}
		return ablation.CaseRunResult{}, err
	}
	seeds := projectConfirmationAblationSeeds(item, request.OpaqueUserNamespace)
	for _, seed := range seeds {
		if _, err := harness.Seed(ctx, seed); err != nil {
			if cleanupErr := runnerAdapter.stopCurrentLocked(); cleanupErr != nil {
				return ablation.CaseRunResult{}, cleanupErr
			}
			return ablation.CaseRunResult{}, ablation.MarkRetryable(errors.New("confirmation ablation seed failed"))
		}
	}
	userID := confirmationOpaqueUUID("user", request.OpaqueUserNamespace)
	caseID := confirmationOpaqueUUID("case", item.CaseID)
	response, err := harness.Run(ctx, protocol.RunRequest{
		CaseID: caseID, SystemPrompt: item.SystemPrompt, UserInput: item.Question,
		Tools: longmemeval.NativeMemoryTools(), BenchVersion: confirmationBenchVersion, UserID: userID,
	})
	if finishErr := runnerAdapter.stopCurrentLocked(); finishErr != nil {
		return ablation.CaseRunResult{}, finishErr
	}
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
	once           sync.Once
	err            error
	sandbox        sandbox.Sandbox
	broker         *inferenceBroker
	image          string
	sessionID      string
	runID          string
	provider       *longmemeval.ProviderSession
	source         io.Closer
	longMem        *confirmationLongMemHarness
	runner         *screenedAblationCaseRunner
	derivedBuffers [][]byte
}

func (closer *confirmationRuntimeCloser) Close() error {
	closer.once.Do(func() {
		var failures []error
		longMemIsolated := true
		if closer.longMem != nil {
			if err := closer.longMem.Close(); err != nil {
				longMemIsolated = false
				failures = append(failures, errors.New("close confirmation LongMem sandbox"))
			}
		}
		ablationIsolated := true
		if closer.runner != nil {
			closer.runner.mu.Lock()
			closer.runner.closed = true
			if err := closer.runner.stopCurrentLocked(); err != nil {
				ablationIsolated = false
				failures = append(failures, errors.New("close confirmation ablation sandbox"))
			}
			closer.runner.mu.Unlock()
		}
		closer.broker.endEmbeddingPhase(closer.sessionID, closer.runID)
		// Session destruction is the final fail-closed revocation/cancellation
		// boundary and must run even if strict process removal could not be proven.
		// Only image release is gated on process isolation.
		closer.broker.removeRun(closer.sessionID, closer.runID)
		if err := closer.provider.Close(); err != nil {
			failures = append(failures, errors.New("close confirmation provider session"))
		}
		if err := closer.source.Close(); err != nil {
			failures = append(failures, errors.New("close confirmation dataset"))
		}
		if longMemIsolated && ablationIsolated {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), confirmationRuntimeCleanupTimeout)
			closer.sandbox.Release(cleanupCtx, closer.image)
			cancel()
		}
		for _, value := range closer.derivedBuffers {
			zeroConfirmationBytes(value)
		}
		closer.err = errors.Join(failures...)
	})
	return closer.err
}

var _ confirmationRuntimeFactory = (*screenedConfirmationRuntimeFactory)(nil)
var _ ablation.CaseRunner = (*screenedAblationCaseRunner)(nil)
