package codingphase

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codingattempt"
	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecution"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingoutbox"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
	"github.com/ditto-assistant/dittobench-api/internal/codingsupervisor"
)

const (
	fixtureTicket  = "33333333-3333-4333-8333-333333333333"
	fixtureRun     = "coding-run-phase-001"
	fixtureCase    = "private-case-phase-001"
	fixtureProfile = "profile-phase-001"
	fixtureAgent   = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
	fixtureRunRow  = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	fixtureFreeze  = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
)

type phaseFixture struct {
	t          *testing.T
	now        time.Time
	deadline   time.Time
	root       string
	policy     codingcontract.InferencePolicy
	model      codingcontract.ModelEvidence
	manifest   codingcontract.RunManifest
	runnerPlan codingexecution.RunnerPlan
	graderPlan codingexecution.GraderPlan
	resource   codingexecution.ResourceProfile
	visible    []byte
	trace      []string
	workspace  *fakeWorkspacePublisher
	harness    *fakeHarness
	runtime    *fakeAttemptRuntime
	inference  *fakeInferenceActivator
	outbox     *codingoutbox.Store
	runner     *Runner
	request    codingsupervisor.Request
	input      codingsupervisor.AuthoringInput
}

type fakeAttemptRuntime struct {
	visible []byte
	trace   *[]string
}

func (runtime *fakeAttemptRuntime) BeginAuthoring(
	ctx context.Context,
	spec codingattempt.AuthoringSpec,
) (AuthoringSession, error) {
	*runtime.trace = append(*runtime.trace, "begin_authoring")
	session, err := codingrunner.NewSession(ctx, spec.RunnerManifest, bytes.NewReader(runtime.visible), nil)
	if err != nil {
		return nil, err
	}
	return &fakeAuthoringSession{session: session, trace: runtime.trace}, nil
}

func (runtime *fakeAttemptRuntime) Grade(
	_ context.Context,
	spec codingattempt.GradingSpec,
	_ codingrunner.FrozenSubmission,
) (codinggrader.Result, error) {
	*runtime.trace = append(*runtime.trace, "grade")
	groups := make([]codingcontract.TestGroupEvidence, len(spec.GraderManifest.TestGroups))
	for index, group := range spec.GraderManifest.TestGroups {
		groups[index] = codingcontract.TestGroupEvidence{
			Group: group.Group, Passed: group.ExpectedTotal, Total: group.ExpectedTotal,
		}
	}
	count := uint32(len(groups))
	if spec.GraderManifest.Build.Required {
		count++
	}
	evidence := &codingcontract.GraderEvidence{
		GraderContractSHA256:       spec.GraderManifest.GraderContractSHA256,
		GraderBundleSHA256:         spec.GraderManifest.GraderBundleSHA256,
		GraderImageDigest:          spec.GraderManifest.GraderImageDigest,
		GraderPlatform:             spec.GraderManifest.GraderPlatform,
		TestManifestSHA256:         spec.GraderManifest.TestManifestSHA256,
		GraderPlanSHA256:           spec.GraderManifest.GraderPlanSHA256,
		ResourceProfileSHA256:      spec.GraderManifest.ResourceProfileSHA256,
		ExecutionReceiptRootSHA256: strings.Repeat("9", 64), ExecutionReceiptCount: count,
		GraderIntegrityBeforeSHA256: strings.Repeat("8", 64),
		GraderIntegrityAfterSHA256:  strings.Repeat("8", 64),
		Build: codingcontract.BuildEvidence{
			CommandID: spec.GraderManifest.Build.Command.ID,
			Required:  spec.GraderManifest.Build.Required, Passed: true,
		},
		TestGroups: groups,
	}
	return codinggrader.Result{
		TerminalDomain:    codingcontract.DomainResolved,
		RepairScoreMicros: codingcontract.ResolvedRepairScoreMicros, Evidence: evidence,
	}, nil
}

type fakeAuthoringSession struct {
	session *codingrunner.Session
	trace   *[]string
}

func (session *fakeAuthoringSession) Handler() http.Handler         { return session.session.Handler() }
func (*fakeAuthoringSession) SeedProjection() codingseed.Projection { return codingseed.Projection{} }
func (session *fakeAuthoringSession) Freeze(
	ctx context.Context,
	revoker codingattempt.CapabilityRevoker,
) (codingrunner.FreezeResult, error) {
	err := revoker.Revoke(ctx)
	*session.trace = append(*session.trace, "freeze")
	return session.session.Freeze(), err
}
func (session *fakeAuthoringSession) WriteTranscript(destination io.Writer) (codingrunner.TranscriptIdentity, error) {
	return session.session.WriteTranscript(destination)
}
func (session *fakeAuthoringSession) Close() error {
	*session.trace = append(*session.trace, "workspace_destroy")
	return session.session.Close()
}

type fakeSeedDeliverer struct{ trace *[]string }

func (deliverer fakeSeedDeliverer) Deliver(
	context.Context,
	codingseed.SeedClient,
	codingseed.Projection,
) (codingseed.Delivery, error) {
	*deliverer.trace = append(*deliverer.trace, "seed")
	return codingseed.Delivery{}, nil
}

type fakeHarnessFactory struct{ harness *fakeHarness }

func (factory fakeHarnessFactory) Acquire(context.Context, HarnessBinding) (Harness, error) {
	*factory.harness.trace = append(*factory.harness.trace, "harness_acquire")
	return factory.harness, nil
}

type fakeHarness struct {
	trace         *[]string
	outbox        *codingoutbox.Store
	workspace     *fakeWorkspacePublisher
	runErr        error
	runStarted    chan struct{}
	waitForCancel bool
	unsupported   bool
	destroyed     bool
}

func (*fakeHarness) InstanceID() string                            { return "harness-phase-001" }
func (harness *fakeHarness) Client() codingcertifier.HarnessClient { return harness }
func (harness *fakeHarness) Activate(context.Context) error {
	*harness.trace = append(*harness.trace, "harness_activate")
	return nil
}
func (harness *fakeHarness) Destroy(context.Context) error {
	*harness.trace = append(*harness.trace, "harness_destroy")
	harness.destroyed = true
	return nil
}
func (harness *fakeHarness) Health(ctx context.Context) (codingcertifier.HealthResponse, error) {
	_, record, err := harness.outbox.Lookup(ctx, codingoutbox.PurposeShadowAttempt, fixtureTicket)
	if err != nil || record.State != codingoutbox.StateCollecting {
		return codingcertifier.HealthResponse{}, errors.New("health preceded durable collecting marker")
	}
	*harness.trace = append(*harness.trace, "health")
	capabilities := []string{"case_scoped_inference_v1", "coding_runner_tools_v1", "scoped_memory_seed_v1"}
	if harness.unsupported {
		capabilities = []string{"scoped_memory_seed_v1"}
	}
	return codingcertifier.HealthResponse{
		Status: "ok", SupportedCodingContractVersions: []int{codingcontract.ContractVersion},
		Capabilities: capabilities,
	}, nil
}
func (*fakeHarness) Seed(_ context.Context, request codingcontract.SeedRequest) (codingcertifier.SeedResponse, error) {
	return codingcertifier.SeedResponse{
		CaseID: request.CaseID, ProfileCapabilityID: request.ProfileCapabilityID,
		MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
	}, nil
}
func (harness *fakeHarness) Run(ctx context.Context, request codingcontract.RunRequest) (codingcertifier.RunResponse, error) {
	*harness.trace = append(*harness.trace, "run")
	if harness.runStarted != nil {
		close(harness.runStarted)
	}
	if harness.waitForCancel {
		<-ctx.Done()
		return codingcertifier.RunResponse{}, ctx.Err()
	}
	if harness.workspace.handler == nil {
		return codingcertifier.RunResponse{}, errors.New("workspace route was not published")
	}
	read := invokeTool(harness.workspace.handler, codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: request.CaseID,
		ProfileCapabilityID: request.ProfileCapabilityID, CallID: "phase-read",
		Name: "repo.read_file", Arguments: json.RawMessage(`{"path":"src/app.py"}`),
	})
	if !read.OK {
		return codingcertifier.RunResponse{}, errors.New("fixture read failed")
	}
	var readResult struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readResult); err != nil {
		return codingcertifier.RunResponse{}, err
	}
	arguments, _ := json.Marshal(map[string]any{
		"path": "src/app.py", "expected_sha256": readResult.SHA256,
		"replacements": []map[string]string{{
			"old_text": "return value.strip()", "new_text": "return value.rstrip()",
		}},
	})
	patched := invokeTool(harness.workspace.handler, codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion, CaseID: request.CaseID,
		ProfileCapabilityID: request.ProfileCapabilityID, CallID: "phase-edit",
		Name: "repo.apply_patch", Arguments: arguments,
	})
	if !patched.OK {
		return codingcertifier.RunResponse{}, errors.New("fixture patch failed")
	}
	if harness.runErr != nil {
		return codingcertifier.RunResponse{}, harness.runErr
	}
	response := codingcertifier.RunResponse{CaseID: request.CaseID}
	response.FinalReport.Summary = "Applied the bounded parser fix."
	response.FinalReport.RemainingRisks = []string{}
	return response, nil
}

type fakeWorkspacePublisher struct {
	trace          *[]string
	handler        http.Handler
	handle         *fakeWorkspaceCapability
	failRevokeOnce bool
}

func (publisher *fakeWorkspacePublisher) Publish(
	_ context.Context,
	_ codingcertifier.CapabilityBinding,
	handler http.Handler,
) (codingcertifier.PublishedCapability, error) {
	*publisher.trace = append(*publisher.trace, "workspace_publish")
	publisher.handler = handler
	publisher.handle = &fakeWorkspaceCapability{
		trace: publisher.trace, failRevokeOnce: publisher.failRevokeOnce,
	}
	return publisher.handle, nil
}

type fakeWorkspaceCapability struct {
	trace          *[]string
	revoked        bool
	closed         bool
	failRevokeOnce bool
	revokeAttempts int
}

func (*fakeWorkspaceCapability) URL() string { return "http://workspace.invalid/capability" }
func (capability *fakeWorkspaceCapability) Revoke(context.Context) error {
	*capability.trace = append(*capability.trace, "workspace_revoke")
	capability.revokeAttempts++
	if capability.failRevokeOnce && capability.revokeAttempts == 1 {
		return errors.New("ambiguous workspace revocation")
	}
	capability.revoked = true
	return nil
}
func (capability *fakeWorkspaceCapability) Close() error {
	*capability.trace = append(*capability.trace, "workspace_close")
	capability.closed = true
	return nil
}

type fakeInferenceActivator struct {
	trace    *[]string
	evidence codingcontract.ModelEvidence
	gateway  *fakeInferenceGateway
}

func (activator *fakeInferenceActivator) Activate(
	ctx context.Context,
	activation InferenceActivation,
) (InferenceGateway, error) {
	*activator.trace = append(*activator.trace, "inference_activate")
	binding := activation.Capability.Binding
	if activation.Revocation.GrantID != binding.GrantID ||
		activation.Revocation.TicketID != binding.TicketID ||
		activation.Revocation.Generation != binding.Generation ||
		activation.Revocation.Bearer == "" || activation.Revocation.URL == "" {
		return nil, errors.New("inference revocation authority mismatch")
	}
	if err := activation.Authorizer.Authorize(ctx, codinggateway.CapabilityBinding{
		AttemptID: binding.AttemptID, AgentArtifactSHA256: binding.AgentArtifactSHA256,
		HarnessInstanceID: binding.HarnessInstanceID, TicketID: binding.TicketID, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, GrantID: binding.GrantID,
		Generation: binding.Generation, InferenceGrantSHA256: binding.InferenceGrantSHA256,
		IssuedAt: binding.IssuedAt, Deadline: binding.Deadline, RequestBudget: binding.RequestBudget,
		PromptTokenBudget: binding.PromptTokenBudget, CompletionTokenBudget: binding.CompletionTokenBudget,
	}); err != nil {
		return nil, err
	}
	activator.gateway = &fakeInferenceGateway{
		trace: activator.trace, binding: binding, evidence: activator.evidence,
	}
	return activator.gateway, nil
}

type fakeInferenceGateway struct {
	trace    *[]string
	binding  codingrelay.Binding
	evidence codingcontract.ModelEvidence
	revoked  bool
	closed   bool
}

func (*fakeInferenceGateway) URL() (string, error) { return "http://inference.invalid/capability", nil }
func (gateway *fakeInferenceGateway) Revoke(context.Context) error {
	*gateway.trace = append(*gateway.trace, "inference_revoke")
	gateway.revoked = true
	return nil
}
func (gateway *fakeInferenceGateway) Evidence(_ context.Context, binding codingrelay.EvidenceBinding) (codingcontract.ModelEvidence, error) {
	*gateway.trace = append(*gateway.trace, "inference_evidence")
	if !gateway.revoked || binding.AttemptID != gateway.binding.AttemptID ||
		binding.HarnessInstanceID != gateway.binding.HarnessInstanceID ||
		binding.InferenceGrantSHA256 != gateway.binding.InferenceGrantSHA256 {
		return codingcontract.ModelEvidence{}, errors.New("inference evidence binding mismatch")
	}
	return gateway.evidence, nil
}
func (gateway *fakeInferenceGateway) Close() error {
	*gateway.trace = append(*gateway.trace, "inference_close")
	if !gateway.revoked {
		return errors.New("inference closed before revoke")
	}
	gateway.closed = true
	return nil
}

func newPhaseFixture(t *testing.T) *phaseFixture {
	t.Helper()
	fixture := &phaseFixture{t: t, now: time.Now().UTC().Truncate(time.Second)}
	fixture.deadline = fixture.now.Add(time.Hour)
	fixture.policy, fixture.model = loadInferenceFixture(t)
	fixture.visible = tarFixture(t, map[string]string{
		"src/app.py": "def normalize(value):\n    return value.strip()\n",
	})
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(fixture.visible), codingrunner.DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	limits := executionLimits(codingrunner.DefaultLimits())
	fixture.runnerPlan = codingexecution.RunnerPlan{
		Schema: codingexecution.RunnerPlanSchema, CodingContractVersion: codingcontract.ContractVersion,
		CaseID: fixtureCase, VisibleBundleSHA256: identity.VisibleBundleSHA256,
		BaseTreeSHA256: identity.TreeSHA256, EditablePaths: []string{"src/app.py"},
		CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingexecution.Command{}, BuildCommands: []codingexecution.Command{}, Limits: limits,
	}
	fixture.resource = codingexecution.ResourceProfile{
		Schema: codingexecution.ResourceSchema, CandidateLimits: limits, ProtectedLimits: limits,
		MaxCombinedDiskBytes: 4 << 30, MemoryLimitBytes: 4 << 30, ScratchLimitBytes: 1 << 30,
		PidsLimit: 512, CPUQuotaMillis: 2_000,
	}
	resourceSHA, err := codingexecution.ResourceProfileSHA256(fixture.resource)
	if err != nil {
		t.Fatal(err)
	}
	groups := []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}
	tests := make([]codingexecution.TestGroup, len(groups))
	for index, group := range groups {
		tests[index] = codingexecution.TestGroup{
			Group: group, ExpectedTotal: 1,
			Command: codingexecution.Command{ID: "test-" + group, Argv: []string{"python", "-m", "pytest"}, TimeoutMilliseconds: 60_000},
		}
	}
	fixture.graderPlan = codingexecution.GraderPlan{
		Schema: codingexecution.GraderPlanSchema, CodingContractVersion: codingcontract.ContractVersion,
		CaseID: fixtureCase, VariantID: "variant-v1", VisibleBundleSHA256: identity.VisibleBundleSHA256,
		BaseTreeSHA256: identity.TreeSHA256, GraderContractSHA256: codinggrader.GraderContractSHA256(),
		GraderBundleSHA256: strings.Repeat("5", 64), GraderImageDigest: "sha256:" + strings.Repeat("6", 64),
		GraderPlatform: "linux/amd64", TestManifestSHA256: strings.Repeat("7", 64),
		ResourceProfileSHA256: resourceSHA, ExecutionTimeoutMilliseconds: 30 * 60 * 1_000,
		BuildRequired: true,
		BuildCommand:  codingexecution.Command{ID: "grader-build", Argv: []string{"python", "-m", "compileall", "src"}, TimeoutMilliseconds: 60_000},
		TestGroups:    tests, ExecutionOrder: []string{"fail_to_pass", "pass_to_pass", "hidden", "adversarial", "integrity"},
	}
	graderPlanSHA, err := codingexecution.GraderPlanSHA256(fixture.graderPlan)
	if err != nil {
		t.Fatal(err)
	}
	policySHA, err := codingcontract.InferencePolicySHA256(fixture.policy)
	if err != nil {
		t.Fatal(err)
	}
	fixture.manifest = codingcontract.RunManifest{
		Schema: "dittobench-coding-run-manifest-v1", CodingContractVersion: codingcontract.ContractVersion,
		BenchFamily: "coding", WeightEligible: false, CodingRunID: fixtureRun, AgentID: fixtureAgent,
		AgentArtifactSHA256: strings.Repeat("a", 64), CorpusReleaseID: "private-corpus-phase-v1",
		CatalogMerkleRoot: strings.Repeat("b", 64), SelectionDerivationID: "coding-selection-phase-v1",
		SelectionChainGenesisHash: "0x" + strings.Repeat("0", 64), SelectionBlockNumber: 1,
		SelectionBlockHash: "0x" + strings.Repeat("c", 64), InferenceGrantSHA256: policySHA,
		GraderContractSHA256: codinggrader.GraderContractSHA256(), TaskSetID: "task-set-phase-001",
		TaskSetManifestSHA256: strings.Repeat("d", 64),
		Tasks: []codingcontract.ManifestTask{{
			CaseID: fixtureCase, VariantID: "variant-v1", ProfileCapabilityID: fixtureProfile,
			VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
			MemoryBundleSHA256:     strings.Repeat("4", 64),
			EnvironmentImageDigest: "sha256:" + strings.Repeat("1", 64), EnvironmentPlatform: "linux/amd64",
			ResourceProfileSHA256: resourceSHA, GraderBundleSHA256: fixture.graderPlan.GraderBundleSHA256,
			GraderImageDigest: fixture.graderPlan.GraderImageDigest, GraderPlatform: "linux/amd64",
			TestManifestSHA256: fixture.graderPlan.TestManifestSHA256, GraderPlanSHA256: graderPlanSHA,
		}},
	}
	if err := fixture.manifest.Validate(); err != nil {
		t.Fatal(err)
	}
	fixture.root = filepath.Join(t.TempDir(), "outbox")
	if err := os.Mkdir(fixture.root, 0o700); err != nil {
		t.Fatal(err)
	}
	fixture.outbox = openFixtureOutbox(t, fixture.root, fixture.now)
	fixture.workspace = &fakeWorkspacePublisher{trace: &fixture.trace}
	fixture.harness = &fakeHarness{trace: &fixture.trace, outbox: fixture.outbox, workspace: fixture.workspace}
	fixture.runtime = &fakeAttemptRuntime{visible: fixture.visible, trace: &fixture.trace}
	fixture.inference = &fakeInferenceActivator{trace: &fixture.trace, evidence: fixture.model}
	fixture.runner = newFixtureRunner(t, fixture)
	fixture.request = fixture.authorRequest(t)
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	fixture.input = codingsupervisor.AuthoringInput{
		Request: fixture.request, SessionID: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
		BrokerPublicKey: base64.RawURLEncoding.EncodeToString(public), BrokerPrivateKey: private,
	}
	t.Cleanup(func() { _ = fixture.outbox.Close() })
	return fixture
}

func (fixture *phaseFixture) authorRequest(t *testing.T) codingsupervisor.Request {
	t.Helper()
	manifestBody := mustJSON(t, fixture.manifest)
	manifestSHA, _ := codingcontract.Digest(fixture.manifest)
	runnerPlanSHA, _ := codingexecution.RunnerPlanSHA256(fixture.runnerPlan)
	issue := codingcontract.Issue{Title: "Fix normalization", Description: "Preserve trailing semantics.", Constraints: []string{"Keep the patch minimal."}}
	policy := codingcontract.RuntimePolicy{EditablePaths: []string{"src/app.py"}, TestCommandIDs: []string{}, BuildCommandIDs: []string{}}
	budgets := codingcontract.Budgets{ModelInputTokens: 200_000, ModelOutputTokens: 30_000, WorkspaceToolCalls: 32, WallTimeSeconds: 1_800}
	issueSHA, _ := codingcontract.IssueDigest(issue)
	policySHA, _ := codingcontract.RuntimePolicyDigest(policy)
	budgetsSHA, _ := codingcontract.BudgetsDigest(budgets)
	capabilities := []json.RawMessage{
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseAuthoring, codingartifacts.KindVisibleBundle, fixture.manifest.Tasks[0].VisibleBundleSHA256),
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseAuthoring, codingartifacts.KindMemoryBundle, fixture.manifest.Tasks[0].MemoryBundleSHA256),
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile, fixture.manifest.Tasks[0].ResourceProfileSHA256),
	}
	lease := map[string]any{
		"schema": "dittobench-coding-authoring-lease-v1", "coding_contract_version": 1, "weight_eligible": false,
		"ticket_id": fixtureTicket, "ticket_deadline": fixture.deadline, "coding_run_id": fixtureRun,
		"run_manifest_sha256": manifestSHA, "task_set_manifest_sha256": fixture.manifest.TaskSetManifestSHA256,
		"repository_epoch": "repository-epoch-phase-001", "issue_sha256": issueSHA,
		"runtime_policy_sha256": policySHA, "budgets_sha256": budgetsSHA,
		"issue": issue, "runtime_policy": policy, "budgets": budgets,
		"runner_plan_sha256": runnerPlanSHA, "runner_plan": fixture.runnerPlan,
		"run_manifest": json.RawMessage(manifestBody), "capabilities": capabilities,
	}
	grant := map[string]any{
		"schema": "dittobench-coding-inference-exchange-v1", "coding_contract_version": 1,
		"weight_eligible": false, "status": "active", "grant_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
		"ticket_id": fixtureTicket, "run_row_id": fixtureRunRow, "case_id": fixtureCase,
		"profile_capability_id": fixtureProfile, "inference_grant_sha256": fixture.manifest.InferenceGrantSHA256,
		"model": fixture.policy.Model, "provider_api": fixture.policy.ProviderAPI,
		"provider_route": fixture.policy.ProviderRoute, "receipt_provider": fixture.policy.ReceiptProvider,
		"provider_route_profile":     fixture.policy.ProviderRouteProfile,
		"provider_account_guardrail": fixture.policy.ProviderAccountGuardrail,
		"provider_pipeline_policy":   fixture.policy.ProviderPipelinePolicy,
		"provider_cache_policy":      fixture.policy.ProviderCachePolicy, "reasoning_effort": fixture.policy.ReasoningEffort,
		"request_budget": 48, "prompt_token_budget": 200_000, "completion_token_budget": 30_000,
		"cost_budget_usd_micros": fixture.policy.MaxCostUSDMicros,
		"expires_at":             fixture.now.Add(10 * time.Minute), "generation": 1,
		"bearer":        "synthetic-coding-bearer-0000000000000000",
		"proxy_url":     "https://relay.invalid/api/v1/inference/coding/chat/completions",
		"revoke_bearer": "synthetic-revoke-bearer-0000000000000000",
		"revoke_url":    "https://platform.invalid/api/v1/validator/coding-shadow/inference-revoke-capability",
	}
	harness := map[string]any{
		"schema": "dittobench-coding-harness-launch-v1", "coding_contract_version": 1,
		"weight_eligible": false, "agent_id": fixtureAgent, "run_row_id": fixtureRunRow,
		"ticket_id": fixtureTicket, "ticket_deadline": fixture.deadline, "bench_version": 12,
		"agent_artifact_sha256": fixture.manifest.AgentArtifactSHA256,
		"screened_image_sha256": strings.Repeat("2", 64), "screened_image_size_bytes": 1024,
		"screened_image_id":  "sha256:" + strings.Repeat("3", 64),
		"screened_image_ref": "ditto-screened/phase:latest", "screening_policy_version": 9,
		"image_url":  "https://storage.invalid/screened-image.tar?X-Amz-Signature=synthetic",
		"expires_at": fixture.now.Add(5 * time.Minute),
	}
	return codingsupervisor.Request{
		Schema: codingsupervisor.RequestSchema, Operation: codingsupervisor.OperationAuthor,
		OperationID: "ffffffff-ffff-4fff-8fff-ffffffffffff", TicketID: fixtureTicket,
		CodingRunID: fixtureRun, Deadline: fixture.deadline, Lease: mustJSON(t, lease),
		Grant: mustJSON(t, grant), Harness: mustJSON(t, harness),
	}
}

func (fixture *phaseFixture) gradeRequest(
	t *testing.T,
	authoring codingsupervisor.AuthoringOutcome,
) codingsupervisor.Request {
	t.Helper()
	manifestBody := mustJSON(t, fixture.manifest)
	manifestSHA, _ := codingcontract.Digest(fixture.manifest)
	var evidence codingcontract.AuthoringEvidence
	if err := json.Unmarshal(authoring.Evidence, &evidence); err != nil {
		t.Fatal(err)
	}
	evidenceSHA, _ := codingcontract.AuthoringEvidenceDigest(evidence)
	capabilities := []json.RawMessage{
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseGrading, codingartifacts.KindVisibleBundle, fixture.manifest.Tasks[0].VisibleBundleSHA256),
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseGrading, codingartifacts.KindResourceProfile, fixture.manifest.Tasks[0].ResourceProfileSHA256),
		fixtureCapability(t, fixture.now, fixture.deadline, codingartifacts.PhaseGrading, codingartifacts.KindGraderBundle, fixture.manifest.Tasks[0].GraderBundleSHA256),
	}
	lease := map[string]any{
		"schema": "dittobench-coding-grading-lease-v1", "coding_contract_version": 1, "weight_eligible": false,
		"agent_id": fixtureAgent, "run_row_id": fixtureRunRow, "ticket_id": fixtureTicket,
		"ticket_deadline": fixture.deadline, "coding_run_id": fixtureRun,
		"run_manifest_sha256": manifestSHA, "task_set_manifest_sha256": fixture.manifest.TaskSetManifestSHA256,
		"freeze_id": fixtureFreeze, "authoring_evidence_sha256": evidenceSHA,
		"frozen_patch_sha256":          evidence.FrozenPatchSHA256,
		"frozen_submission_object_key": "sha256/" + evidence.FrozenPatchSHA256,
		"run_manifest":                 json.RawMessage(manifestBody), "grader_plan": fixture.graderPlan,
		"grader_resource_profile": fixture.resource, "capabilities": capabilities,
	}
	return codingsupervisor.Request{
		Schema: codingsupervisor.RequestSchema, Operation: codingsupervisor.OperationGrade,
		OperationID: "12121212-1212-4212-8212-121212121212", TicketID: fixtureTicket,
		CodingRunID: fixtureRun, Deadline: fixture.deadline, Lease: mustJSON(t, lease),
		Authoring: mustJSON(t, authoring),
	}
}

func TestRunnerAuthorsThenGradesAcrossOutboxRestart(t *testing.T) {
	fixture := newPhaseFixture(t)
	authoring, err := fixture.runner.Author(t.Context(), fixture.input)
	if err != nil {
		t.Fatal(err)
	}
	if !authoring.CapabilitiesRevoked || !authoring.AuthoringEnvironmentDestroyed || !fixture.harness.destroyed {
		t.Fatalf("authoring lifecycle=%#v destroyed=%v", authoring, fixture.harness.destroyed)
	}
	wantOrder := []string{
		"harness_acquire", "begin_authoring", "harness_activate", "health", "seed", "inference_activate",
		"workspace_publish", "run", "workspace_revoke", "inference_revoke", "freeze",
		"inference_evidence", "workspace_close", "inference_close", "workspace_destroy", "harness_destroy",
	}
	if !slices.Equal(fixture.trace, wantOrder) {
		t.Fatalf("trace=%v want=%v", fixture.trace, wantOrder)
	}
	_, record, err := fixture.outbox.Lookup(t.Context(), codingoutbox.PurposeShadowAttempt, fixtureTicket)
	if err != nil || record.State != codingoutbox.StateReady || record.Transcript == nil || record.Frozen == nil {
		t.Fatalf("record=%#v err=%v", record, err)
	}
	if err := fixture.outbox.Close(); err != nil {
		t.Fatal(err)
	}
	fixture.outbox = openFixtureOutbox(t, fixture.root, fixture.now)
	fixture.runner.outbox = fixture.outbox
	grading, err := fixture.runner.Grade(t.Context(), fixture.gradeRequest(t, authoring))
	if err != nil {
		t.Fatal(err)
	}
	if !grading.GradingEnvironmentDestroyed || len(grading.TaskEvidence) != 1 {
		t.Fatalf("grading=%#v", grading)
	}
	var evidence codingcontract.TaskEvidence
	if err := json.Unmarshal(grading.TaskEvidence[0], &evidence); err != nil ||
		evidence.ValidateAgainst(fixture.manifest, fixtureTicket) != nil ||
		evidence.TerminalDomain != codingcontract.DomainResolved {
		t.Fatalf("task evidence=%#v err=%v", evidence, err)
	}
	recovery, err := fixture.runner.Recover(t.Context(), codingsupervisor.Request{
		TicketID: fixtureTicket, Deadline: fixture.deadline,
	})
	if err != nil || recovery.State != "ambiguous" {
		t.Fatalf("recovery=%#v err=%v", recovery, err)
	}
}

func TestRunnerFailureStillRevokesFreezesAndForbidsCleanRetry(t *testing.T) {
	fixture := newPhaseFixture(t)
	fixture.harness.runErr = errors.New("candidate run failed")
	if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
		t.Fatal("candidate run failure was accepted")
	}
	_, record, err := fixture.outbox.Lookup(t.Context(), codingoutbox.PurposeShadowAttempt, fixtureTicket)
	if err != nil || record.State != codingoutbox.StateReady || record.Transcript == nil || record.Frozen == nil {
		t.Fatalf("record=%#v err=%v", record, err)
	}
	if !fixture.workspace.handle.revoked || !fixture.workspace.handle.closed ||
		!fixture.inference.gateway.revoked || !fixture.inference.gateway.closed || !fixture.harness.destroyed {
		t.Fatal("authoring failure did not clean every capability and environment")
	}
	if _, err := fixture.outbox.Reserve(t.Context(), record.Binding, fixture.runnerPlanManifest(t).Limits); err != nil {
		t.Fatalf("exact evidence replay must resolve: %v", err)
	}
	attempt, _, _ := fixture.outbox.Lookup(t.Context(), codingoutbox.PurposeShadowAttempt, fixtureTicket)
	if _, err := attempt.BeginTranscript(t.Context()); !errors.Is(err, codingoutbox.ErrState) {
		t.Fatalf("clean retry was not blocked: %v", err)
	}
}

func TestRunnerRetriesAmbiguousRevocationBeforeReturningEvidence(t *testing.T) {
	fixture := newPhaseFixture(t)
	fixture.workspace.failRevokeOnce = true
	if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
		t.Fatal("freeze failure was cleared by retry revocation")
	}
	if fixture.workspace.handle.revokeAttempts != 2 ||
		!fixture.workspace.handle.revoked || !fixture.inference.gateway.revoked {
		t.Fatalf("workspace=%#v gateway=%#v", fixture.workspace.handle, fixture.inference.gateway)
	}
}

func TestUnsupportedHarnessStopsBeforeCapabilityPublication(t *testing.T) {
	fixture := newPhaseFixture(t)
	fixture.harness.unsupported = true
	if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
		t.Fatal("unsupported harness was accepted")
	}
	if slices.Contains(fixture.trace, "inference_activate") || slices.Contains(fixture.trace, "workspace_publish") ||
		slices.Contains(fixture.trace, "run") {
		t.Fatalf("unsupported harness received capabilities: %v", fixture.trace)
	}
	if !fixture.harness.destroyed {
		t.Fatal("unsupported harness was not destroyed")
	}
}

func TestCancellationDuringRunStillCompletesAuthoritativeCleanup(t *testing.T) {
	fixture := newPhaseFixture(t)
	fixture.harness.runStarted = make(chan struct{})
	fixture.harness.waitForCancel = true
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() {
		_, err := fixture.runner.Author(ctx, fixture.input)
		done <- err
	}()
	<-fixture.harness.runStarted
	cancel()
	if err := <-done; err == nil {
		t.Fatal("cancelled run was accepted")
	}
	if !fixture.workspace.handle.revoked || !fixture.workspace.handle.closed ||
		!fixture.inference.gateway.revoked || !fixture.inference.gateway.closed || !fixture.harness.destroyed {
		t.Fatal("cancellation skipped authoritative cleanup")
	}
	_, record, err := fixture.outbox.Lookup(t.Context(), codingoutbox.PurposeShadowAttempt, fixtureTicket)
	if err != nil || record.State != codingoutbox.StateReady || record.Transcript == nil || record.Frozen == nil {
		t.Fatalf("cancelled record=%#v err=%v", record, err)
	}
}

func TestGrantAuthorityDriftFailsBeforeInferenceActivation(t *testing.T) {
	for name, mutate := range map[string]func(map[string]any){
		"provider route": func(grant map[string]any) { grant["provider_route"] = "other/provider" },
		"request budget": func(grant map[string]any) { grant["request_budget"] = 49 },
		"cost budget":    func(grant map[string]any) { grant["cost_budget_usd_micros"] = 10_000_001 },
		"proxy path":     func(grant map[string]any) { grant["proxy_url"] = "https://relay.invalid/wrong" },
		"revoke path": func(grant map[string]any) {
			grant["revoke_url"] = "https://platform.invalid/wrong"
		},
		"revoke bearer": func(grant map[string]any) { grant["revoke_bearer"] = "short" },
		"shared bearer": func(grant map[string]any) { grant["revoke_bearer"] = grant["bearer"] },
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newPhaseFixture(t)
			var grant map[string]any
			if err := json.Unmarshal(fixture.request.Grant, &grant); err != nil {
				t.Fatal(err)
			}
			mutate(grant)
			fixture.input.Request.Grant = mustJSON(t, grant)
			if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
				t.Fatal("drifted grant was accepted")
			}
			if fixture.inference.gateway != nil || slices.Contains(fixture.trace, "inference_activate") {
				t.Fatalf("inference activated for invalid grant: %v", fixture.trace)
			}
			_, record, err := fixture.outbox.Lookup(t.Context(), codingoutbox.PurposeShadowAttempt, fixtureTicket)
			if err != nil || record.State != codingoutbox.StateReady {
				t.Fatalf("invalid-grant terminal record=%#v err=%v", record, err)
			}
		})
	}
}

func TestHarnessLaunchDriftFailsBeforeDormantAcquire(t *testing.T) {
	for name, mutate := range map[string]func(map[string]any){
		"agent": func(harness map[string]any) {
			harness["agent_id"] = "99999999-9999-4999-8999-999999999999"
		},
		"artifact": func(harness map[string]any) {
			harness["agent_artifact_sha256"] = strings.Repeat("f", 64)
		},
		"screened digest": func(harness map[string]any) {
			harness["screened_image_sha256"] = "bad"
		},
		"image URL": func(harness map[string]any) {
			harness["image_url"] = "http://storage.invalid/image?signature=x"
		},
		"expiry": func(harness map[string]any) {
			harness["expires_at"] = time.Now().UTC().Add(3 * time.Hour)
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newPhaseFixture(t)
			var harness map[string]any
			if err := json.Unmarshal(fixture.input.Request.Harness, &harness); err != nil {
				t.Fatal(err)
			}
			mutate(harness)
			fixture.input.Request.Harness = mustJSON(t, harness)
			if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
				t.Fatal("drifted harness authority was accepted")
			}
			if len(fixture.trace) != 0 {
				t.Fatalf("dormant harness acquired before authority rejection: %v", fixture.trace)
			}
		})
	}
}

func TestRunManifestMustMatchCompiledPolicyAndGraderBeforeHarnessAcquire(t *testing.T) {
	for name, mutate := range map[string]func(*codingcontract.RunManifest){
		"inference policy": func(manifest *codingcontract.RunManifest) {
			manifest.InferenceGrantSHA256 = strings.Repeat("e", 64)
		},
		"grader contract": func(manifest *codingcontract.RunManifest) {
			manifest.GraderContractSHA256 = strings.Repeat("f", 64)
		},
		"agent identity": func(manifest *codingcontract.RunManifest) {
			manifest.AgentID = "noncanonical-agent"
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newPhaseFixture(t)
			var lease map[string]json.RawMessage
			if err := json.Unmarshal(fixture.request.Lease, &lease); err != nil {
				t.Fatal(err)
			}
			var manifest codingcontract.RunManifest
			if err := json.Unmarshal(lease["run_manifest"], &manifest); err != nil {
				t.Fatal(err)
			}
			mutate(&manifest)
			manifestSHA, err := codingcontract.Digest(manifest)
			if err != nil {
				t.Fatal(err)
			}
			lease["run_manifest"] = mustJSON(t, manifest)
			lease["run_manifest_sha256"] = mustJSON(t, manifestSHA)
			fixture.input.Request.Lease = mustJSON(t, lease)
			if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
				t.Fatal("drifted manifest was accepted")
			}
			if len(fixture.trace) != 0 {
				t.Fatalf("harness acquired before manifest rejection: %v", fixture.trace)
			}
		})
	}
}

func TestGatewayEvidenceCannotExceedEffectiveGrantBudget(t *testing.T) {
	fixture := newPhaseFixture(t)
	fixture.inference.evidence = loadInferenceModelEvidence(t, "complete")
	fixture.inference.evidence.Requests = 49
	if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
		t.Fatal("over-budget gateway evidence was accepted")
	}
	if !fixture.workspace.handle.revoked || !fixture.inference.gateway.revoked || !fixture.harness.destroyed {
		t.Fatal("over-budget evidence skipped terminal cleanup")
	}
}

func TestBrokerKeyMismatchFailsBeforeInferenceActivation(t *testing.T) {
	fixture := newPhaseFixture(t)
	_, different, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	fixture.input.BrokerPrivateKey = different
	if _, err := fixture.runner.Author(t.Context(), fixture.input); err == nil {
		t.Fatal("mismatched broker key was accepted")
	}
	if slices.Contains(fixture.trace, "inference_activate") {
		t.Fatalf("inference activated with mismatched key: %v", fixture.trace)
	}
}

func TestAuthoringLeaseContainsNoProtectedGraderPlan(t *testing.T) {
	fixture := newPhaseFixture(t)
	var lease map[string]json.RawMessage
	if err := json.Unmarshal(fixture.request.Lease, &lease); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"grader_plan", "grader_resource_profile", "grader_bundle"} {
		if _, present := lease[forbidden]; present {
			t.Fatalf("authoring lease exposed top-level %q", forbidden)
		}
	}
	if bytes.Contains(lease["runner_plan"], []byte("protected_limits")) {
		t.Fatal("authoring runner plan exposed protected limits")
	}
}

func TestRuntimeAdapterAndConfigFailClosed(t *testing.T) {
	if _, err := NewRuntimeAdapter(nil); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("nil runtime adapter err=%v", err)
	}
	fixture := newPhaseFixture(t)
	config := Config{
		Attempts: RuntimeAdapter{}, Outbox: fixture.outbox,
		Seeds:     fakeSeedDeliverer{trace: &fixture.trace},
		Harnesses: fakeHarnessFactory{harness: fixture.harness}, WorkspaceRoutes: fixture.workspace,
		Inference: fixture.inference, InferencePolicy: fixture.policy,
	}
	if _, err := New(config); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("zero runtime adapter err=%v", err)
	}
	for name, value := range map[string]any{
		"config": Config{}, "activation": InferenceActivation{}, "runner": fixture.runner,
		"harness": HarnessBinding{ImageURL: "https://private.invalid/image?secret=value"},
	} {
		if _, err := json.Marshal(value); err == nil {
			t.Fatalf("%s serialized private state", name)
		}
	}
}

func (fixture *phaseFixture) runnerPlanManifest(t *testing.T) codingrunner.Manifest {
	t.Helper()
	manifest, err := fixture.runnerPlan.Manifest(codingexecution.RunnerBinding{
		TicketID: fixtureTicket, ProfileCapabilityID: fixtureProfile, Deadline: fixture.deadline,
	}, fixture.now)
	if err != nil {
		t.Fatal(err)
	}
	return manifest
}

func newFixtureRunner(t *testing.T, fixture *phaseFixture) *Runner {
	t.Helper()
	runner, err := New(Config{
		Attempts: fixture.runtime, Outbox: fixture.outbox, Seeds: fakeSeedDeliverer{trace: &fixture.trace},
		Harnesses: fakeHarnessFactory{harness: fixture.harness}, WorkspaceRoutes: fixture.workspace,
		Inference: fixture.inference, InferencePolicy: fixture.policy,
		Now: func() time.Time { return fixture.now }, CleanupTimeout: time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	return runner
}

func openFixtureOutbox(t *testing.T, root string, now time.Time) *codingoutbox.Store {
	t.Helper()
	store, err := codingoutbox.Open(codingoutbox.Config{
		Root: root, MaxTotalBytes: 1 << 30, MaxAttempts: 16,
		FinalizationGrace: time.Minute, OrphanGrace: time.Minute,
		ReleasedRetention: time.Hour, ExpiredRetention: time.Hour,
		Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func loadInferenceFixture(t *testing.T) (codingcontract.InferencePolicy, codingcontract.ModelEvidence) {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", "coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		Policy        json.RawMessage            `json:"policy"`
		ModelEvidence map[string]json.RawMessage `json:"model_evidence"`
	}
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	policy, err := codingcontract.ParseInferencePolicy(vector.Policy)
	if err != nil {
		t.Fatal(err)
	}
	var evidence codingcontract.ModelEvidence
	if err := json.Unmarshal(vector.ModelEvidence["not_invoked"], &evidence); err != nil || evidence.Validate() != nil {
		t.Fatalf("model evidence: %v", err)
	}
	return policy, evidence
}

func loadInferenceModelEvidence(t *testing.T, name string) codingcontract.ModelEvidence {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", "coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector struct {
		ModelEvidence map[string]json.RawMessage `json:"model_evidence"`
	}
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	var evidence codingcontract.ModelEvidence
	if err := json.Unmarshal(vector.ModelEvidence[name], &evidence); err != nil || evidence.Validate() != nil {
		t.Fatalf("model evidence %q: %v", name, err)
	}
	return evidence
}

func fixtureCapability(
	t *testing.T,
	now time.Time,
	deadline time.Time,
	phase codingartifacts.DeliveryPhase,
	kind codingartifacts.Kind,
	digest string,
) json.RawMessage {
	t.Helper()
	audiences := map[codingartifacts.Kind]codingartifacts.Audience{
		codingartifacts.KindVisibleBundle:   codingartifacts.AudienceWorkspaceMaterializer,
		codingartifacts.KindMemoryBundle:    codingartifacts.AudienceMemorySeedProjector,
		codingartifacts.KindResourceProfile: codingartifacts.AudienceResourceSupervisor,
		codingartifacts.KindGraderBundle:    codingartifacts.AudienceProtectedGrader,
	}
	expires := now.Add(5 * time.Minute)
	value := codingartifacts.WireCapability{
		Schema: "dittobench-coding-artifact-capability-v1", CodingContractVersion: 1,
		WeightEligible: false, TicketID: fixtureTicket, TicketDeadline: deadline,
		DeliveryPhase: phase, ArtifactKind: kind, Audience: audiences[kind], SHA256: digest,
		SizeBytes: 1024, URL: fmt.Sprintf(
			"https://storage.invalid/private/coding-artifacts/v1/%s/sha256/%s?X-Amz-Date=%s&X-Amz-Expires=300&X-Amz-Signature=synthetic",
			kind, digest, now.Format("20060102T150405Z"),
		), ExpiresAt: expires,
	}
	return mustJSON(t, value)
}

func executionLimits(value codingrunner.Limits) codingexecution.Limits {
	return codingexecution.Limits{
		MaxBundleBytes: value.MaxBundleBytes, MaxWorkspaceBytes: value.MaxWorkspaceBytes,
		MaxFileBytes: value.MaxFileBytes, MaxPatchBytes: value.MaxPatchBytes,
		MaxEntries: value.MaxEntries, MaxToolCalls: value.MaxToolCalls,
		MaxReadBytes: value.MaxReadBytes, MaxResponseBytes: value.MaxResponseBytes,
		MaxSearchResults: value.MaxSearchResults, MaxReplayCacheBytes: value.MaxReplayCacheBytes,
		MaxTranscriptBytes: value.MaxTranscriptBytes,
	}
}

func invokeTool(handler http.Handler, request codingrunner.ToolRequest) codingrunner.ToolResponse {
	body, _ := json.Marshal(request)
	recorder := httptest.NewRecorder()
	httpRequest := httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body))
	handler.ServeHTTP(recorder, httpRequest)
	var response codingrunner.ToolResponse
	_ = json.Unmarshal(recorder.Body.Bytes(), &response)
	return response
}

func tarFixture(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var output bytes.Buffer
	archive := tar.NewWriter(&output)
	paths := make([]string, 0, len(files))
	for name := range files {
		paths = append(paths, name)
	}
	slices.Sort(paths)
	for _, name := range paths {
		body := files[name]
		if err := archive.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := archive.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func mustJSON(t *testing.T, value any) json.RawMessage {
	t.Helper()
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return body
}
