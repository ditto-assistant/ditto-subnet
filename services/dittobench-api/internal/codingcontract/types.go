// Package codingcontract defines the shadow-only DittoBench Coding v1 wire.
//
// The package is deliberately disconnected from active scoring. Its canonical
// models and digests are evidence building blocks for future shadow runtimes.
package codingcontract

const (
	ContractVersion           = 1
	ResolvedRepairScoreMicros = 1_000_000
	MaxCanonicalJSONBytes     = 1 << 20
)

type TerminalDomain string

const (
	DomainResolved                TerminalDomain = "resolved"
	DomainRepairFailure           TerminalDomain = "repair_failure"
	DomainValidatorInfrastructure TerminalDomain = "validator_infrastructure"
	DomainTaskInvalid             TerminalDomain = "task_invalid"
	DomainIntegrityIncident       TerminalDomain = "integrity_incident"
)

type ManifestTask struct {
	CaseID                 string `json:"case_id"`
	VariantID              string `json:"variant_id"`
	ProfileCapabilityID    string `json:"profile_capability_id"`
	VisibleBundleSHA256    string `json:"visible_bundle_sha256"`
	BaseTreeSHA256         string `json:"base_tree_sha256"`
	MemoryBundleSHA256     string `json:"memory_bundle_sha256"`
	EnvironmentImageDigest string `json:"environment_image_digest"`
	EnvironmentPlatform    string `json:"environment_platform"`
	ResourceProfileSHA256  string `json:"resource_profile_sha256"`
	GraderBundleSHA256     string `json:"grader_bundle_sha256"`
	GraderImageDigest      string `json:"grader_image_digest"`
	TestManifestSHA256     string `json:"test_manifest_sha256"`
}

type RunManifest struct {
	Schema                string         `json:"schema"`
	CodingContractVersion int            `json:"coding_contract_version"`
	WeightEligible        bool           `json:"weight_eligible"`
	TicketID              string         `json:"ticket_id"`
	AgentID               string         `json:"agent_id"`
	AgentArtifactSHA256   string         `json:"agent_artifact_sha256"`
	CorpusReleaseID       string         `json:"corpus_release_id"`
	CatalogMerkleRoot     string         `json:"catalog_merkle_root"`
	SelectionDerivationID string         `json:"selection_derivation_id"`
	SelectionBlockNumber  uint64         `json:"selection_block_number"`
	SelectionBlockHash    string         `json:"selection_block_hash"`
	TaskSetID             string         `json:"task_set_id"`
	TaskSetManifestSHA256 string         `json:"task_set_manifest_sha256"`
	Tasks                 []ManifestTask `json:"tasks"`
}

type VisibleMemory struct {
	MemoryID               string   `json:"memory_id"`
	RepositoryCapabilityID *string  `json:"repository_capability_id"`
	FactGroupID            *string  `json:"fact_group_id"`
	Scope                  string   `json:"scope"`
	Type                   string   `json:"type"`
	Content                string   `json:"content"`
	ValidFromEpoch         *string  `json:"valid_from_epoch"`
	ValidUntilEpoch        *string  `json:"valid_until_epoch"`
	Supersedes             []string `json:"supersedes"`
	ConfidenceMicros       uint32   `json:"confidence_micros"`
}

type SeedRequest struct {
	CodingContractVersion int             `json:"coding_contract_version"`
	TicketID              string          `json:"ticket_id"`
	CaseID                string          `json:"case_id"`
	ProfileCapabilityID   string          `json:"profile_capability_id"`
	MemoryBundleSHA256    string          `json:"memory_bundle_sha256"`
	Memories              []VisibleMemory `json:"memories"`
}

type Issue struct {
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Constraints []string `json:"constraints"`
}

type RuntimePolicy struct {
	EditablePaths   []string `json:"editable_paths"`
	TestCommandIDs  []string `json:"test_command_ids"`
	BuildCommandIDs []string `json:"build_command_ids"`
}

type Budgets struct {
	ModelInputTokens   uint64 `json:"model_input_tokens"`
	ModelOutputTokens  uint64 `json:"model_output_tokens"`
	WorkspaceToolCalls uint32 `json:"workspace_tool_calls"`
	WallTimeSeconds    uint64 `json:"wall_time_seconds"`
}

type RunRequest struct {
	CodingContractVersion  int           `json:"coding_contract_version"`
	TicketID               string        `json:"ticket_id"`
	CaseID                 string        `json:"case_id"`
	ProfileCapabilityID    string        `json:"profile_capability_id"`
	RepositoryEpoch        string        `json:"repository_epoch"`
	VisibleBundleSHA256    string        `json:"visible_bundle_sha256"`
	Issue                  Issue         `json:"issue"`
	RuntimePolicy          RuntimePolicy `json:"runtime_policy"`
	WorkspaceCapabilityURL string        `json:"workspace_capability_url"`
	InferenceBaseURL       string        `json:"inference_base_url"`
	Budgets                Budgets       `json:"budgets"`
}

type ModelEvidence struct {
	Model                    string `json:"model"`
	Provider                 string `json:"provider"`
	ProviderRouteProfile     string `json:"provider_route_profile"`
	ReasoningEffort          string `json:"reasoning_effort"`
	PromptSHA256             string `json:"prompt_sha256"`
	ToolSchemaSHA256         string `json:"tool_schema_sha256"`
	ProviderReceiptSetSHA256 string `json:"provider_receipt_set_sha256"`
	Requests                 uint64 `json:"requests"`
	PromptTokens             uint64 `json:"prompt_tokens"`
	CompletionTokens         uint64 `json:"completion_tokens"`
	TotalTokens              uint64 `json:"total_tokens"`
	CostUSDMicros            uint64 `json:"cost_usd_micros"`
	RetryCount               uint32 `json:"retry_count"`
}

type AuthoringEvidence struct {
	Model                     ModelEvidence `json:"model"`
	AuthoringEventRoot        string        `json:"authoring_event_root"`
	AuthoringTranscriptSHA256 string        `json:"authoring_transcript_sha256"`
	FrozenPatchSHA256         string        `json:"frozen_patch_sha256"`
	ChangedPathRoot           string        `json:"changed_path_root"`
	FinalTreeSHA256           string        `json:"final_tree_sha256"`
	ChangedPathCount          uint32        `json:"changed_path_count"`
	ChangedBytes              uint64        `json:"changed_bytes"`
	ProtectedPathsIntact      bool          `json:"protected_paths_intact"`
}

type BuildEvidence struct {
	CommandID string `json:"command_id"`
	Required  bool   `json:"required"`
	Passed    bool   `json:"passed"`
}

type TestGroupEvidence struct {
	Group  string `json:"group"`
	Passed uint32 `json:"passed"`
	Total  uint32 `json:"total"`
}

type GraderEvidence struct {
	GraderBundleSHA256          string              `json:"grader_bundle_sha256"`
	GraderImageDigest           string              `json:"grader_image_digest"`
	TestManifestSHA256          string              `json:"test_manifest_sha256"`
	GraderIntegrityBeforeSHA256 string              `json:"grader_integrity_before_sha256"`
	GraderIntegrityAfterSHA256  string              `json:"grader_integrity_after_sha256"`
	Build                       BuildEvidence       `json:"build"`
	TestGroups                  []TestGroupEvidence `json:"test_groups"`
}

type TaskEvidence struct {
	Schema                string             `json:"schema"`
	CodingContractVersion int                `json:"coding_contract_version"`
	WeightEligible        bool               `json:"weight_eligible"`
	TicketID              string             `json:"ticket_id"`
	AgentID               string             `json:"agent_id"`
	AgentArtifactSHA256   string             `json:"agent_artifact_sha256"`
	CorpusReleaseID       string             `json:"corpus_release_id"`
	TaskSetID             string             `json:"task_set_id"`
	TaskSetManifestSHA256 string             `json:"task_set_manifest_sha256"`
	Task                  ManifestTask       `json:"task"`
	Authoring             *AuthoringEvidence `json:"authoring"`
	Grader                *GraderEvidence    `json:"grader"`
	TerminalDomain        TerminalDomain     `json:"terminal_domain"`
	FailureCode           *string            `json:"failure_code"`
	RepairScoreMicros     uint32             `json:"repair_score_micros"`
}

type TaskResult struct {
	CaseID             string         `json:"case_id"`
	VariantID          string         `json:"variant_id"`
	TaskEvidenceSHA256 string         `json:"task_evidence_sha256"`
	TerminalDomain     TerminalDomain `json:"terminal_domain"`
	RepairScoreMicros  uint32         `json:"repair_score_micros"`
}

type RunEvidence struct {
	Schema                 string       `json:"schema"`
	CodingContractVersion  int          `json:"coding_contract_version"`
	WeightEligible         bool         `json:"weight_eligible"`
	RunManifestSHA256      string       `json:"run_manifest_sha256"`
	TaskSetManifestSHA256  string       `json:"task_set_manifest_sha256"`
	Tasks                  []TaskResult `json:"tasks"`
	ResolvedCount          uint32       `json:"resolved_count"`
	RepairFailureCount     uint32       `json:"repair_failure_count"`
	InfrastructureCount    uint32       `json:"infrastructure_count"`
	InvalidCount           uint32       `json:"invalid_count"`
	IntegrityIncidentCount uint32       `json:"integrity_incident_count"`
	ScoreableTaskCount     uint32       `json:"scoreable_task_count"`
	RepairMeanMicros       uint32       `json:"repair_mean_micros"`
}
