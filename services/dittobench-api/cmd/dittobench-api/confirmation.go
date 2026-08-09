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
	"net/http"
	"strings"
	"time"
)

const (
	confirmationPurpose      = "v9_confirmation_bundle"
	confirmationBenchVersion = 9
	confirmationBodyLimit    = 1 << 20
	confirmationMaxRequests  = 100_000
	confirmationMaxTokens    = 100_000_000
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

func (request confirmationExecutionRequest) validate(now time.Time, readiness confirmationReadiness) error {
	if request.Purpose != confirmationPurpose || request.BenchVersion != confirmationBenchVersion {
		return errors.New("confirmation execution requires the internal bench v9 purpose")
	}
	validSlot := len(request.SlotID) == len("slot-0") &&
		strings.HasPrefix(request.SlotID, "slot-") &&
		request.SlotID[len(request.SlotID)-1] >= '0' &&
		request.SlotID[len(request.SlotID)-1] <= '7'
	if strings.TrimSpace(request.BundleID) == "" || strings.TrimSpace(request.TicketID) == "" ||
		strings.TrimSpace(request.AgentID) == "" || !validSlot {
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
	ctx, cancel := context.WithDeadline(r.Context(), request.Deadline)
	defer cancel()
	result, err := s.confirmation.Execute(ctx, request)
	if err != nil {
		writeError(w, http.StatusUnprocessableEntity, "confirmation execution failed")
		return
	}
	if err := result.validate(); err != nil {
		writeError(w, http.StatusUnprocessableEntity, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, result)
}
