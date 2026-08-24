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
	"log"
	"net/http"
	"strings"
	"time"
)

const (
	confirmationPurpose             = "v9_confirmation_bundle"
	confirmationBenchVersion        = 9
	confirmationBodyLimit           = 1 << 20
	confirmationMaxRequests         = 100_000
	confirmationMaxTokens           = 100_000_000
	confirmationFailureClassHeader  = "X-Ditto-Confirmation-Failure-Class"
	confirmationFailureStatusHeader = "X-Ditto-Confirmation-Failure-Status"
)

type confirmationReadiness struct {
	Ready           bool   `json:"ready"`
	ProfileRevision string `json:"profile_revision,omitempty"`
	ProfileChecksum string `json:"profile_checksum,omitempty"`
}

type confirmationExecutionRequest struct {
	Purpose                string          `json:"purpose"`
	BundleID               string          `json:"bundle_id"`
	TicketID               string          `json:"ticket_id"`
	AgentID                string          `json:"agent_id"`
	SlotID                 string          `json:"slot_id"`
	InferenceSessionID     string          `json:"inference_session_id"`
	Mode                   string          `json:"mode"`
	ArtifactURL            string          `json:"artifact_url"`
	ArtifactSHA256         string          `json:"artifact_sha256"`
	ScreenedImageURL       string          `json:"screened_image_url"`
	ScreenedImageSHA256    string          `json:"screened_image_sha256"`
	ScreenedImageSizeBytes int64           `json:"screened_image_size_bytes"`
	ScreenedImageID        string          `json:"screened_image_id"`
	ScreenedImageRef       string          `json:"screened_image_ref"`
	BenchVersion           int             `json:"bench_version"`
	Deadline               time.Time       `json:"deadline"`
	ProfileRevision        string          `json:"profile_revision"`
	ProfileChecksum        string          `json:"profile_checksum"`
	SettingsRevision       int             `json:"settings_revision"`
	SettingsChecksum       string          `json:"settings_checksum"`
	RetestGeneration       int             `json:"retest_generation"`
	PerBundleRequestCap    uint64          `json:"per_bundle_request_cap"`
	PerBundleTokenCap      uint64          `json:"per_bundle_token_cap"`
	ExecutionProfile       json.RawMessage `json:"execution_profile"`
}

type confirmationExecutionResult struct {
	LongMemEval                  json.RawMessage `json:"longmemeval"`
	InferenceAblation            json.RawMessage `json:"inference_ablation"`
	EmbeddingAblation            json.RawMessage `json:"embedding_ablation"`
	AblationCoordinatorLatencyMS uint64          `json:"ablation_coordinator_latency_ms"`
	EvidenceSHA256               string          `json:"evidence_sha256"`
}

type confirmationExecutor interface {
	Readiness() confirmationReadiness
	Execute(context.Context, confirmationExecutionRequest) (confirmationExecutionResult, error)
}

func canonicalConfirmationSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validIndexedSlot(value, prefix string, maxIndex byte) bool {
	return len(value) == len(prefix)+1 && strings.HasPrefix(value, prefix) &&
		value[len(value)-1] >= '0' && value[len(value)-1] <= maxIndex
}

func validLongMemSlot(value string) bool { return validIndexedSlot(value, "longmem-", '3') }

func validBrokerSlot(value string) bool {
	return validIndexedSlot(value, "slot-", '7') || validLongMemSlot(value)
}

func (request confirmationExecutionRequest) validate(now time.Time, readiness confirmationReadiness) error {
	if request.Purpose != confirmationPurpose || !confirmationSubjectEpochSupported(request.BenchVersion) {
		return errors.New("confirmation execution requires a supported confirmation bench version")
	}
	if strings.TrimSpace(request.BundleID) == "" || strings.TrimSpace(request.TicketID) == "" ||
		strings.TrimSpace(request.AgentID) == "" || !validLongMemSlot(request.SlotID) || len(request.InferenceSessionID) < 16 ||
		len(request.InferenceSessionID) > 128 || strings.TrimSpace(request.InferenceSessionID) != request.InferenceSessionID {
		return errors.New("confirmation bundle, ticket, agent, and slot identities are required")
	}
	if request.Mode != "shadow" && request.Mode != "enforce" {
		return errors.New("confirmation mode must authorize execution")
	}
	if request.Deadline.IsZero() || !request.Deadline.After(now) {
		return errors.New("confirmation ticket deadline is not live")
	}
	if request.PerBundleRequestCap == 0 || request.PerBundleRequestCap > confirmationMaxRequests ||
		request.PerBundleTokenCap == 0 || request.PerBundleTokenCap > confirmationMaxTokens {
		return errors.New("confirmation execution caps are out of range")
	}
	if request.SettingsRevision < 1 || request.RetestGeneration < 0 ||
		!canonicalConfirmationSHA256(request.SettingsChecksum) ||
		!canonicalConfirmationSHA256(request.ArtifactSHA256) {
		return errors.New("confirmation frozen identity is malformed")
	}
	if !readiness.Ready || request.ProfileRevision != readiness.ProfileRevision ||
		request.ProfileChecksum != readiness.ProfileChecksum ||
		!canonicalConfirmationSHA256(request.ProfileChecksum) {
		return errors.New("confirmation execution profile is not exactly ready")
	}
	if len(request.ExecutionProfile) == 0 || bytes.Equal(request.ExecutionProfile, []byte("null")) {
		return errors.New("confirmation execution profile is missing")
	}
	if strings.TrimSpace(request.ArtifactURL) == "" {
		return errors.New("confirmation artifact URL is missing")
	}
	if strings.TrimSpace(request.ScreenedImageURL) == "" ||
		!canonicalConfirmationSHA256(request.ScreenedImageSHA256) ||
		request.ScreenedImageSizeBytes <= 0 ||
		strings.TrimSpace(request.ScreenedImageID) == "" ||
		strings.TrimSpace(request.ScreenedImageRef) == "" {
		return errors.New("confirmation screened image identity is incomplete")
	}
	return nil
}

func (result confirmationExecutionResult) validate() error {
	for name, raw := range map[string]json.RawMessage{
		"longmemeval":        result.LongMemEval,
		"inference_ablation": result.InferenceAblation,
		"embedding_ablation": result.EmbeddingAblation,
	} {
		if len(raw) == 0 || bytes.Equal(raw, []byte("null")) || !json.Valid(raw) {
			return fmt.Errorf("confirmation %s evidence is invalid", name)
		}
	}
	if result.AblationCoordinatorLatencyMS == 0 {
		return errors.New("confirmation coordinator latency is missing")
	}
	if !canonicalConfirmationSHA256(result.EvidenceSHA256) {
		return errors.New("confirmation native wire digest is invalid")
	}
	return nil
}

func decodeConfirmationExecutionRequest(w http.ResponseWriter, r *http.Request) (confirmationExecutionRequest, error) {
	reader := http.MaxBytesReader(w, r.Body, confirmationBodyLimit)
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	var request confirmationExecutionRequest
	if err := decoder.Decode(&request); err != nil {
		return confirmationExecutionRequest{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return confirmationExecutionRequest{}, errors.New("confirmation request contains trailing JSON")
	}
	return request, nil
}

func (s *server) handleConfirmationReadiness(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if s.confirmation == nil {
		writeJSON(w, http.StatusServiceUnavailable, confirmationReadiness{Ready: false})
		return
	}
	readiness := s.confirmation.Readiness()
	if !readiness.Ready || strings.TrimSpace(readiness.ProfileRevision) == "" ||
		!canonicalConfirmationSHA256(readiness.ProfileChecksum) {
		writeJSON(w, http.StatusServiceUnavailable, confirmationReadiness{Ready: false})
		return
	}
	writeJSON(w, http.StatusOK, readiness)
}

func (s *server) handleConfirmationExecute(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	if s.confirmation == nil {
		writeError(w, http.StatusServiceUnavailable, "v9 confirmation executor is unconfigured")
		return
	}
	request, err := decodeConfirmationExecutionRequest(w, r)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid confirmation request")
		return
	}
	readiness := s.confirmation.Readiness()
	if err := request.validate(time.Now(), readiness); err != nil {
		writeError(w, http.StatusConflict, err.Error())
		return
	}
	slots := s.confirmationSlots
	if slots == nil {
		// Unit-test and embedded-server compatibility. Production always installs
		// the explicitly bounded channel during startup.
		slots = make(chan struct{}, 1)
	}
	select {
	case slots <- struct{}{}:
		defer func() { <-slots }()
	default:
		writeError(w, http.StatusTooManyRequests, "confirmation capacity is full")
		return
	}
	// Match the validator's HTTP timeout: ticket remaining minus the two-minute
	// signed-report margin. Otherwise a 48-case LongMem run can consume the
	// LongMem slice, then die two minutes into the 30-minute ablation window.
	executionDeadline := request.Deadline.Add(-2 * time.Minute)
	if !executionDeadline.After(time.Now()) {
		writeError(w, http.StatusConflict, "confirmation ticket cannot fund execution while preserving its reporting margin")
		return
	}
	ctx, cancel := context.WithDeadline(r.Context(), executionDeadline)
	defer cancel()
	result, err := s.confirmation.Execute(ctx, request)
	if err != nil {
		stage := confirmationExecutionStage(err)
		failureClass, failureStatus := confirmationExecutionDiagnostic(err)
		if failureClass != "unclassified" {
			// This protected scorer-to-validator response carries only the same
			// bounded diagnostic already admitted to local scorer logs. Keep the
			// public JSON body byte-stable and never forward wrapped error text,
			// response bodies, opaque identities, or source details.
			w.Header().Set(confirmationFailureClassHeader, failureClass)
			w.Header().Set(confirmationFailureStatusHeader, fmt.Sprintf("%d", failureStatus))
		}
		log.Printf(
			"confirmation execution failed: stage=%s failure_class=%s failure_status=%d bundle_id=%s agent_id=%s slot_id=%s",
			stage,
			failureClass,
			failureStatus,
			request.BundleID,
			request.AgentID,
			request.SlotID,
		)
		writeError(w, http.StatusUnprocessableEntity, "confirmation execution failed at "+stage)
		return
	}
	if err := result.validate(); err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

type confirmationProgressSnapshot struct {
	Ready bool   `json:"ready"`
	Done  int    `json:"done,omitempty"`
	Total int    `json:"total,omitempty"`
	Stage string `json:"stage,omitempty"`
}

func (s *server) handleConfirmationProgress(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	executor, ok := s.confirmation.(*trustedConfirmationExecutor)
	if !ok || executor == nil {
		writeJSON(w, http.StatusOK, confirmationProgressSnapshot{Ready: false})
		return
	}
	done, total, ready := executor.SnapshotProgress()
	if !ready {
		writeJSON(w, http.StatusOK, confirmationProgressSnapshot{Ready: false})
		return
	}
	writeJSON(w, http.StatusOK, confirmationProgressSnapshot{
		Ready: true, Done: done, Total: total, Stage: "running_confirmation",
	})
}
