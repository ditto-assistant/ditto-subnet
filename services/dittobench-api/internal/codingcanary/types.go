package codingcanary

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
)

const (
	RequestSchema  = "dittobench-coding-certification-canary-request-v1"
	ResponseSchema = "dittobench-coding-certification-canary-response-v1"

	maximumRequestBytes  = 64 << 10
	maximumResponseBytes = 128 << 10
	defaultTimeout       = 20 * time.Minute
	maximumTimeout       = 32 * time.Minute
	sha256Length         = 64
)

var (
	ErrInvalidConfig = errors.New("coding canary configuration is invalid")
	ErrInvalid       = errors.New("coding canary request is invalid")
	ErrUnauthorized  = errors.New("coding canary authorization failed")
	ErrConcurrent    = errors.New("coding canary operation is already active")
	ErrUnavailable   = errors.New("coding canary backend is unavailable")
	ErrClosed        = errors.New("coding canary is closed")
)

type Request struct {
	Schema                 string    `json:"schema"`
	OperationID            string    `json:"operation_id"`
	LeaseID                string    `json:"lease_id"`
	Deadline               time.Time `json:"deadline"`
	AgentID                string    `json:"agent_id"`
	AgentArtifactSHA256    string    `json:"agent_artifact_sha256"`
	ScreenedImageSHA256    string    `json:"screened_image_sha256"`
	ScreenedImageID        string    `json:"screened_image_id"`
	ScreenedImageRef       string    `json:"screened_image_ref"`
	ScreenedImageUploadID  string    `json:"screened_image_upload_id"`
	ScreenedImageSizeBytes int64     `json:"screened_image_size_bytes"`
	ScreeningPolicyVersion int       `json:"screening_policy_version"`
	ImageURL               string    `json:"image_url"`
	ImageExpiresAt         time.Time `json:"image_expires_at"`
	BenchVersion           int       `json:"bench_version"`
	CanaryManifestSHA256   string    `json:"canary_manifest_sha256"`
	RunnerPlanSHA256       string    `json:"runner_plan_sha256"`
	GraderPlanSHA256       string    `json:"grader_plan_sha256"`
	ResourceProfileSHA256  string    `json:"resource_profile_sha256"`
	InferencePolicySHA256  string    `json:"inference_policy_sha256"`
	CodingContractVersion  int       `json:"coding_contract_version"`
	WeightEligible         bool      `json:"weight_eligible"`
}

type Outcome struct {
	LeaseID             string
	CapabilitiesRevoked bool
	HarnessDestroyed    bool
	Receipt             codingcertifier.Receipt
}

type Response struct {
	Schema              string          `json:"schema"`
	LeaseID             string          `json:"lease_id"`
	CapabilitiesRevoked bool            `json:"capabilities_revoked"`
	HarnessDestroyed    bool            `json:"harness_destroyed"`
	Receipt             json.RawMessage `json:"receipt"`
}

type Backend interface {
	Certify(ctx context.Context, request Request) (Outcome, error)
}
