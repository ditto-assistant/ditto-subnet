package codingcertifier

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
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

const (
	CertificationSchema      = "dittobench-coding-capability-certification-v1"
	CertificationSolverModel = codingcontract.InferenceSolverModel
	minimumTTL               = time.Minute
	maximumTTL               = 24 * time.Hour
)

var requiredCapabilities = []string{
	"case_scoped_inference_v1",
	"coding_runner_tools_v1",
	"scoped_memory_seed_v1",
}

// Status is the terminal capability-certification state. Unsupported is a
// compatibility result, not a zero coding score.
type Status string

const (
	StatusUnsupported Status = "unsupported"
	StatusFailed      Status = "failed"
	StatusCertified   Status = "certified"
)

// Stage identifies the last authoritative certification transition.
type Stage string

const (
	StageHealth Stage = "health"
	StageSeed   Stage = "seed"
	StageRun    Stage = "run"
	StageFreeze Stage = "freeze"
	StageGrade  Stage = "grade"
)

// HarnessAttestation is supplied by the trusted sandbox controller. It binds
// the HTTP harness instance to the exact screened artifact and isolation
// policy; the untrusted harness cannot self-attest these fields.
type HarnessAttestation struct {
	HarnessInstanceID      string `json:"harness_instance_id"`
	AgentArtifactSHA256    string `json:"agent_artifact_sha256"`
	ReadOnlyRootFilesystem bool   `json:"read_only_root_filesystem"`
	NetworkPolicyEnforced  bool   `json:"network_policy_enforced"`
	CapabilityEgressOnly   bool   `json:"capability_egress_only"`
	NoHostDockerSocket     bool   `json:"no_host_docker_socket"`
	CredentialsAbsent      bool   `json:"credentials_absent"`
}

func (attestation HarnessAttestation) validate(expectedArtifact string) error {
	if !validIdentifier(attestation.HarnessInstanceID, 256) ||
		!lowerSHA256(attestation.AgentArtifactSHA256) ||
		attestation.AgentArtifactSHA256 != expectedArtifact ||
		!attestation.ReadOnlyRootFilesystem || !attestation.NetworkPolicyEnforced ||
		!attestation.CapabilityEgressOnly || !attestation.NoHostDockerSocket ||
		!attestation.CredentialsAbsent {
		return errors.New("coding harness attestation is invalid")
	}
	return nil
}

// HealthResponse is the bounded additive /coding/health response.
type HealthResponse struct {
	Status                          string   `json:"status"`
	SupportedCodingContractVersions []int    `json:"supported_coding_contract_versions"`
	Capabilities                    []string `json:"capabilities"`
}

func (health HealthResponse) validate() error {
	if health.Status != "ok" || health.SupportedCodingContractVersions == nil || health.Capabilities == nil ||
		len(health.SupportedCodingContractVersions) > 16 || len(health.Capabilities) > 64 {
		return errors.New("coding health response is invalid")
	}
	versions := append([]int(nil), health.SupportedCodingContractVersions...)
	slices.Sort(versions)
	if !uniqueInts(versions) {
		return errors.New("coding health versions contain duplicates")
	}
	for _, version := range versions {
		if version <= 0 || version > 1_000_000 {
			return errors.New("coding health version is outside bounds")
		}
	}
	capabilities := append([]string(nil), health.Capabilities...)
	slices.Sort(capabilities)
	if !uniqueStrings(capabilities) {
		return errors.New("coding health capabilities contain duplicates")
	}
	for _, capability := range capabilities {
		if !validIdentifier(capability, 128) {
			return errors.New("coding health capability is invalid")
		}
	}
	return nil
}

func (health HealthResponse) normalized() HealthResponse {
	health.SupportedCodingContractVersions = cloneSlice(health.SupportedCodingContractVersions)
	health.Capabilities = cloneSlice(health.Capabilities)
	slices.Sort(health.SupportedCodingContractVersions)
	slices.Sort(health.Capabilities)
	return health
}

func (health HealthResponse) supportsCodingV1() bool {
	if !slices.Contains(health.SupportedCodingContractVersions, codingcontract.ContractVersion) {
		return false
	}
	for _, required := range requiredCapabilities {
		if !slices.Contains(health.Capabilities, required) {
			return false
		}
	}
	return true
}

// SupportsCodingV1 reports whether the validated health projection advertises
// every additive coding-v1 capability required by a private attempt.
func (health HealthResponse) SupportsCodingV1() bool {
	return health.validate() == nil && health.supportsCodingV1()
}

// SeedResponse is the known-field /coding/seed response.
type SeedResponse struct {
	CaseID              string `json:"case_id"`
	ProfileCapabilityID string `json:"profile_capability_id"`
	MemoryBundleSHA256  string `json:"memory_bundle_sha256"`
	MemoryCount         int    `json:"memory_count"`
	IdempotentReplay    bool   `json:"idempotent_replay"`
}

func (response SeedResponse) validate(request codingcontract.SeedRequest, replay bool) error {
	if response.CaseID != request.CaseID || response.ProfileCapabilityID != request.ProfileCapabilityID ||
		response.MemoryBundleSHA256 != request.MemoryBundleSHA256 || response.MemoryCount != len(request.Memories) ||
		response.IdempotentReplay != replay {
		return errors.New("coding seed response does not match the canary request")
	}
	return nil
}

// ValidateIdentity checks the invariant acknowledgement fields while allowing
// either fresh installation or exact idempotent replay.
func (response SeedResponse) ValidateIdentity(request codingcontract.SeedRequest) error {
	if response.CaseID != request.CaseID || response.ProfileCapabilityID != request.ProfileCapabilityID ||
		response.MemoryBundleSHA256 != request.MemoryBundleSHA256 || response.MemoryCount != len(request.Memories) {
		return errors.New("coding seed response does not match the request")
	}
	return nil
}

// RunResponse is the advisory known-field /coding/run response. Workspace and
// grader evidence remain authoritative.
type RunResponse struct {
	CaseID      string `json:"case_id"`
	FinalReport struct {
		Summary        string   `json:"summary"`
		RemainingRisks []string `json:"remaining_risks"`
	} `json:"final_report"`
}

func (response RunResponse) validate(request codingcontract.RunRequest) error {
	if response.CaseID != request.CaseID || response.FinalReport.Summary == "" ||
		!utf8.ValidString(response.FinalReport.Summary) || len(response.FinalReport.Summary) > 2_000 ||
		response.FinalReport.RemainingRisks == nil || len(response.FinalReport.RemainingRisks) > 32 {
		return errors.New("coding run response is invalid")
	}
	for _, risk := range response.FinalReport.RemainingRisks {
		if risk == "" || !utf8.ValidString(risk) || len(risk) > 2_000 {
			return errors.New("coding run risk is invalid")
		}
	}
	return nil
}

// HarnessClient invokes one already-started, sandbox-attested harness.
type HarnessClient interface {
	Health(ctx context.Context) (HealthResponse, error)
	Seed(ctx context.Context, request codingcontract.SeedRequest) (SeedResponse, error)
	Run(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error)
}

// CapabilityBinding is the trusted outer route identity supplied to a
// capability publisher.
type CapabilityBinding struct {
	HarnessInstanceID   string
	AgentArtifactSHA256 string
	TicketID            string
	CaseID              string
	ProfileCapabilityID string
}

// PublishedCapability is revoked before the workspace is frozen. Revoke must
// be idempotent because the first successful commit can lose its response.
type PublishedCapability interface {
	URL() string
	Revoke(ctx context.Context) error
	Close() error
}

// CapabilityPublisher mounts the runner handler behind a source-bound,
// unguessable route. A plain public listener is not a production publisher.
type CapabilityPublisher interface {
	Publish(ctx context.Context, binding CapabilityBinding, handler http.Handler) (PublishedCapability, error)
}

// BundleOpener returns a fresh reader for one immutable content-addressed
// artifact. The visible bundle is opened once for authoring and again for the
// pristine replay.
type BundleOpener func() (io.ReadCloser, error)

// EvidenceBinding identifies immutable authoring artifacts before any bytes
// leave the runner.
type EvidenceBinding struct {
	CertificationID      string
	AgentArtifactSHA256  string
	HarnessInstanceID    string
	CanaryManifestSHA256 string
	TicketID             string
	CaseID               string
	ProfileCapabilityID  string
}

// TranscriptArtifact is the durable content-addressed result returned by a
// trusted sink.
type TranscriptArtifact struct {
	ObjectKey string
	SHA256    string
	SizeBytes int64
	Events    uint64
}

func (artifact TranscriptArtifact) validate(identity codingrunner.TranscriptIdentity) error {
	if artifact.ObjectKey != "sha256/"+identity.SHA256 || artifact.SHA256 != identity.SHA256 ||
		artifact.SizeBytes != identity.SizeBytes || artifact.Events != identity.Events {
		return errors.New("coding certification transcript artifact identity mismatch")
	}
	return nil
}

// TranscriptWriter receives exact canonical JSONL bytes, then atomically
// commits or aborts the artifact. Abort must be safe after Commit.
type TranscriptWriter interface {
	io.Writer
	Commit(ctx context.Context, identity codingrunner.TranscriptIdentity) (TranscriptArtifact, error)
	Abort() error
}

// TranscriptSink begins one durable, content-addressed transcript write.
type TranscriptSink interface {
	Begin(ctx context.Context, binding EvidenceBinding) (TranscriptWriter, error)
}

// FrozenSubmissionArtifact is the durable replayable patch accepted by the
// local evidence outbox before pristine grading begins.
type FrozenSubmissionArtifact struct {
	ObjectKey         string
	FrozenPatchSHA256 string
	FinalTreeSHA256   string
	ChangedPathRoot   string
}

func (artifact FrozenSubmissionArtifact) validate(submission codingrunner.FrozenSubmission) error {
	if artifact.ObjectKey != "sha256/"+submission.FrozenPatchSHA256 ||
		artifact.FrozenPatchSHA256 != submission.FrozenPatchSHA256 ||
		artifact.FinalTreeSHA256 != submission.FinalTreeSHA256 ||
		artifact.ChangedPathRoot != submission.ChangedPathRoot {
		return errors.New("coding frozen-submission artifact identity mismatch")
	}
	return nil
}

// FrozenSubmissionSink durably stores a replayable frozen submission in a
// validator-local outbox. Store must be idempotent for the binding and digest.
type FrozenSubmissionSink interface {
	Store(
		ctx context.Context,
		binding EvidenceBinding,
		submission codingrunner.FrozenSubmission,
	) (FrozenSubmissionArtifact, error)
}

// InferenceBinding scopes trusted relay evidence to the same artifact, task,
// deadline, and effective budgets as the workspace capability.
type InferenceBinding struct {
	CertificationID      string
	AgentArtifactSHA256  string
	HarnessInstanceID    string
	TicketID             string
	CaseID               string
	ProfileCapabilityID  string
	InferenceGrantSHA256 string
	Deadline             time.Time
	Budgets              codingcontract.Budgets
	RequestBudget        uint32
}

// InferenceEvidenceSource finalizes validator-observed model evidence after
// the harness run. It must not trust miner-reported token or provider fields.
type InferenceEvidenceSource interface {
	Evidence(ctx context.Context, binding InferenceBinding) (codingcontract.ModelEvidence, error)
}

// ErrInferenceNotObserved is candidate-attributable: the harness advertised
// case-scoped inference but never used the trusted relay.
var ErrInferenceNotObserved = errors.New("coding inference was not observed")

// Request is one trusted public-canary certification input.
type Request struct {
	CertificationID      string
	AgentArtifactSHA256  string
	CanaryManifestSHA256 string
	HarnessAttestation   HarnessAttestation
	Seed                 codingcontract.SeedRequest
	Issue                codingcontract.Issue
	RepositoryEpoch      string
	InferenceBaseURL     string
	InferenceGrantSHA256 string
	SolverModel          string
	SolverProvider       string
	ProviderRouteProfile string
	Budgets              codingcontract.Budgets
	RunnerManifest       codingrunner.Manifest
	GraderManifest       codinggrader.Manifest
}

func cloneRequest(request Request) Request {
	request.Issue.Constraints = cloneSlice(request.Issue.Constraints)
	request.Seed.Memories = cloneSlice(request.Seed.Memories)
	for index := range request.Seed.Memories {
		memory := &request.Seed.Memories[index]
		memory.RepositoryCapabilityID = cloneString(memory.RepositoryCapabilityID)
		memory.FactGroupID = cloneString(memory.FactGroupID)
		memory.ValidFromEpoch = cloneString(memory.ValidFromEpoch)
		memory.ValidUntilEpoch = cloneString(memory.ValidUntilEpoch)
		memory.Supersedes = cloneSlice(memory.Supersedes)
	}
	request.RunnerManifest.EditablePaths = cloneSlice(request.RunnerManifest.EditablePaths)
	request.RunnerManifest.CreatablePaths = cloneSlice(request.RunnerManifest.CreatablePaths)
	request.RunnerManifest.DeletablePaths = cloneSlice(request.RunnerManifest.DeletablePaths)
	request.RunnerManifest.TestCommands = cloneRunnerCommands(request.RunnerManifest.TestCommands)
	request.RunnerManifest.BuildCommands = cloneRunnerCommands(request.RunnerManifest.BuildCommands)
	request.GraderManifest.Build.Command.Argv = cloneSlice(request.GraderManifest.Build.Command.Argv)
	request.GraderManifest.TestGroups = cloneSlice(request.GraderManifest.TestGroups)
	for index := range request.GraderManifest.TestGroups {
		request.GraderManifest.TestGroups[index].Command.Argv = cloneSlice(request.GraderManifest.TestGroups[index].Command.Argv)
	}
	return request
}

func cloneRunnerCommands(commands []codingrunner.CommandSpec) []codingrunner.CommandSpec {
	result := cloneSlice(commands)
	for index := range result {
		result[index].Argv = cloneSlice(result[index].Argv)
	}
	return result
}

func cloneSlice[T any](values []T) []T {
	if values == nil {
		return nil
	}
	result := make([]T, len(values))
	copy(result, values)
	return result
}

func (request Request) validate(now time.Time) error {
	if !validIdentifier(request.CertificationID, 256) || !lowerSHA256(request.AgentArtifactSHA256) ||
		!lowerSHA256(request.CanaryManifestSHA256) || !lowerSHA256(request.InferenceGrantSHA256) ||
		request.SolverModel != CertificationSolverModel || !validIdentifier(request.SolverProvider, 128) ||
		!validIdentifier(request.ProviderRouteProfile, 128) {
		return errors.New("coding certification identity is invalid")
	}
	if err := request.HarnessAttestation.validate(request.AgentArtifactSHA256); err != nil {
		return err
	}
	if err := request.Seed.Validate(); err != nil {
		return fmt.Errorf("coding certification seed: %w", err)
	}
	if err := request.RunnerManifest.Validate(now); err != nil {
		return fmt.Errorf("coding certification runner: %w", err)
	}
	if request.RunnerManifest.CodingContractVersion != codingcontract.ContractVersion ||
		request.GraderManifest.CodingContractVersion != codingcontract.ContractVersion ||
		request.Seed.CodingContractVersion != codingcontract.ContractVersion ||
		request.RunnerManifest.TicketID != request.Seed.TicketID ||
		request.RunnerManifest.CaseID != request.Seed.CaseID ||
		request.RunnerManifest.ProfileCapabilityID != request.Seed.ProfileCapabilityID ||
		request.GraderManifest.CaseID != request.Seed.CaseID ||
		request.GraderManifest.VisibleBundleSHA256 != request.RunnerManifest.VisibleBundleSHA256 ||
		request.GraderManifest.BaseTreeSHA256 != request.RunnerManifest.BaseTreeSHA256 {
		return errors.New("coding certification task identities disagree")
	}
	if err := request.GraderManifest.Validate(now); err != nil {
		return fmt.Errorf("coding certification grader: %w", err)
	}
	if request.RunnerManifest.Limits != request.GraderManifest.ResourcePolicy.CandidateLimits {
		return errors.New("coding certification runner and grader resource limits disagree")
	}
	run := request.runRequest("http://workspace.invalid/capability")
	if err := run.Validate(); err != nil {
		return fmt.Errorf("coding certification run: %w", err)
	}
	manifestSHA, err := CanaryManifestSHA256(request)
	if err != nil || manifestSHA != request.CanaryManifestSHA256 {
		return errors.New("coding certification canary manifest digest mismatch")
	}
	return nil
}

func (request Request) runRequest(workspaceURL string) codingcontract.RunRequest {
	return codingcontract.RunRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              request.Seed.TicketID, CaseID: request.Seed.CaseID,
		ProfileCapabilityID: request.Seed.ProfileCapabilityID,
		RepositoryEpoch:     request.RepositoryEpoch,
		VisibleBundleSHA256: request.RunnerManifest.VisibleBundleSHA256,
		Issue:               request.Issue,
		RuntimePolicy: codingcontract.RuntimePolicy{
			EditablePaths:   append([]string(nil), request.RunnerManifest.EditablePaths...),
			TestCommandIDs:  commandIDs(request.RunnerManifest.TestCommands),
			BuildCommandIDs: commandIDs(request.RunnerManifest.BuildCommands),
		},
		WorkspaceCapabilityURL: workspaceURL,
		InferenceBaseURL:       request.InferenceBaseURL,
		Budgets:                request.Budgets,
	}
}

func commandIDs(commands []codingrunner.CommandSpec) []string {
	result := make([]string, len(commands))
	for index, command := range commands {
		result[index] = command.ID
	}
	return result
}

type canaryCommandProjection struct {
	ID                  string   `json:"id"`
	Argv                []string `json:"argv"`
	TimeoutMilliseconds int64    `json:"timeout_milliseconds"`
}

type canaryLimitsProjection struct {
	MaxBundleBytes      int64  `json:"max_bundle_bytes"`
	MaxWorkspaceBytes   int64  `json:"max_workspace_bytes"`
	MaxFileBytes        int64  `json:"max_file_bytes"`
	MaxPatchBytes       int64  `json:"max_patch_bytes"`
	MaxEntries          int    `json:"max_entries"`
	MaxToolCalls        uint32 `json:"max_tool_calls"`
	MaxReadBytes        int    `json:"max_read_bytes"`
	MaxResponseBytes    int    `json:"max_response_bytes"`
	MaxSearchResults    int    `json:"max_search_results"`
	MaxReplayCacheBytes int64  `json:"max_replay_cache_bytes"`
	MaxTranscriptBytes  int64  `json:"max_transcript_bytes"`
}

type canaryManifestProjection struct {
	Schema                string                    `json:"schema"`
	CodingContractVersion int                       `json:"coding_contract_version"`
	TicketID              string                    `json:"ticket_id"`
	CaseID                string                    `json:"case_id"`
	ProfileCapabilityID   string                    `json:"profile_capability_id"`
	MemoryBundleSHA256    string                    `json:"memory_bundle_sha256"`
	RepositoryEpoch       string                    `json:"repository_epoch"`
	VisibleBundleSHA256   string                    `json:"visible_bundle_sha256"`
	BaseTreeSHA256        string                    `json:"base_tree_sha256"`
	Issue                 codingcontract.Issue      `json:"issue"`
	Budgets               codingcontract.Budgets    `json:"budgets"`
	EditablePaths         []string                  `json:"editable_paths"`
	CreatablePaths        []string                  `json:"creatable_paths"`
	DeletablePaths        []string                  `json:"deletable_paths"`
	TestCommands          []canaryCommandProjection `json:"test_commands"`
	BuildCommands         []canaryCommandProjection `json:"build_commands"`
	RunnerLimits          canaryLimitsProjection    `json:"runner_limits"`
	GraderPlanSHA256      string                    `json:"grader_plan_sha256"`
	GraderResourceSHA256  string                    `json:"grader_resource_sha256"`
	GraderImageDigest     string                    `json:"grader_image_digest"`
	GraderPlatform        string                    `json:"grader_platform"`
	InferenceGrantSHA256  string                    `json:"inference_grant_sha256"`
	SolverModel           string                    `json:"solver_model"`
	SolverProvider        string                    `json:"solver_provider"`
	ProviderRouteProfile  string                    `json:"provider_route_profile"`
}

// CanaryManifestSHA256 binds every non-ephemeral input used by the public
// certification task. Transport capability URLs and wall-clock deadlines are
// deliberately excluded; their authorities are separately attested.
func CanaryManifestSHA256(request Request) (string, error) {
	request = cloneRequest(request)
	project := func(commands []codingrunner.CommandSpec) []canaryCommandProjection {
		result := make([]canaryCommandProjection, len(commands))
		for index, command := range commands {
			result[index] = canaryCommandProjection{
				ID: command.ID, Argv: append([]string(nil), command.Argv...),
				TimeoutMilliseconds: command.Timeout.Milliseconds(),
			}
		}
		return result
	}
	limits := request.RunnerManifest.Limits
	return digestCanonical(canaryManifestProjection{
		Schema: "dittobench-coding-certification-canary-v1", CodingContractVersion: codingcontract.ContractVersion,
		TicketID: request.Seed.TicketID, CaseID: request.Seed.CaseID,
		ProfileCapabilityID: request.Seed.ProfileCapabilityID, MemoryBundleSHA256: request.Seed.MemoryBundleSHA256,
		RepositoryEpoch: request.RepositoryEpoch, VisibleBundleSHA256: request.RunnerManifest.VisibleBundleSHA256,
		BaseTreeSHA256: request.RunnerManifest.BaseTreeSHA256, Issue: request.Issue, Budgets: request.Budgets,
		EditablePaths:  cloneSlice(request.RunnerManifest.EditablePaths),
		CreatablePaths: cloneSlice(request.RunnerManifest.CreatablePaths),
		DeletablePaths: cloneSlice(request.RunnerManifest.DeletablePaths),
		TestCommands:   project(request.RunnerManifest.TestCommands), BuildCommands: project(request.RunnerManifest.BuildCommands),
		RunnerLimits: canaryLimitsProjection{
			MaxBundleBytes: limits.MaxBundleBytes, MaxWorkspaceBytes: limits.MaxWorkspaceBytes,
			MaxFileBytes: limits.MaxFileBytes, MaxPatchBytes: limits.MaxPatchBytes, MaxEntries: limits.MaxEntries,
			MaxToolCalls: limits.MaxToolCalls, MaxReadBytes: limits.MaxReadBytes, MaxResponseBytes: limits.MaxResponseBytes,
			MaxSearchResults: limits.MaxSearchResults, MaxReplayCacheBytes: limits.MaxReplayCacheBytes,
			MaxTranscriptBytes: limits.MaxTranscriptBytes,
		},
		GraderPlanSHA256:     request.GraderManifest.GraderPlanSHA256,
		GraderResourceSHA256: request.GraderManifest.ResourceProfileSHA256,
		GraderImageDigest:    request.GraderManifest.GraderImageDigest,
		GraderPlatform:       request.GraderManifest.GraderPlatform,
		InferenceGrantSHA256: request.InferenceGrantSHA256,
		SolverModel:          request.SolverModel, SolverProvider: request.SolverProvider,
		ProviderRouteProfile: request.ProviderRouteProfile,
	})
}

// Receipt is an expiring shadow capability result. It is deliberately not a
// ScoreReport and weight_eligible is permanently false.
type Receipt struct {
	Schema                           string                         `json:"schema"`
	CodingContractVersion            int                            `json:"coding_contract_version"`
	WeightEligible                   bool                           `json:"weight_eligible"`
	CertificationID                  string                         `json:"certification_id"`
	AgentArtifactSHA256              string                         `json:"agent_artifact_sha256"`
	HarnessInstanceID                string                         `json:"harness_instance_id"`
	CanaryManifestSHA256             string                         `json:"canary_manifest_sha256"`
	IssuedAtUnix                     int64                          `json:"issued_at_unix"`
	ExpiresAtUnix                    int64                          `json:"expires_at_unix"`
	Status                           Status                         `json:"status"`
	FailureStage                     *Stage                         `json:"failure_stage"`
	FailureCode                      *string                        `json:"failure_code"`
	SupportedCodingContractVersions  []int                          `json:"supported_coding_contract_versions"`
	Capabilities                     []string                       `json:"capabilities"`
	MemoryBundleSHA256               string                         `json:"memory_bundle_sha256"`
	VisibleBundleSHA256              string                         `json:"visible_bundle_sha256"`
	BaseTreeSHA256                   string                         `json:"base_tree_sha256"`
	InferenceGrantSHA256             string                         `json:"inference_grant_sha256"`
	ModelEvidence                    *codingcontract.ModelEvidence  `json:"model_evidence"`
	FrozenPatchSHA256                *string                        `json:"frozen_patch_sha256"`
	FrozenSubmissionObjectKey        *string                        `json:"frozen_submission_object_key"`
	ChangedPathRoot                  *string                        `json:"changed_path_root"`
	FinalTreeSHA256                  *string                        `json:"final_tree_sha256"`
	AuthoringEventRoot               *string                        `json:"authoring_event_root"`
	AuthoringTranscriptSHA256        *string                        `json:"authoring_transcript_sha256"`
	AuthoringTranscriptObjectKey     *string                        `json:"authoring_transcript_object_key"`
	AuthoringTranscriptBytes         int64                          `json:"authoring_transcript_bytes"`
	AuthoringEventCount              uint64                         `json:"authoring_event_count"`
	ProtectedPathsIntact             bool                           `json:"protected_paths_intact"`
	CanaryTerminalDomain             *codingcontract.TerminalDomain `json:"canary_terminal_domain"`
	GraderPlanSHA256                 string                         `json:"grader_plan_sha256"`
	GraderExecutionReceiptRootSHA256 *string                        `json:"grader_execution_receipt_root_sha256"`
	CertificationSHA256              string                         `json:"certification_sha256"`
}

type receiptProjection struct {
	Schema                           string                         `json:"schema"`
	CodingContractVersion            int                            `json:"coding_contract_version"`
	WeightEligible                   bool                           `json:"weight_eligible"`
	CertificationID                  string                         `json:"certification_id"`
	AgentArtifactSHA256              string                         `json:"agent_artifact_sha256"`
	HarnessInstanceID                string                         `json:"harness_instance_id"`
	CanaryManifestSHA256             string                         `json:"canary_manifest_sha256"`
	IssuedAtUnix                     int64                          `json:"issued_at_unix"`
	ExpiresAtUnix                    int64                          `json:"expires_at_unix"`
	Status                           Status                         `json:"status"`
	FailureStage                     *Stage                         `json:"failure_stage"`
	FailureCode                      *string                        `json:"failure_code"`
	SupportedCodingContractVersions  []int                          `json:"supported_coding_contract_versions"`
	Capabilities                     []string                       `json:"capabilities"`
	MemoryBundleSHA256               string                         `json:"memory_bundle_sha256"`
	VisibleBundleSHA256              string                         `json:"visible_bundle_sha256"`
	BaseTreeSHA256                   string                         `json:"base_tree_sha256"`
	InferenceGrantSHA256             string                         `json:"inference_grant_sha256"`
	ModelEvidence                    *codingcontract.ModelEvidence  `json:"model_evidence"`
	FrozenPatchSHA256                *string                        `json:"frozen_patch_sha256"`
	FrozenSubmissionObjectKey        *string                        `json:"frozen_submission_object_key"`
	ChangedPathRoot                  *string                        `json:"changed_path_root"`
	FinalTreeSHA256                  *string                        `json:"final_tree_sha256"`
	AuthoringEventRoot               *string                        `json:"authoring_event_root"`
	AuthoringTranscriptSHA256        *string                        `json:"authoring_transcript_sha256"`
	AuthoringTranscriptObjectKey     *string                        `json:"authoring_transcript_object_key"`
	AuthoringTranscriptBytes         int64                          `json:"authoring_transcript_bytes"`
	AuthoringEventCount              uint64                         `json:"authoring_event_count"`
	ProtectedPathsIntact             bool                           `json:"protected_paths_intact"`
	CanaryTerminalDomain             *codingcontract.TerminalDomain `json:"canary_terminal_domain"`
	GraderPlanSHA256                 string                         `json:"grader_plan_sha256"`
	GraderExecutionReceiptRootSHA256 *string                        `json:"grader_execution_receipt_root_sha256"`
}

func (receipt Receipt) projection() receiptProjection {
	return receiptProjection{
		Schema: receipt.Schema, CodingContractVersion: receipt.CodingContractVersion,
		WeightEligible: receipt.WeightEligible, CertificationID: receipt.CertificationID,
		AgentArtifactSHA256: receipt.AgentArtifactSHA256, HarnessInstanceID: receipt.HarnessInstanceID,
		CanaryManifestSHA256: receipt.CanaryManifestSHA256, IssuedAtUnix: receipt.IssuedAtUnix,
		ExpiresAtUnix: receipt.ExpiresAtUnix, Status: receipt.Status,
		FailureStage: cloneStage(receipt.FailureStage), FailureCode: cloneString(receipt.FailureCode),
		SupportedCodingContractVersions: cloneSlice(receipt.SupportedCodingContractVersions),
		Capabilities:                    cloneSlice(receipt.Capabilities),
		MemoryBundleSHA256:              receipt.MemoryBundleSHA256, VisibleBundleSHA256: receipt.VisibleBundleSHA256,
		BaseTreeSHA256: receipt.BaseTreeSHA256, InferenceGrantSHA256: receipt.InferenceGrantSHA256,
		ModelEvidence: cloneModelEvidence(receipt.ModelEvidence), FrozenPatchSHA256: cloneString(receipt.FrozenPatchSHA256),
		FrozenSubmissionObjectKey: cloneString(receipt.FrozenSubmissionObjectKey),
		ChangedPathRoot:           cloneString(receipt.ChangedPathRoot), FinalTreeSHA256: cloneString(receipt.FinalTreeSHA256),
		AuthoringEventRoot:           cloneString(receipt.AuthoringEventRoot),
		AuthoringTranscriptSHA256:    cloneString(receipt.AuthoringTranscriptSHA256),
		AuthoringTranscriptObjectKey: cloneString(receipt.AuthoringTranscriptObjectKey),
		AuthoringTranscriptBytes:     receipt.AuthoringTranscriptBytes, AuthoringEventCount: receipt.AuthoringEventCount,
		ProtectedPathsIntact: receipt.ProtectedPathsIntact,
		CanaryTerminalDomain: cloneDomain(receipt.CanaryTerminalDomain), GraderPlanSHA256: receipt.GraderPlanSHA256,
		GraderExecutionReceiptRootSHA256: cloneString(receipt.GraderExecutionReceiptRootSHA256),
	}
}

func (receipt *Receipt) seal() error {
	digest, err := digestCanonical(receipt.projection())
	if err != nil {
		return err
	}
	receipt.CertificationSHA256 = digest
	return receipt.Validate()
}

// Validate checks the complete known-field certification receipt and digest.
func (receipt Receipt) Validate() error {
	if receipt.Schema != CertificationSchema || receipt.CodingContractVersion != codingcontract.ContractVersion ||
		receipt.WeightEligible || !validIdentifier(receipt.CertificationID, 256) ||
		!lowerSHA256(receipt.AgentArtifactSHA256) || !validIdentifier(receipt.HarnessInstanceID, 256) ||
		!lowerSHA256(receipt.CanaryManifestSHA256) || receipt.IssuedAtUnix <= 0 ||
		receipt.ExpiresAtUnix <= receipt.IssuedAtUnix || receipt.ExpiresAtUnix-receipt.IssuedAtUnix > int64(maximumTTL/time.Second) ||
		!lowerSHA256(receipt.MemoryBundleSHA256) || !lowerSHA256(receipt.VisibleBundleSHA256) ||
		!lowerSHA256(receipt.BaseTreeSHA256) || !lowerSHA256(receipt.InferenceGrantSHA256) ||
		!lowerSHA256(receipt.GraderPlanSHA256) ||
		receipt.AuthoringTranscriptBytes < 0 || !lowerSHA256(receipt.CertificationSHA256) {
		return errors.New("coding certification receipt identity is invalid")
	}
	if receipt.SupportedCodingContractVersions == nil || receipt.Capabilities == nil ||
		!slices.IsSorted(receipt.SupportedCodingContractVersions) || !uniqueInts(receipt.SupportedCodingContractVersions) ||
		!slices.IsSorted(receipt.Capabilities) || !uniqueStrings(receipt.Capabilities) {
		return errors.New("coding certification capabilities are not canonical")
	}
	for _, version := range receipt.SupportedCodingContractVersions {
		if version <= 0 || version > 1_000_000 {
			return errors.New("coding certification version is outside bounds")
		}
	}
	for _, capability := range receipt.Capabilities {
		if !validIdentifier(capability, 128) {
			return errors.New("coding certification capability is invalid")
		}
	}
	if receipt.Status != StatusUnsupported && receipt.Status != StatusFailed && receipt.Status != StatusCertified {
		return errors.New("coding certification status is invalid")
	}
	if (receipt.AuthoringTranscriptBytes == 0) != (receipt.AuthoringEventCount == 0) {
		return errors.New("coding certification transcript accounting is inconsistent")
	}
	if receipt.FailureStage != nil && *receipt.FailureStage != StageHealth && *receipt.FailureStage != StageSeed &&
		*receipt.FailureStage != StageRun && *receipt.FailureStage != StageFreeze && *receipt.FailureStage != StageGrade {
		return errors.New("coding certification failure stage is invalid")
	}
	if receipt.FailureCode != nil && !validIdentifier(*receipt.FailureCode, 128) {
		return errors.New("coding certification failure code is invalid")
	}
	if receipt.Status == StatusCertified {
		if receipt.FailureStage != nil || receipt.FailureCode != nil || receipt.FrozenPatchSHA256 == nil ||
			receipt.FrozenSubmissionObjectKey == nil || receipt.ChangedPathRoot == nil ||
			receipt.FinalTreeSHA256 == nil || receipt.AuthoringEventRoot == nil ||
			receipt.AuthoringTranscriptSHA256 == nil || receipt.AuthoringTranscriptObjectKey == nil ||
			receipt.ModelEvidence == nil || receipt.CanaryTerminalDomain == nil ||
			*receipt.CanaryTerminalDomain != codingcontract.DomainResolved ||
			receipt.GraderExecutionReceiptRootSHA256 == nil || receipt.AuthoringTranscriptBytes <= 0 ||
			receipt.AuthoringEventCount == 0 || !receipt.ProtectedPathsIntact {
			return errors.New("certified coding receipt lacks complete canary evidence")
		}
		health := HealthResponse{
			Status: "ok", SupportedCodingContractVersions: receipt.SupportedCodingContractVersions,
			Capabilities: receipt.Capabilities,
		}
		if !health.supportsCodingV1() {
			return errors.New("certified coding receipt lacks required advertised capabilities")
		}
	} else if receipt.FailureStage == nil || receipt.FailureCode == nil {
		return errors.New("non-certified coding receipt lacks a terminal reason")
	}
	if receipt.Status == StatusUnsupported {
		if *receipt.FailureStage != StageHealth || receipt.FrozenPatchSHA256 != nil ||
			receipt.FrozenSubmissionObjectKey != nil || receipt.ChangedPathRoot != nil ||
			receipt.FinalTreeSHA256 != nil || receipt.AuthoringEventRoot != nil ||
			receipt.AuthoringTranscriptSHA256 != nil || receipt.AuthoringTranscriptObjectKey != nil ||
			receipt.ModelEvidence != nil || receipt.AuthoringTranscriptBytes != 0 ||
			receipt.AuthoringEventCount != 0 || receipt.ProtectedPathsIntact ||
			receipt.CanaryTerminalDomain != nil || receipt.GraderExecutionReceiptRootSHA256 != nil {
			return errors.New("unsupported coding receipt contains execution evidence")
		}
	}
	for _, value := range []*string{
		receipt.FrozenPatchSHA256, receipt.ChangedPathRoot, receipt.FinalTreeSHA256, receipt.AuthoringEventRoot,
		receipt.AuthoringTranscriptSHA256, receipt.GraderExecutionReceiptRootSHA256,
	} {
		if value != nil && !lowerSHA256(*value) {
			return errors.New("coding certification evidence digest is invalid")
		}
	}
	if receipt.AuthoringTranscriptObjectKey != nil {
		if receipt.AuthoringTranscriptSHA256 == nil ||
			*receipt.AuthoringTranscriptObjectKey != "sha256/"+*receipt.AuthoringTranscriptSHA256 {
			return errors.New("coding certification transcript object key is invalid")
		}
	} else if receipt.AuthoringTranscriptSHA256 != nil {
		return errors.New("coding certification transcript bytes are not durably addressed")
	}
	if receipt.FrozenSubmissionObjectKey != nil {
		if receipt.FrozenPatchSHA256 == nil ||
			*receipt.FrozenSubmissionObjectKey != "sha256/"+*receipt.FrozenPatchSHA256 {
			return errors.New("coding frozen-submission object key is invalid")
		}
	} else if receipt.FrozenPatchSHA256 != nil {
		return errors.New("coding frozen submission is not durably addressed")
	}
	if receipt.ModelEvidence != nil {
		if err := receipt.ModelEvidence.Validate(); err != nil ||
			receipt.ModelEvidence.InferenceGrantSHA256 != receipt.InferenceGrantSHA256 {
			return errors.New("coding certification model evidence is invalid")
		}
		if receipt.Status == StatusCertified && receipt.ModelEvidence.UsageStatus != codingcontract.ModelUsageComplete {
			return errors.New("certified coding receipt lacks complete model evidence")
		}
	}
	if receipt.CanaryTerminalDomain != nil {
		switch *receipt.CanaryTerminalDomain {
		case codingcontract.DomainResolved, codingcontract.DomainRepairFailure,
			codingcontract.DomainCandidateIntegrity:
		default:
			return errors.New("coding certification terminal domain is invalid")
		}
	}
	digest, err := digestCanonical(receipt.projection())
	if err != nil || digest != receipt.CertificationSHA256 {
		return errors.New("coding certification receipt digest mismatch")
	}
	return nil
}

// ValidateAt checks structural integrity plus the active issuance window. A
// persisted adapter must call this before treating a receipt as current.
func (receipt Receipt) ValidateAt(now time.Time) error {
	if err := receipt.Validate(); err != nil {
		return err
	}
	unix := now.UTC().Unix()
	if unix < receipt.IssuedAtUnix || unix >= receipt.ExpiresAtUnix {
		return errors.New("coding certification receipt is not currently active")
	}
	return nil
}

func digestCanonical(value any) (string, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		return "", err
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		return "", err
	}
	if output.Len() > codingcontract.MaxCanonicalJSONBytes {
		return "", errors.New("canonical coding certification JSON exceeds 4 MiB")
	}
	digest := sha256.Sum256(output.Bytes())
	return hex.EncodeToString(digest[:]), nil
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneStage(value *Stage) *Stage {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneDomain(value *codingcontract.TerminalDomain) *codingcontract.TerminalDomain {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneModelEvidence(value *codingcontract.ModelEvidence) *codingcontract.ModelEvidence {
	if value == nil {
		return nil
	}
	copy := *value
	copy.ProviderReceiptSetSHA256 = cloneString(value.ProviderReceiptSetSHA256)
	return &copy
}

func lowerSHA256(value string) bool {
	if len(value) != 64 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func uniqueStrings(values []string) bool {
	for index := 1; index < len(values); index++ {
		if values[index] == values[index-1] {
			return false
		}
	}
	return true
}

func uniqueInts(values []int) bool {
	for index := 1; index < len(values); index++ {
		if values[index] == values[index-1] {
			return false
		}
	}
	return true
}
