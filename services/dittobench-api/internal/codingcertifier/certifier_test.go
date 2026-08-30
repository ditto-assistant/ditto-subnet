package codingcertifier

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type scriptedHarness struct {
	mu               sync.Mutex
	order            []string
	health           HealthResponse
	run              func(context.Context, codingcontract.RunRequest) (RunResponse, error)
	seedCalls        int
	healthErr        error
	seedErr          error
	brokenSeedReplay bool
}

type blockingHealthHarness struct {
	delegate HarnessClient
	started  chan struct{}
	release  chan struct{}
}

func (harness *blockingHealthHarness) Health(ctx context.Context) (HealthResponse, error) {
	close(harness.started)
	select {
	case <-ctx.Done():
		return HealthResponse{}, ctx.Err()
	case <-harness.release:
		return harness.delegate.Health(ctx)
	}
}

func (harness *blockingHealthHarness) Seed(
	ctx context.Context,
	request codingcontract.SeedRequest,
) (SeedResponse, error) {
	return harness.delegate.Seed(ctx, request)
}

func (harness *blockingHealthHarness) Run(
	ctx context.Context,
	request codingcontract.RunRequest,
) (RunResponse, error) {
	return harness.delegate.Run(ctx, request)
}

func (harness *scriptedHarness) append(value string) {
	harness.mu.Lock()
	defer harness.mu.Unlock()
	harness.order = append(harness.order, value)
}

func (harness *scriptedHarness) Health(_ context.Context) (HealthResponse, error) {
	harness.append("health")
	return harness.health, harness.healthErr
}

func (harness *scriptedHarness) Seed(_ context.Context, request codingcontract.SeedRequest) (SeedResponse, error) {
	harness.append("seed")
	harness.mu.Lock()
	harness.seedCalls++
	count := harness.seedCalls
	harness.mu.Unlock()
	if harness.seedErr != nil {
		return SeedResponse{}, harness.seedErr
	}
	return SeedResponse{
		CaseID: request.CaseID, ProfileCapabilityID: request.ProfileCapabilityID,
		MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
		IdempotentReplay: count > 1 && !harness.brokenSeedReplay,
	}, nil
}

func (harness *scriptedHarness) Run(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error) {
	harness.append("run")
	return harness.run(ctx, request)
}

type loopbackPublisher struct {
	mu        sync.Mutex
	published int
	binding   CapabilityBinding
	closeErr  error
}

func (publisher *loopbackPublisher) Publish(
	_ context.Context,
	binding CapabilityBinding,
	handler http.Handler,
) (PublishedCapability, error) {
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	publisher.published++
	publisher.binding = binding
	server := httptest.NewServer(handler)
	return &loopbackCapability{server: server, closeErr: publisher.closeErr}, nil
}

type loopbackCapability struct {
	mu       sync.Mutex
	server   *httptest.Server
	revoked  bool
	closeErr error
}

type memoryTranscriptSink struct {
	mu        sync.Mutex
	binding   EvidenceBinding
	body      []byte
	artifact  TranscriptArtifact
	commitErr error
}

func (sink *memoryTranscriptSink) Begin(
	_ context.Context,
	binding EvidenceBinding,
) (TranscriptWriter, error) {
	sink.mu.Lock()
	sink.binding = binding
	sink.mu.Unlock()
	return &memoryTranscriptWriter{sink: sink}, nil
}

type memoryTranscriptWriter struct {
	sink      *memoryTranscriptSink
	buffer    bytes.Buffer
	committed bool
}

func (writer *memoryTranscriptWriter) Write(body []byte) (int, error) {
	return writer.buffer.Write(body)
}

func (writer *memoryTranscriptWriter) Commit(
	_ context.Context,
	identity codingrunner.TranscriptIdentity,
) (TranscriptArtifact, error) {
	if writer.committed {
		return TranscriptArtifact{}, errors.New("transcript was already committed")
	}
	writer.sink.mu.Lock()
	commitErr := writer.sink.commitErr
	writer.sink.mu.Unlock()
	if commitErr != nil {
		return TranscriptArtifact{}, commitErr
	}
	body := append([]byte(nil), writer.buffer.Bytes()...)
	artifact := TranscriptArtifact{
		ObjectKey: "sha256/" + byteDigest(body), SHA256: byteDigest(body),
		SizeBytes: int64(len(body)), Events: identity.Events,
	}
	writer.sink.mu.Lock()
	writer.sink.body = body
	writer.sink.artifact = artifact
	writer.sink.mu.Unlock()
	writer.committed = true
	return artifact, nil
}

func (writer *memoryTranscriptWriter) Abort() error { return nil }

type memoryFrozenSubmissionSink struct {
	mu         sync.Mutex
	binding    EvidenceBinding
	artifact   FrozenSubmissionArtifact
	patchBytes []byte
	err        error
}

func (sink *memoryFrozenSubmissionSink) Store(
	_ context.Context,
	binding EvidenceBinding,
	submission codingrunner.FrozenSubmission,
) (FrozenSubmissionArtifact, error) {
	sink.mu.Lock()
	defer sink.mu.Unlock()
	if sink.err != nil {
		return FrozenSubmissionArtifact{}, sink.err
	}
	artifact := FrozenSubmissionArtifact{
		ObjectKey:         "sha256/" + submission.FrozenPatchSHA256,
		FrozenPatchSHA256: submission.FrozenPatchSHA256,
		FinalTreeSHA256:   submission.FinalTreeSHA256,
		ChangedPathRoot:   submission.ChangedPathRoot,
	}
	sink.binding = binding
	sink.artifact = artifact
	sink.patchBytes = append([]byte(nil), submission.Patch...)
	return artifact, nil
}

type staticInferenceEvidence struct {
	mu       sync.Mutex
	binding  InferenceBinding
	evidence codingcontract.ModelEvidence
	err      error
}

func (source *staticInferenceEvidence) Evidence(
	_ context.Context,
	binding InferenceBinding,
) (codingcontract.ModelEvidence, error) {
	source.mu.Lock()
	defer source.mu.Unlock()
	source.binding = binding
	return source.evidence, source.err
}

func (capability *loopbackCapability) URL() string {
	return capability.server.URL + "/tool"
}

func (capability *loopbackCapability) Revoke(_ context.Context) error {
	capability.mu.Lock()
	defer capability.mu.Unlock()
	if !capability.revoked {
		capability.server.Close()
		capability.revoked = true
	}
	return nil
}

func (capability *loopbackCapability) Close() error {
	return errors.Join(capability.Revoke(context.Background()), capability.closeErr)
}

type passingGraderExecutor struct {
	mu           sync.Mutex
	commands     []string
	manifest     codinggrader.Manifest
	preflightErr error
	failGroup    string
}

func (executor *passingGraderExecutor) Execute(
	_ context.Context,
	_ string,
	command codingrunner.CommandSpec,
) (codingrunner.CommandResult, error) {
	executor.mu.Lock()
	executor.commands = append(executor.commands, command.ID)
	executor.mu.Unlock()
	return codingrunner.CommandResult{ReturnCode: 0, Stdout: "visible test passed", Duration: time.Millisecond}, nil
}

func (executor *passingGraderExecutor) Preflight(
	_ context.Context,
	_ string,
) (codinggrader.ExecutorAttestation, error) {
	if executor.preflightErr != nil {
		return codinggrader.ExecutorAttestation{}, executor.preflightErr
	}
	return codinggrader.ExecutorAttestation{
		ExecutorInstanceID: "grader-cert-001", GraderImageDigest: executor.manifest.GraderImageDigest,
		GraderPlatform: executor.manifest.GraderPlatform, GraderContractSHA256: executor.manifest.GraderContractSHA256,
		GraderPlanSHA256: executor.manifest.GraderPlanSHA256, ResourceProfileSHA256: executor.manifest.ResourceProfileSHA256,
		NetworkDisabled: true, CandidateMountReadOnly: true, ProtectedMountHidden: true, ProcessGroupsIsolated: true,
	}, nil
}

func (executor *passingGraderExecutor) Build(
	_ context.Context,
	workspace string,
	command codingrunner.CommandSpec,
) (codinggrader.BuildRun, error) {
	if _, err := os.ReadFile(filepath.Join(workspace, "app.py")); err != nil {
		return codinggrader.BuildRun{}, errors.New("certification workspace is absent")
	}
	digest, _ := codinggrader.CommandSHA256(command.ID, command.Argv, command.Timeout.Milliseconds())
	return codinggrader.BuildRun{
		CommandID: command.ID, CommandSHA256: digest, ExecutorInstanceID: "grader-cert-001",
		ReturnCode: 0, Completed: true,
	}, nil
}

func (executor *passingGraderExecutor) Test(
	_ context.Context,
	workspace string,
	protected string,
	group codinggrader.TestGroupSpec,
) (codinggrader.TestRun, error) {
	if _, err := os.Stat(filepath.Join(protected, ".dittobench-grader", "tests.json")); err != nil {
		return codinggrader.TestRun{}, errors.New("protected certification grader is absent")
	}
	if _, err := os.Stat(filepath.Join(workspace, ".dittobench-grader", "tests.json")); !os.IsNotExist(err) {
		return codinggrader.TestRun{}, errors.New("protected grader leaked into candidate workspace")
	}
	digest, _ := codinggrader.CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
	body, err := os.ReadFile(filepath.Join(workspace, "app.py"))
	if err != nil {
		return codinggrader.TestRun{}, errors.New("certification workspace is absent")
	}
	if group.Group == executor.failGroup || !strings.Contains(string(body), "rstrip") {
		return codinggrader.TestRun{
			CommandID: group.Command.ID, CommandSHA256: digest, ExecutorInstanceID: "grader-cert-001",
			ReturnCode: 1, Passed: group.ExpectedTotal - 1, Total: group.ExpectedTotal, Completed: true,
		}, nil
	}
	return codinggrader.TestRun{
		CommandID: group.Command.ID, CommandSHA256: digest, ExecutorInstanceID: "grader-cert-001",
		ReturnCode: 0, Passed: group.ExpectedTotal, Total: group.ExpectedTotal, Completed: true,
	}, nil
}

type certificationFixture struct {
	request     Request
	visible     []byte
	grader      []byte
	harness     *scriptedHarness
	publisher   *loopbackPublisher
	graderExec  *passingGraderExecutor
	transcripts *memoryTranscriptSink
	frozen      *memoryFrozenSubmissionSink
	inference   *staticInferenceEvidence
	certifier   *Certifier
}

func newCertificationFixture(t *testing.T) certificationFixture {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	visible := tarBytes(t, map[string]string{
		"app.py":   "def normalize(value):\n    return value.strip()\n",
		"tests.py": "def test_visible():\n    assert True\n",
	})
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(visible), limits)
	if err != nil {
		t.Fatal(err)
	}
	runnerManifest := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		TicketID:              "ticket-cert-001", CaseID: "case-cert-001", ProfileCapabilityID: "profile-cert-001",
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		Deadline: now.Add(time.Hour), EditablePaths: []string{"app.py"}, CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingrunner.CommandSpec{{
			ID: "visible-unit", Argv: []string{"python", "-m", "pytest", "tests.py"}, Timeout: time.Minute,
		}},
		BuildCommands: []codingrunner.CommandSpec{}, Limits: limits,
	}
	graderBundle := tarBytes(t, map[string]string{".dittobench-grader/tests.json": "{}\n"})
	protectedLimits := codingrunner.DefaultLimits()
	protectedLimits.MaxBundleBytes = 8 << 20
	protectedLimits.MaxWorkspaceBytes = 16 << 20
	protectedLimits.MaxFileBytes = 4 << 20
	protectedLimits.MaxPatchBytes = 4 << 20
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: limits, ProtectedLimits: protectedLimits,
		MaxCombinedDiskBytes: limits.MaxWorkspaceBytes + protectedLimits.MaxWorkspaceBytes + limits.MaxBundleBytes + 1<<30,
		MemoryLimitBytes:     4 << 30, ScratchLimitBytes: 1 << 30, PidsLimit: 512, CPUQuotaMillis: 2_000,
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	groups := make([]codinggrader.TestGroupSpec, 0, 5)
	for index, group := range []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"} {
		groups = append(groups, codinggrader.TestGroupSpec{
			Group: group,
			Command: codingrunner.CommandSpec{
				ID: "cert-" + group, Argv: []string{"dittobench-test-driver", group}, Timeout: time.Minute,
			},
			ExpectedTotal: uint32(index + 1),
		})
	}
	graderManifest := codinggrader.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                runnerManifest.CaseID, VariantID: "variant-cert-v1",
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		GraderContractSHA256: codinggrader.GraderContractSHA256(), GraderBundleSHA256: byteDigest(graderBundle),
		GraderImageDigest: "sha256:" + strings.Repeat("2", 64), GraderPlatform: "linux/amd64",
		TestManifestSHA256: strings.Repeat("3", 64), ResourceProfileSHA256: resourceSHA,
		Deadline: now.Add(time.Hour), ExecutionTimeout: 30 * time.Minute, ResourcePolicy: policy,
		Build: codinggrader.BuildSpec{Required: true, Command: codingrunner.CommandSpec{
			ID: "cert-build", Argv: []string{"python", "-m", "compileall", "app.py"}, Timeout: time.Minute,
		}},
		TestGroups: groups,
	}
	graderManifest.GraderPlanSHA256, err = codinggrader.GraderPlanSHA256(graderManifest)
	if err != nil {
		t.Fatal(err)
	}
	seed := fixtureSeed(t)
	request := Request{
		CertificationID: "certification-001", AgentArtifactSHA256: strings.Repeat("a", 64),
		HarnessAttestation: HarnessAttestation{
			HarnessInstanceID: "harness-instance-001", AgentArtifactSHA256: strings.Repeat("a", 64),
			ReadOnlyRootFilesystem: true, NetworkPolicyEnforced: true, CapabilityEgressOnly: true,
			NoHostDockerSocket: true, CredentialsAbsent: true,
		},
		Seed: seed, Issue: codingcontract.Issue{
			Title: "Fix normalize", Description: "Preserve leading whitespace while removing trailing whitespace.", Constraints: []string{},
		},
		RepositoryEpoch: "repository-epoch-001", InferenceBaseURL: "http://inference.invalid/capability",
		InferenceGrantSHA256: strings.Repeat("4", 64), SolverModel: CertificationSolverModel,
		SolverProvider: "scripted-practice", ProviderRouteProfile: "certification-fixture-v1",
		Budgets: codingcontract.Budgets{
			ModelInputTokens: 10_000, ModelOutputTokens: 2_000, WorkspaceToolCalls: 16, WallTimeSeconds: 60,
		},
		RunnerManifest: runnerManifest, GraderManifest: graderManifest,
	}
	request.CanaryManifestSHA256, err = CanaryManifestSHA256(request)
	if err != nil {
		t.Fatal(err)
	}
	harness := &scriptedHarness{health: HealthResponse{
		Status: "ok", SupportedCodingContractVersions: []int{1},
		Capabilities: []string{"scoped_memory_seed_v1", "coding_runner_tools_v1", "case_scoped_inference_v1"},
	}}
	harness.run = successfulCanaryRun
	publisher := &loopbackPublisher{}
	graderExec := &passingGraderExecutor{manifest: graderManifest}
	transcripts := &memoryTranscriptSink{}
	frozen := &memoryFrozenSubmissionSink{}
	receiptRoot := strings.Repeat("6", 64)
	inference := &staticInferenceEvidence{evidence: codingcontract.ModelEvidence{
		Model: request.SolverModel, Provider: request.SolverProvider,
		ProviderRouteProfile: request.ProviderRouteProfile, ReasoningEffort: "medium",
		InferenceGrantSHA256: request.InferenceGrantSHA256,
		PromptSHA256:         strings.Repeat("7", 64), ToolSchemaSHA256: strings.Repeat("8", 64),
		UsageStatus: codingcontract.ModelUsageComplete, FallbackUsed: false,
		CostSource: "provider_receipt_v1", Currency: "USD", ProviderReceiptSetSHA256: &receiptRoot,
		Requests: 1, PromptTokens: 10, CompletionTokens: 5, TotalTokens: 15, CostUSDMicros: 10,
	}}
	certifier, err := New(Config{
		Harness: harness, Publisher: publisher, Executor: graderExec,
		TranscriptSink: transcripts, FrozenSubmissionSink: frozen, InferenceEvidence: inference,
		OpenVisibleBundle: bytesOpener(visible), OpenGraderBundle: bytesOpener(graderBundle),
		CertificationTTL: time.Hour, Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	return certificationFixture{
		request: request, visible: visible, grader: graderBundle, harness: harness,
		publisher: publisher, graderExec: graderExec, transcripts: transcripts, frozen: frozen,
		inference: inference, certifier: certifier,
	}
}

func successfulCanaryRun(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error) {
	read := invokeWorkspace(tContext{ctx}, request, "cert-read", "repo.read_file", map[string]any{"path": "app.py"})
	var readResult struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readResult); err != nil {
		return RunResponse{}, err
	}
	invokeWorkspace(tContext{ctx}, request, "cert-edit", "repo.apply_patch", map[string]any{
		"path": "app.py", "expected_sha256": readResult.SHA256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	})
	invokeWorkspace(tContext{ctx}, request, "cert-test", "tests.run", map[string]any{"command_id": "visible-unit"})
	invokeWorkspace(tContext{ctx}, request, "cert-diff", "git.diff", map[string]any{})
	response := RunResponse{CaseID: request.CaseID}
	response.FinalReport.Summary = "Applied the canary repair and ran the visible test."
	response.FinalReport.RemainingRisks = []string{}
	return response, nil
}

type tContext struct{ context.Context }

func invokeWorkspace(
	ctx tContext,
	request codingcontract.RunRequest,
	callID string,
	name string,
	arguments any,
) codingrunner.ToolResponse {
	body, err := json.Marshal(codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                request.CaseID, ProfileCapabilityID: request.ProfileCapabilityID,
		CallID: callID, Name: name, Arguments: mustJSON(arguments),
	})
	if err != nil {
		panic(err)
	}
	httpRequest, err := http.NewRequestWithContext(ctx, http.MethodPost, request.WorkspaceCapabilityURL, bytes.NewReader(body))
	if err != nil {
		panic(err)
	}
	httpRequest.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(httpRequest)
	if err != nil {
		panic(err)
	}
	defer response.Body.Close()
	var toolResponse codingrunner.ToolResponse
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&toolResponse) != nil || !toolResponse.OK {
		panic("workspace certification call failed")
	}
	return toolResponse
}

func mustJSON(value any) json.RawMessage {
	body, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return body
}

func TestCertifierRequiresTheCompleteArtifactBoundCanary(t *testing.T) {
	fixture := newCertificationFixture(t)
	if fixture.request.CanaryManifestSHA256 != "acede79c745bbe27e29d2c6cf2147dcb0d53867046ed20dfda2cd48ac658233c" {
		t.Fatalf("canary manifest digest changed: %s", fixture.request.CanaryManifestSHA256)
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusCertified || receipt.WeightEligible || receipt.CanaryTerminalDomain == nil ||
		*receipt.CanaryTerminalDomain != codingcontract.DomainResolved || receipt.AuthoringEventCount != 4 ||
		receipt.FrozenPatchSHA256 == nil || receipt.GraderExecutionReceiptRootSHA256 == nil ||
		receipt.AuthoringTranscriptObjectKey == nil || receipt.ModelEvidence == nil {
		t.Fatalf("receipt=%#v", receipt)
	}
	if err := receipt.Validate(); err != nil {
		t.Fatal(err)
	}
	if err := receipt.ValidateAt(time.Unix(receipt.IssuedAtUnix, 0)); err != nil {
		t.Fatal(err)
	}
	if err := receipt.ValidateAt(time.Unix(receipt.ExpiresAtUnix, 0)); err == nil {
		t.Fatal("expired certification remained active")
	}
	fixture.harness.mu.Lock()
	order := append([]string(nil), fixture.harness.order...)
	fixture.harness.mu.Unlock()
	if !slices.Equal(order, []string{"health", "seed", "seed", "run"}) {
		t.Fatalf("unexpected certification order: %v", order)
	}
	fixture.publisher.mu.Lock()
	binding := fixture.publisher.binding
	fixture.publisher.mu.Unlock()
	if binding.AgentArtifactSHA256 != fixture.request.AgentArtifactSHA256 ||
		binding.HarnessInstanceID != fixture.request.HarnessAttestation.HarnessInstanceID {
		t.Fatalf("capability binding=%#v", binding)
	}
	fixture.graderExec.mu.Lock()
	commands := append([]string(nil), fixture.graderExec.commands...)
	fixture.graderExec.mu.Unlock()
	if !slices.Equal(commands, []string{"visible-unit"}) {
		t.Fatalf("authoring commands=%v", commands)
	}
	fixture.transcripts.mu.Lock()
	transcriptBody := append([]byte(nil), fixture.transcripts.body...)
	transcriptBinding := fixture.transcripts.binding
	fixture.transcripts.mu.Unlock()
	if len(transcriptBody) == 0 || transcriptBinding.AgentArtifactSHA256 != fixture.request.AgentArtifactSHA256 {
		t.Fatalf("transcript binding=%#v bytes=%d", transcriptBinding, len(transcriptBody))
	}
	fixture.frozen.mu.Lock()
	frozenPatch := append([]byte(nil), fixture.frozen.patchBytes...)
	frozenBinding := fixture.frozen.binding
	fixture.frozen.mu.Unlock()
	if len(frozenPatch) == 0 || frozenBinding.CanaryManifestSHA256 != fixture.request.CanaryManifestSHA256 {
		t.Fatalf("frozen binding=%#v bytes=%d", frozenBinding, len(frozenPatch))
	}
	fixture.inference.mu.Lock()
	inferenceBinding := fixture.inference.binding
	fixture.inference.mu.Unlock()
	if inferenceBinding.InferenceGrantSHA256 != fixture.request.InferenceGrantSHA256 ||
		inferenceBinding.ProfileCapabilityID != fixture.request.Seed.ProfileCapabilityID ||
		!inferenceBinding.Deadline.Equal(fixture.request.RunnerManifest.Deadline) ||
		inferenceBinding.Budgets != fixture.request.Budgets ||
		inferenceBinding.RequestBudget != codingcontract.EffectiveInferenceRequestBudget(fixture.request.Budgets.WorkspaceToolCalls) {
		t.Fatalf("inference binding=%#v", inferenceBinding)
	}
}

func TestCertifierKeepsUnsupportedAgentsCoreOnly(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.health.Capabilities = []string{"case_scoped_inference_v1"}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusUnsupported || receipt.FailureStage == nil || *receipt.FailureStage != StageHealth ||
		receipt.WeightEligible || fixture.publisher.published != 0 || fixture.harness.seedCalls != 0 {
		t.Fatalf("unsupported receipt=%#v", receipt)
	}
	if err := receipt.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestCertifierRejectsHealthOnlyClaims(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.run = func(context.Context, codingcontract.RunRequest) (RunResponse, error) {
		return RunResponse{}, errors.New("declared coding endpoint cannot run")
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusFailed || receipt.FailureStage == nil || *receipt.FailureStage != StageRun ||
		receipt.FailureCode == nil || *receipt.FailureCode != "coding_run_failed" {
		t.Fatalf("health-only receipt=%#v", receipt)
	}
}

func TestCertifierRequiresAuthoritativeCapabilitySequence(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.run = func(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error) {
		read := invokeWorkspace(tContext{ctx}, request, "partial-read", "repo.read_file", map[string]any{"path": "app.py"})
		var result struct {
			SHA256 string `json:"sha256"`
		}
		if err := json.Unmarshal(read.Result, &result); err != nil {
			return RunResponse{}, err
		}
		invokeWorkspace(tContext{ctx}, request, "partial-edit", "repo.apply_patch", map[string]any{
			"path": "app.py", "expected_sha256": result.SHA256,
			"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
		})
		response := RunResponse{CaseID: request.CaseID}
		response.FinalReport.Summary = "patched without the required validation sequence"
		response.FinalReport.RemainingRisks = []string{}
		return response, nil
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusFailed || receipt.FailureCode == nil ||
		*receipt.FailureCode != "coding_capability_proof_incomplete" || receipt.AuthoringTranscriptObjectKey == nil {
		t.Fatalf("receipt=%#v", receipt)
	}
}

func TestCertifierRequiresTrustedInferenceEvidence(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.inference.err = ErrInferenceNotObserved
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusFailed || receipt.FailureCode == nil ||
		*receipt.FailureCode != "coding_inference_not_observed" || receipt.ModelEvidence != nil {
		t.Fatalf("receipt=%#v", receipt)
	}
}

func TestCertifierRejectsInferenceEvidenceBeyondSignedRunBudget(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.inference.evidence.PromptTokens = fixture.request.Budgets.ModelInputTokens + 1
	fixture.inference.evidence.TotalTokens =
		fixture.inference.evidence.PromptTokens + fixture.inference.evidence.CompletionTokens
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err == nil || receipt.Status != "" || !strings.Contains(err.Error(), "canary authority") {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestEvidenceInfrastructureDoesNotDecertify(t *testing.T) {
	tests := map[string]func(*certificationFixture){
		"transcript": func(fixture *certificationFixture) {
			fixture.transcripts.commitErr = errors.New("artifact store unavailable")
		},
		"inference": func(fixture *certificationFixture) {
			fixture.inference.err = errors.New("relay receipt store unavailable")
		},
		"frozen_submission": func(fixture *certificationFixture) {
			fixture.frozen.err = errors.New("frozen outbox unavailable")
		},
	}
	for name, configure := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newCertificationFixture(t)
			configure(&fixture)
			receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
			if err == nil || receipt.Status != "" {
				t.Fatalf("receipt=%#v err=%v", receipt, err)
			}
			switch name {
			case "transcript":
				fixture.frozen.mu.Lock()
				stored := len(fixture.frozen.patchBytes)
				fixture.frozen.mu.Unlock()
				if stored == 0 {
					t.Fatal("frozen submission was not retained after transcript failure")
				}
			case "frozen_submission":
				fixture.transcripts.mu.Lock()
				stored := len(fixture.transcripts.body)
				fixture.transcripts.mu.Unlock()
				if stored == 0 {
					t.Fatal("transcript was not retained after frozen-submission failure")
				}
			}
		})
	}
}

func TestPreAuthoritativeTransportFailureIsInfrastructure(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.inference.err = ErrInferenceNotObserved
	fixture.harness.run = func(context.Context, codingcontract.RunRequest) (RunResponse, error) {
		return RunResponse{}, &HarnessError{
			Operation: "run", Kind: HarnessFailureTransport, Err: errors.New("route unavailable"),
		}
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err == nil || receipt.Status != "" || !strings.Contains(err.Error(), "before authoritative activity") {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestRelayEvidenceMakesTransportFailureAuthoritative(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.run = func(context.Context, codingcontract.RunRequest) (RunResponse, error) {
		return RunResponse{}, &HarnessError{
			Operation: "run", Kind: HarnessFailureTransport, Err: errors.New("response stream closed"),
		}
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil || receipt.Status != StatusFailed || receipt.FailureCode == nil ||
		*receipt.FailureCode != "coding_run_failed" || receipt.ModelEvidence == nil {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestPostAuthoritativeTransportFailureStillGrades(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.run = func(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error) {
		response, err := successfulCanaryRun(ctx, request)
		if err != nil {
			return RunResponse{}, err
		}
		return response, &HarnessError{
			Operation: "run", Kind: HarnessFailureTransport, Err: errors.New("response stream closed"),
		}
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil || receipt.Status != StatusCertified {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestCertifierRequiresSeedIdempotencyBeforePublishingWorkspace(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.harness.brokenSeedReplay = true
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusFailed || receipt.FailureStage == nil || *receipt.FailureStage != StageSeed ||
		receipt.FailureCode == nil || *receipt.FailureCode != "coding_seed_idempotency_failed" ||
		fixture.publisher.published != 0 {
		t.Fatalf("receipt=%#v published=%d", receipt, fixture.publisher.published)
	}
}

func TestCertifierDoesNotBlameTheAgentForGraderInfrastructure(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.graderExec.preflightErr = errors.New("daemon unavailable")
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err == nil || receipt.Status != "" || !strings.Contains(err.Error(), "not candidate-attributable") {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestGraderInfrastructureTakesPrecedenceOverRunFailure(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.graderExec.preflightErr = errors.New("daemon unavailable")
	fixture.harness.run = func(ctx context.Context, request codingcontract.RunRequest) (RunResponse, error) {
		response, err := successfulCanaryRun(ctx, request)
		if err != nil {
			return RunResponse{}, err
		}
		return response, errors.New("response transport failed after authoring")
	}
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err == nil || receipt.Status != "" || !strings.Contains(err.Error(), "not candidate-attributable") {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestCertifierRecordsCandidateAttributableCanaryFailure(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.graderExec.failGroup = "hidden"
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Status != StatusFailed || receipt.FailureStage == nil || *receipt.FailureStage != StageGrade ||
		receipt.FailureCode == nil || *receipt.FailureCode != "tests_failed" ||
		receipt.CanaryTerminalDomain == nil || *receipt.CanaryTerminalDomain != codingcontract.DomainRepairFailure {
		t.Fatalf("receipt=%#v", receipt)
	}
	if err := receipt.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestCertificationRequestBindsArtifactCanaryAndResourcePolicy(t *testing.T) {
	fixture := newCertificationFixture(t)
	tests := map[string]func(*Request){
		"artifact": func(request *Request) {
			request.HarnessAttestation.AgentArtifactSHA256 = strings.Repeat("f", 64)
		},
		"canary": func(request *Request) {
			request.CanaryManifestSHA256 = strings.Repeat("f", 64)
		},
		"resource": func(request *Request) {
			request.RunnerManifest.Limits.MaxToolCalls--
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			request := fixture.request
			mutate(&request)
			if _, err := fixture.certifier.Certify(t.Context(), request); err == nil {
				t.Fatal("mutated trusted request was accepted")
			}
		})
	}
}

func TestCertificationReceiptDigestDetectsTampering(t *testing.T) {
	fixture := newCertificationFixture(t)
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	receipt.AgentArtifactSHA256 = strings.Repeat("f", 64)
	if err := receipt.Validate(); err == nil || !strings.Contains(err.Error(), "digest") {
		t.Fatalf("expected digest mismatch, got %v", err)
	}
}

func TestCleanupFailureReturnsNoUsableReceipt(t *testing.T) {
	fixture := newCertificationFixture(t)
	fixture.publisher.closeErr = errors.New("capability cleanup failed")
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err == nil || receipt.Status != "" || !strings.Contains(err.Error(), "cleanup") {
		t.Fatalf("receipt=%#v err=%v", receipt, err)
	}
}

func TestCertifierDeepClonesRequestBeforeExternalCalls(t *testing.T) {
	fixture := newCertificationFixture(t)
	blocking := &blockingHealthHarness{
		delegate: fixture.harness, started: make(chan struct{}), release: make(chan struct{}),
	}
	certifier, err := New(Config{
		Harness: blocking, Publisher: fixture.publisher, Executor: fixture.graderExec,
		TranscriptSink: fixture.transcripts, FrozenSubmissionSink: fixture.frozen,
		InferenceEvidence: fixture.inference,
		OpenVisibleBundle: bytesOpener(fixture.visible), OpenGraderBundle: bytesOpener(fixture.grader),
		CertificationTTL: time.Hour, Now: func() time.Time { return time.Now().UTC() },
	})
	if err != nil {
		t.Fatal(err)
	}
	request := fixture.request
	result := make(chan struct {
		receipt Receipt
		err     error
	}, 1)
	go func() {
		receipt, runErr := certifier.Certify(t.Context(), request)
		result <- struct {
			receipt Receipt
			err     error
		}{receipt, runErr}
	}()
	<-blocking.started
	request.RunnerManifest.EditablePaths[0] = "mutated-after-validation.py"
	request.RunnerManifest.TestCommands[0].Argv[0] = "mutated"
	close(blocking.release)
	outcome := <-result
	if outcome.err != nil || outcome.receipt.Status != StatusCertified {
		t.Fatalf("receipt=%#v err=%v", outcome.receipt, outcome.err)
	}
}

func TestReceiptProjectionCommitsEveryKnownField(t *testing.T) {
	receiptType := reflect.TypeOf(Receipt{})
	projectionType := reflect.TypeOf(receiptProjection{})
	projectionFields := make(map[string]struct{}, projectionType.NumField())
	for index := range projectionType.NumField() {
		projectionFields[projectionType.Field(index).Tag.Get("json")] = struct{}{}
	}
	for index := range receiptType.NumField() {
		field := receiptType.Field(index)
		name := field.Tag.Get("json")
		if name == "certification_sha256" {
			continue
		}
		if _, exists := projectionFields[name]; !exists {
			t.Fatalf("receipt field %s is not committed by the digest projection", name)
		}
	}
	if projectionType.NumField() != receiptType.NumField()-1 {
		t.Fatal("receipt projection contains an unexpected field")
	}
}

func TestReceiptParserRequiresKnownFieldsAndIgnoresFutureFields(t *testing.T) {
	fixture := newCertificationFixture(t)
	receipt, err := fixture.certifier.Certify(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(receipt)
	if err != nil {
		t.Fatal(err)
	}
	var object map[string]any
	if err := json.Unmarshal(body, &object); err != nil {
		t.Fatal(err)
	}
	object["future_field"] = true
	forward, _ := json.Marshal(object)
	parsed, err := ParseReceipt(forward)
	if err != nil || parsed.CertificationSHA256 != receipt.CertificationSHA256 {
		t.Fatalf("parsed=%#v err=%v", parsed, err)
	}
	delete(object, "weight_eligible")
	missing, _ := json.Marshal(object)
	if _, err := ParseReceipt(missing); err == nil {
		t.Fatal("receipt missing a known field was accepted")
	}
	duplicate := bytes.Replace(body, []byte(`{"schema":`), []byte(`{"schema":"duplicate","schema":`), 1)
	if _, err := ParseReceipt(duplicate); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("duplicate receipt field was accepted: %v", err)
	}
}

func bytesOpener(body []byte) BundleOpener {
	copy := append([]byte(nil), body...)
	return func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(copy)), nil
	}
}

func tarBytes(t *testing.T, files map[string]string) []byte {
	t.Helper()
	paths := make([]string, 0, len(files))
	for path := range files {
		paths = append(paths, path)
	}
	slices.Sort(paths)
	var output bytes.Buffer
	archive := tar.NewWriter(&output)
	for _, path := range paths {
		body := files[path]
		header := &tar.Header{Name: path, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}
		if err := archive.WriteHeader(header); err != nil {
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

func byteDigest(body []byte) string {
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:])
}
