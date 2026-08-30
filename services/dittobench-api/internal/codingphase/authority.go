package codingphase

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecution"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsupervisor"
	"github.com/google/uuid"
)

type authoringLeaseWire struct {
	Schema                string                       `json:"schema"`
	CodingContractVersion int                          `json:"coding_contract_version"`
	WeightEligible        bool                         `json:"weight_eligible"`
	TicketID              string                       `json:"ticket_id"`
	TicketDeadline        time.Time                    `json:"ticket_deadline"`
	CodingRunID           string                       `json:"coding_run_id"`
	RunManifestSHA256     string                       `json:"run_manifest_sha256"`
	TaskSetManifestSHA256 string                       `json:"task_set_manifest_sha256"`
	RepositoryEpoch       string                       `json:"repository_epoch"`
	IssueSHA256           string                       `json:"issue_sha256"`
	RuntimePolicySHA256   string                       `json:"runtime_policy_sha256"`
	BudgetsSHA256         string                       `json:"budgets_sha256"`
	Issue                 codingcontract.Issue         `json:"issue"`
	RuntimePolicy         codingcontract.RuntimePolicy `json:"runtime_policy"`
	Budgets               codingcontract.Budgets       `json:"budgets"`
	RunnerPlanSHA256      string                       `json:"runner_plan_sha256"`
	RunnerPlan            json.RawMessage              `json:"runner_plan"`
	RunManifest           json.RawMessage              `json:"run_manifest"`
	Capabilities          []json.RawMessage            `json:"capabilities"`
}

type gradingLeaseWire struct {
	Schema                    string            `json:"schema"`
	CodingContractVersion     int               `json:"coding_contract_version"`
	WeightEligible            bool              `json:"weight_eligible"`
	AgentID                   string            `json:"agent_id"`
	RunRowID                  string            `json:"run_row_id"`
	TicketID                  string            `json:"ticket_id"`
	TicketDeadline            time.Time         `json:"ticket_deadline"`
	CodingRunID               string            `json:"coding_run_id"`
	RunManifestSHA256         string            `json:"run_manifest_sha256"`
	TaskSetManifestSHA256     string            `json:"task_set_manifest_sha256"`
	FreezeID                  string            `json:"freeze_id"`
	AuthoringEvidenceSHA256   string            `json:"authoring_evidence_sha256"`
	FrozenPatchSHA256         string            `json:"frozen_patch_sha256"`
	FrozenSubmissionObjectKey string            `json:"frozen_submission_object_key"`
	RunManifest               json.RawMessage   `json:"run_manifest"`
	GraderPlan                json.RawMessage   `json:"grader_plan"`
	GraderResourceProfile     json.RawMessage   `json:"grader_resource_profile"`
	Capabilities              []json.RawMessage `json:"capabilities"`
}

type authoringOutcomeWire struct {
	Evidence                      json.RawMessage `json:"evidence"`
	AuthoringTranscriptObjectKey  string          `json:"authoring_transcript_object_key"`
	AuthoringTranscriptBytes      int64           `json:"authoring_transcript_bytes"`
	AuthoringEventCount           uint64          `json:"authoring_event_count"`
	FrozenSubmissionObjectKey     string          `json:"frozen_submission_object_key"`
	CapabilitiesRevoked           bool            `json:"capabilities_revoked"`
	AuthoringEnvironmentDestroyed bool            `json:"authoring_environment_destroyed"`
}

type grantWire struct {
	Schema                   string    `json:"schema"`
	CodingContractVersion    int       `json:"coding_contract_version"`
	WeightEligible           bool      `json:"weight_eligible"`
	Status                   string    `json:"status"`
	GrantID                  string    `json:"grant_id"`
	TicketID                 string    `json:"ticket_id"`
	RunRowID                 string    `json:"run_row_id"`
	CaseID                   string    `json:"case_id"`
	ProfileCapabilityID      string    `json:"profile_capability_id"`
	InferenceGrantSHA256     string    `json:"inference_grant_sha256"`
	Model                    string    `json:"model"`
	ProviderAPI              string    `json:"provider_api"`
	ProviderRoute            string    `json:"provider_route"`
	ReceiptProvider          string    `json:"receipt_provider"`
	ProviderRouteProfile     string    `json:"provider_route_profile"`
	ProviderAccountGuardrail string    `json:"provider_account_guardrail"`
	ProviderPipelinePolicy   string    `json:"provider_pipeline_policy"`
	ProviderCachePolicy      string    `json:"provider_cache_policy"`
	ReasoningEffort          string    `json:"reasoning_effort"`
	RequestBudget            uint32    `json:"request_budget"`
	PromptTokenBudget        uint64    `json:"prompt_token_budget"`
	CompletionTokenBudget    uint64    `json:"completion_token_budget"`
	CostBudgetUSDMicros      uint64    `json:"cost_budget_usd_micros"`
	ExpiresAt                time.Time `json:"expires_at"`
	Generation               uint32    `json:"generation"`
	Bearer                   string    `json:"bearer"`
	ProxyURL                 string    `json:"proxy_url"`
}

type authoringAuthority struct {
	lease          authoringLeaseWire
	manifest       codingcontract.RunManifest
	task           codingcontract.ManifestTask
	runnerPlan     codingexecution.RunnerPlan
	runnerManifest codingrunner.Manifest
	visible        codingartifacts.Capability
	memory         codingartifacts.Capability
	resource       codingartifacts.Capability
}

type gradingAuthority struct {
	lease        gradingLeaseWire
	manifest     codingcontract.RunManifest
	task         codingcontract.ManifestTask
	authoring    codingcontract.AuthoringEvidence
	outcome      authoringOutcomeWire
	graderPlan   codingexecution.GraderPlan
	resourcePlan codingexecution.ResourceProfile
	visible      codingartifacts.Capability
	resource     codingartifacts.Capability
	grader       codingartifacts.Capability
	spec         codingattempt.GradingSpec
}

func parseAuthoringAuthority(
	request codingsupervisor.Request,
	policy codingcontract.InferencePolicy,
	now time.Time,
) (authoringAuthority, error) {
	var zero authoringAuthority
	var lease authoringLeaseWire
	if err := parseRequiredObject(request.Lease, &lease, []string{
		"schema", "coding_contract_version", "weight_eligible", "ticket_id", "ticket_deadline",
		"coding_run_id", "run_manifest_sha256", "task_set_manifest_sha256", "repository_epoch",
		"issue_sha256", "runtime_policy_sha256", "budgets_sha256", "issue", "runtime_policy",
		"budgets", "runner_plan_sha256", "runner_plan", "run_manifest", "capabilities",
	}); err != nil {
		return zero, fmt.Errorf("%w: authoring lease", ErrInvalid)
	}
	if lease.Schema != "dittobench-coding-authoring-lease-v1" ||
		lease.CodingContractVersion != codingcontract.ContractVersion || lease.WeightEligible ||
		lease.TicketID != request.TicketID || lease.CodingRunID != request.CodingRunID ||
		!lease.TicketDeadline.Equal(request.Deadline) || !validPhaseDeadline(request.Deadline, now) ||
		!validIdentifier(lease.RepositoryEpoch, 256) {
		return zero, fmt.Errorf("%w: authoring lease identity", ErrInvalid)
	}
	manifest, err := codingcontract.ParseRunManifest(lease.RunManifest)
	if err != nil || len(manifest.Tasks) != 1 {
		return zero, fmt.Errorf("%w: authoring run manifest", ErrInvalid)
	}
	policySHA, policyDigestErr := codingcontract.InferencePolicySHA256(policy)
	manifestSHA, digestErr := codingcontract.Digest(manifest)
	if digestErr != nil || manifestSHA != lease.RunManifestSHA256 || manifest.CodingRunID != request.CodingRunID ||
		manifest.TaskSetManifestSHA256 != lease.TaskSetManifestSHA256 || !canonicalUUID(manifest.AgentID) ||
		policyDigestErr != nil || manifest.InferenceGrantSHA256 != policySHA ||
		manifest.GraderContractSHA256 != codinggrader.GraderContractSHA256() {
		return zero, fmt.Errorf("%w: authoring manifest commitment", ErrInvalid)
	}
	plan, err := codingexecution.ParseRunnerPlan(lease.RunnerPlan)
	planSHA, planDigestErr := codingexecution.RunnerPlanSHA256(plan)
	issueSHA, issueErr := codingcontract.IssueDigest(lease.Issue)
	runtimePolicySHA, policyErr := codingcontract.RuntimePolicyDigest(lease.RuntimePolicy)
	budgetsSHA, budgetsErr := codingcontract.BudgetsDigest(lease.Budgets)
	if err != nil || planDigestErr != nil || issueErr != nil || policyErr != nil || budgetsErr != nil ||
		planSHA != lease.RunnerPlanSHA256 || issueSHA != lease.IssueSHA256 ||
		runtimePolicySHA != lease.RuntimePolicySHA256 || budgetsSHA != lease.BudgetsSHA256 ||
		!runtimePolicyMatchesPlan(lease.RuntimePolicy, plan) {
		return zero, fmt.Errorf("%w: authoring visible authority", ErrInvalid)
	}
	task := manifest.Tasks[0]
	if plan.CaseID != task.CaseID || plan.VisibleBundleSHA256 != task.VisibleBundleSHA256 ||
		plan.BaseTreeSHA256 != task.BaseTreeSHA256 {
		return zero, fmt.Errorf("%w: authoring selected task", ErrInvalid)
	}
	runnerManifest, err := plan.Manifest(codingexecution.RunnerBinding{
		TicketID: request.TicketID, ProfileCapabilityID: task.ProfileCapabilityID, Deadline: request.Deadline,
	}, now)
	if err != nil {
		return zero, fmt.Errorf("%w: authoring runner manifest", ErrInvalid)
	}
	capabilities, err := decodeCapabilities(lease.Capabilities, request.TicketID, request.Deadline,
		codingartifacts.PhaseAuthoring, []codingartifacts.Kind{
			codingartifacts.KindVisibleBundle, codingartifacts.KindMemoryBundle, codingartifacts.KindResourceProfile,
		})
	if err != nil || capabilities[0].SHA256 != task.VisibleBundleSHA256 ||
		capabilities[1].SHA256 != task.MemoryBundleSHA256 || capabilities[2].SHA256 != task.ResourceProfileSHA256 {
		return zero, fmt.Errorf("%w: authoring artifact capabilities", ErrInvalid)
	}
	return authoringAuthority{
		lease: lease, manifest: manifest, task: task, runnerPlan: plan, runnerManifest: runnerManifest,
		visible: capabilities[0], memory: capabilities[1], resource: capabilities[2],
	}, nil
}

func parseGradingAuthority(
	request codingsupervisor.Request,
	policy codingcontract.InferencePolicy,
	now time.Time,
) (gradingAuthority, error) {
	var zero gradingAuthority
	var lease gradingLeaseWire
	if err := parseRequiredObject(request.Lease, &lease, []string{
		"schema", "coding_contract_version", "weight_eligible", "agent_id", "run_row_id", "ticket_id",
		"ticket_deadline", "coding_run_id", "run_manifest_sha256", "task_set_manifest_sha256", "freeze_id",
		"authoring_evidence_sha256", "frozen_patch_sha256", "frozen_submission_object_key", "run_manifest",
		"grader_plan", "grader_resource_profile", "capabilities",
	}); err != nil {
		return zero, fmt.Errorf("%w: grading lease", ErrInvalid)
	}
	if lease.Schema != "dittobench-coding-grading-lease-v1" ||
		lease.CodingContractVersion != codingcontract.ContractVersion || lease.WeightEligible ||
		lease.TicketID != request.TicketID || lease.CodingRunID != request.CodingRunID ||
		!lease.TicketDeadline.Equal(request.Deadline) || !validPhaseDeadline(request.Deadline, now) ||
		!canonicalUUID(lease.AgentID) || !canonicalUUID(lease.RunRowID) || !canonicalUUID(lease.FreezeID) ||
		lease.FrozenSubmissionObjectKey != "sha256/"+lease.FrozenPatchSHA256 {
		return zero, fmt.Errorf("%w: grading lease identity", ErrInvalid)
	}
	manifest, err := codingcontract.ParseRunManifest(lease.RunManifest)
	if err != nil || len(manifest.Tasks) != 1 {
		return zero, fmt.Errorf("%w: grading run manifest", ErrInvalid)
	}
	manifestSHA, digestErr := codingcontract.Digest(manifest)
	policySHA, policyDigestErr := codingcontract.InferencePolicySHA256(policy)
	if digestErr != nil || manifestSHA != lease.RunManifestSHA256 || manifest.AgentID != lease.AgentID ||
		manifest.CodingRunID != request.CodingRunID || manifest.TaskSetManifestSHA256 != lease.TaskSetManifestSHA256 ||
		policyDigestErr != nil || manifest.InferenceGrantSHA256 != policySHA ||
		manifest.GraderContractSHA256 != codinggrader.GraderContractSHA256() {
		return zero, fmt.Errorf("%w: grading manifest commitment", ErrInvalid)
	}
	plan, err := codingexecution.ParseGraderPlan(lease.GraderPlan)
	resource, resourceErr := codingexecution.ParseResourceProfile(lease.GraderResourceProfile)
	planSHA, planDigestErr := codingexecution.GraderPlanSHA256(plan)
	resourceSHA, resourceDigestErr := codingexecution.ResourceProfileSHA256(resource)
	task := manifest.Tasks[0]
	if err != nil || resourceErr != nil || planDigestErr != nil || resourceDigestErr != nil ||
		planSHA != task.GraderPlanSHA256 || resourceSHA != task.ResourceProfileSHA256 ||
		plan.CaseID != task.CaseID || plan.VariantID != task.VariantID ||
		plan.VisibleBundleSHA256 != task.VisibleBundleSHA256 || plan.BaseTreeSHA256 != task.BaseTreeSHA256 ||
		plan.GraderBundleSHA256 != task.GraderBundleSHA256 || plan.GraderImageDigest != task.GraderImageDigest ||
		plan.TestManifestSHA256 != task.TestManifestSHA256 {
		return zero, fmt.Errorf("%w: grading protected authority", ErrInvalid)
	}
	graderManifest, err := plan.Manifest(resource, request.Deadline, now)
	if err != nil {
		return zero, fmt.Errorf("%w: grading manifest", ErrInvalid)
	}
	var outcome authoringOutcomeWire
	if err := parseRequiredObject(request.Authoring, &outcome, []string{
		"evidence", "authoring_transcript_object_key", "authoring_transcript_bytes",
		"authoring_event_count", "frozen_submission_object_key", "capabilities_revoked",
		"authoring_environment_destroyed",
	}); err != nil || !outcome.CapabilitiesRevoked || !outcome.AuthoringEnvironmentDestroyed {
		return zero, fmt.Errorf("%w: grading authoring outcome", ErrInvalid)
	}
	var authoring codingcontract.AuthoringEvidence
	if err := parseRequiredObject(outcome.Evidence, &authoring, []string{
		"model", "authoring_event_root", "authoring_transcript_sha256", "frozen_patch_sha256",
		"changed_path_root", "final_tree_sha256", "changed_path_count", "changed_bytes", "protected_paths_intact",
	}); err != nil || authoring.Validate() != nil {
		return zero, fmt.Errorf("%w: grading authoring evidence", ErrInvalid)
	}
	authoringSHA, err := codingcontract.AuthoringEvidenceDigest(authoring)
	_, modelErr := codingcontract.InferenceModelEvidenceSHA256(policy, authoring.Model)
	if err != nil || modelErr != nil || authoringSHA != lease.AuthoringEvidenceSHA256 ||
		authoring.Model.InferenceGrantSHA256 != manifest.InferenceGrantSHA256 ||
		authoring.FrozenPatchSHA256 != lease.FrozenPatchSHA256 ||
		outcome.FrozenSubmissionObjectKey != lease.FrozenSubmissionObjectKey ||
		outcome.AuthoringTranscriptObjectKey != "sha256/"+authoring.AuthoringTranscriptSHA256 ||
		outcome.AuthoringTranscriptBytes <= 0 || outcome.AuthoringTranscriptBytes > 512<<20 ||
		outcome.AuthoringEventCount == 0 || outcome.AuthoringEventCount > 1_000 {
		return zero, fmt.Errorf("%w: grading freeze authority", ErrInvalid)
	}
	capabilities, err := decodeCapabilities(lease.Capabilities, request.TicketID, request.Deadline,
		codingartifacts.PhaseGrading, []codingartifacts.Kind{
			codingartifacts.KindVisibleBundle, codingartifacts.KindResourceProfile, codingartifacts.KindGraderBundle,
		})
	if err != nil || capabilities[0].SHA256 != task.VisibleBundleSHA256 ||
		capabilities[1].SHA256 != task.ResourceProfileSHA256 || capabilities[2].SHA256 != task.GraderBundleSHA256 {
		return zero, fmt.Errorf("%w: grading artifact capabilities", ErrInvalid)
	}
	spec := codingattempt.GradingSpec{
		Binding: codingattempt.Binding{
			TicketID: request.TicketID, CaseID: task.CaseID,
			ProfileCapabilityID: task.ProfileCapabilityID, Deadline: request.Deadline,
		},
		FreezeID: lease.FreezeID, AuthoringEvidenceSHA256: lease.AuthoringEvidenceSHA256,
		FrozenSubmissionKey: lease.FrozenSubmissionObjectKey, FrozenPatchSHA256: lease.FrozenPatchSHA256,
		VisibleBundle: capabilities[0], ResourceProfile: capabilities[1], GraderBundle: capabilities[2],
		GraderManifest: graderManifest,
	}
	return gradingAuthority{
		lease: lease, manifest: manifest, task: task, authoring: authoring, outcome: outcome,
		graderPlan: plan, resourcePlan: resource, visible: capabilities[0],
		resource: capabilities[1], grader: capabilities[2], spec: spec,
	}, nil
}

func parseGrant(
	body json.RawMessage,
	request codingsupervisor.Request,
	authority authoringAuthority,
	policy codingcontract.InferencePolicy,
	harnessInstanceID string,
	attemptID string,
	brokerPublicKey string,
	brokerPrivateKey []byte,
	now time.Time,
) (codingplatform.GrantCapability, error) {
	var zero codingplatform.GrantCapability
	var grant grantWire
	if err := parseRequiredObject(body, &grant, []string{
		"schema", "coding_contract_version", "weight_eligible", "status", "grant_id", "ticket_id",
		"run_row_id", "case_id", "profile_capability_id", "inference_grant_sha256", "model",
		"provider_api", "provider_route", "receipt_provider", "provider_route_profile",
		"provider_account_guardrail", "provider_pipeline_policy", "provider_cache_policy",
		"reasoning_effort", "request_budget", "prompt_token_budget", "completion_token_budget",
		"cost_budget_usd_micros", "expires_at", "generation", "bearer", "proxy_url",
	}); err != nil {
		return zero, fmt.Errorf("%w: inference grant", ErrInvalid)
	}
	policySHA, err := codingcontract.InferencePolicySHA256(policy)
	requestBudget := codingcontract.EffectiveInferenceRequestBudget(authority.lease.Budgets.WorkspaceToolCalls)
	if err != nil || grant.Schema != "dittobench-coding-inference-exchange-v1" ||
		grant.CodingContractVersion != codingcontract.ContractVersion || grant.WeightEligible || grant.Status != "active" ||
		!canonicalUUID(grant.GrantID) || !canonicalUUID(grant.RunRowID) ||
		grant.TicketID != request.TicketID || grant.CaseID != authority.task.CaseID ||
		grant.ProfileCapabilityID != authority.task.ProfileCapabilityID || grant.InferenceGrantSHA256 != policySHA ||
		grant.InferenceGrantSHA256 != authority.manifest.InferenceGrantSHA256 || grant.Model != policy.Model ||
		grant.ProviderAPI != policy.ProviderAPI || grant.ProviderRoute != policy.ProviderRoute ||
		grant.ReceiptProvider != policy.ReceiptProvider || grant.ProviderRouteProfile != policy.ProviderRouteProfile ||
		grant.ProviderAccountGuardrail != policy.ProviderAccountGuardrail ||
		grant.ProviderPipelinePolicy != policy.ProviderPipelinePolicy || grant.ProviderCachePolicy != policy.ProviderCachePolicy ||
		grant.ReasoningEffort != policy.ReasoningEffort || grant.Generation == 0 || grant.Generation > 1<<31-1 ||
		grant.RequestBudget == 0 || grant.RequestBudget > requestBudget ||
		grant.PromptTokenBudget == 0 || grant.PromptTokenBudget > authority.lease.Budgets.ModelInputTokens ||
		grant.PromptTokenBudget > policy.MaxPromptTokens ||
		grant.CompletionTokenBudget == 0 || grant.CompletionTokenBudget > authority.lease.Budgets.ModelOutputTokens ||
		grant.CompletionTokenBudget > policy.MaxCompletionTokens ||
		grant.CostBudgetUSDMicros == 0 || grant.CostBudgetUSDMicros > policy.MaxCostUSDMicros ||
		!grant.ExpiresAt.After(now) || grant.ExpiresAt.After(request.Deadline) ||
		!validIdentifier(harnessInstanceID, 256) || !validIdentifier(attemptID, 256) ||
		!validBearer(grant.Bearer) || !validBrokerPair(brokerPublicKey, brokerPrivateKey) ||
		!validProxyURL(grant.ProxyURL) {
		return zero, fmt.Errorf("%w: inference grant authority", ErrInvalid)
	}
	binding := codingrelay.Binding{
		AttemptID: attemptID, AgentArtifactSHA256: authority.manifest.AgentArtifactSHA256,
		HarnessInstanceID: harnessInstanceID, TicketID: request.TicketID, CaseID: authority.task.CaseID,
		ProfileCapabilityID: authority.task.ProfileCapabilityID, GrantID: grant.GrantID,
		Generation: grant.Generation, InferenceGrantSHA256: grant.InferenceGrantSHA256,
		IssuedAt: now, Deadline: grant.ExpiresAt,
		RequestBudget: grant.RequestBudget, PromptTokenBudget: grant.PromptTokenBudget,
		CompletionTokenBudget: grant.CompletionTokenBudget, CostBudgetUSDMicros: grant.CostBudgetUSDMicros,
	}
	return codingplatform.GrantCapability{
		Binding: binding, Bearer: grant.Bearer, BrokerPublicKey: brokerPublicKey,
		BrokerPrivateKey: ed25519.PrivateKey(append([]byte(nil), brokerPrivateKey...)), ProxyURL: grant.ProxyURL,
	}, nil
}

func decodeCapabilities(
	values []json.RawMessage,
	ticketID string,
	deadline time.Time,
	phase codingartifacts.DeliveryPhase,
	kinds []codingartifacts.Kind,
) ([]codingartifacts.Capability, error) {
	if len(values) != len(kinds) {
		return nil, ErrInvalid
	}
	result := make([]codingartifacts.Capability, len(values))
	for index, raw := range values {
		wire, err := codingartifacts.DecodeWireCapability(raw)
		capability, convertErr := wire.ToCapability()
		if err != nil || convertErr != nil || wire.TicketID != ticketID || !wire.TicketDeadline.Equal(deadline) ||
			wire.DeliveryPhase != phase || wire.ArtifactKind != kinds[index] {
			return nil, ErrInvalid
		}
		result[index] = capability
	}
	return result, nil
}

func runtimePolicyMatchesPlan(policy codingcontract.RuntimePolicy, plan codingexecution.RunnerPlan) bool {
	paths := append([]string(nil), plan.EditablePaths...)
	paths = append(paths, plan.CreatablePaths...)
	paths = append(paths, plan.DeletablePaths...)
	slices.Sort(paths)
	tests := make([]string, len(plan.TestCommands))
	for index := range plan.TestCommands {
		tests[index] = plan.TestCommands[index].ID
	}
	builds := make([]string, len(plan.BuildCommands))
	for index := range plan.BuildCommands {
		builds[index] = plan.BuildCommands[index].ID
	}
	return slices.Equal(policy.EditablePaths, paths) && slices.Equal(policy.TestCommandIDs, tests) &&
		slices.Equal(policy.BuildCommandIDs, builds)
}

func parseRequiredObject(body []byte, destination any, required []string) error {
	trimmed := bytes.TrimSpace(body)
	if len(trimmed) < 2 || len(trimmed) > maximumLeaseBytes || trimmed[0] != '{' || trimmed[len(trimmed)-1] != '}' ||
		codingcontract.ValidateJSONDocument(trimmed, maximumLeaseBytes) != nil {
		return ErrInvalid
	}
	var shape map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &shape); err != nil {
		return ErrInvalid
	}
	for _, field := range required {
		if _, present := shape[field]; !present {
			return ErrInvalid
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	if err := decoder.Decode(destination); err != nil {
		return ErrInvalid
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ErrInvalid
	}
	return nil
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
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

func lowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && hex.EncodeToString(decoded) == value
}

func validBearer(value string) bool {
	if len(value) < 32 || len(value) > 128 {
		return false
	}
	for _, character := range value {
		if !(character >= 'a' && character <= 'z') && !(character >= 'A' && character <= 'Z') &&
			!(character >= '0' && character <= '9') && character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func validBrokerPair(public string, private []byte) bool {
	if len(private) != ed25519.PrivateKeySize {
		return false
	}
	decoded, err := base64.RawURLEncoding.DecodeString(public)
	if err != nil || len(decoded) != ed25519.PublicKeySize {
		return false
	}
	derived, ok := ed25519.PrivateKey(private).Public().(ed25519.PublicKey)
	return ok && bytes.Equal(decoded, derived)
}

func validProxyURL(value string) bool {
	parsed, err := url.ParseRequestURI(value)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil ||
		parsed.RawQuery != "" || parsed.Fragment != "" ||
		parsed.Path != "/api/v1/inference/coding/chat/completions" ||
		(parsed.Port() != "" && parsed.Port() != "443") {
		return false
	}
	return true
}

func validPhaseDeadline(deadline, now time.Time) bool {
	return !now.IsZero() && deadline.After(now) && !deadline.After(now.Add(2*time.Hour))
}
