package codinggrader

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

type fixtureFile struct {
	path string
	body string
}

type graderWireVectors struct {
	Plan     json.RawMessage    `json:"grader_plan"`
	Resource json.RawMessage    `json:"grader_resource_profile"`
	Receipts []ExecutionReceipt `json:"grader_execution_receipts"`
	Digests  map[string]string  `json:"digests"`
}

func loadGraderWireVectors(t *testing.T) graderWireVectors {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", "coding_contract_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors graderWireVectors
	if err := json.Unmarshal(body, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

type slowReader struct {
	source io.Reader
	delay  time.Duration
	once   sync.Once
}

func (reader *slowReader) Read(buffer []byte) (int, error) {
	reader.once.Do(func() { time.Sleep(reader.delay) })
	return reader.source.Read(buffer)
}

func tarBundle(t *testing.T, files ...fixtureFile) []byte {
	t.Helper()
	var output bytes.Buffer
	archive := tar.NewWriter(&output)
	for _, file := range files {
		header := &tar.Header{Name: file.path, Mode: 0o644, Size: int64(len(file.body)), Typeflag: tar.TypeReg}
		if err := archive.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := archive.Write([]byte(file.body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func digest(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}

func frozenFixture(t *testing.T) (codingrunner.FrozenSubmission, []byte, codingrunner.Limits) {
	return frozenFixtureVersion(t, false)
}

func frozenFixtureVersion(t *testing.T, hosted bool) (codingrunner.FrozenSubmission, []byte, codingrunner.Limits) {
	t.Helper()
	visible := tarBundle(t,
		fixtureFile{path: "src/app.py", body: "def normalize(value):\n    return value.strip()\n"},
		fixtureFile{path: "tests/test_visible.py", body: "def test_visible():\n    assert True\n"},
	)
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(visible), limits)
	if err != nil {
		t.Fatal(err)
	}
	manifest := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		TicketID:              "ticket-grade-001",
		CaseID:                "case-grade-001",
		ProfileCapabilityID:   "profile-grade-001",
		VisibleBundleSHA256:   identity.VisibleBundleSHA256,
		BaseTreeSHA256:        identity.TreeSHA256,
		Deadline:              time.Now().Add(time.Hour),
		EditablePaths:         []string{"src/app.py"},
		CreatablePaths:        []string{},
		DeletablePaths:        []string{},
		TestCommands:          []codingrunner.CommandSpec{},
		BuildCommands:         []codingrunner.CommandSpec{},
		Limits:                limits,
	}
	var session *codingrunner.Session
	if hosted {
		manifest.CodingContractVersion = codingrunner.HostedContractVersion
		manifest.TicketID, manifest.CaseID = "10000000-0000-4000-8000-000000000001", "20000000-0000-4000-8000-000000000002"
		session, err = codingrunner.NewHostedSession(t.Context(), codingrunner.HostedAuthority{EvaluationID: manifest.TicketID, AttemptID: manifest.CaseID, AssignmentSHA256: strings.Repeat("a", 64)}, manifest, bytes.NewReader(visible), nil)
	} else {
		session, err = codingrunner.NewSession(t.Context(), manifest, bytes.NewReader(visible), nil)
	}
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	readRequest := codingrunner.ToolRequest{
		CodingContractVersion: manifest.CodingContractVersion,
		CaseID:                manifest.CaseID,
		ProfileCapabilityID:   manifest.ProfileCapabilityID,
		CallID:                "grade-read",
		Name:                  "repo.read_file",
		Arguments:             json.RawMessage(`{"path":"src/app.py"}`),
	}
	read, err := session.Invoke(t.Context(), readRequest)
	if err != nil || !read.OK {
		t.Fatalf("read response=%#v err=%v", read, err)
	}
	var readResult struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readResult); err != nil {
		t.Fatal(err)
	}
	arguments, _ := json.Marshal(map[string]any{
		"path": "src/app.py", "expected_sha256": readResult.SHA256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	})
	edit, err := session.Invoke(t.Context(), codingrunner.ToolRequest{
		CodingContractVersion: manifest.CodingContractVersion,
		CaseID:                manifest.CaseID,
		ProfileCapabilityID:   manifest.ProfileCapabilityID,
		CallID:                "grade-edit",
		Name:                  "repo.apply_patch",
		Arguments:             arguments,
	})
	if err != nil || !edit.OK {
		t.Fatalf("edit response=%#v err=%v", edit, err)
	}
	frozen := session.Freeze()
	if frozen.Submission == nil {
		t.Fatalf("freeze=%#v", frozen)
	}
	return *frozen.Submission, visible, limits
}

func graderFixture(t *testing.T, submission codingrunner.FrozenSubmission, limits codingrunner.Limits) (Manifest, []byte) {
	t.Helper()
	bundle := tarBundle(t,
		fixtureFile{path: ".dittobench-grader/hidden_test.py", body: "assert True\n"},
		fixtureFile{path: ".dittobench-grader/runner.json", body: "{}\n"},
	)
	groups := make([]TestGroupSpec, len(evidenceGroups))
	for index, name := range evidenceGroups {
		groups[index] = TestGroupSpec{
			Group: name,
			Command: codingrunner.CommandSpec{
				ID: "test-" + name, Argv: []string{"python", "-m", "pytest", ".dittobench-grader/hidden_test.py"}, Timeout: time.Minute,
			},
			ExpectedTotal: uint32(index + 1),
		}
	}
	protectedLimits := codingrunner.DefaultLimits()
	protectedLimits.MaxBundleBytes = 8 << 20
	protectedLimits.MaxWorkspaceBytes = 16 << 20
	protectedLimits.MaxFileBytes = 4 << 20
	protectedLimits.MaxPatchBytes = 4 << 20
	policy := ResourcePolicy{
		CandidateLimits:      limits,
		ProtectedLimits:      protectedLimits,
		MaxCombinedDiskBytes: limits.MaxWorkspaceBytes + protectedLimits.MaxWorkspaceBytes + limits.MaxBundleBytes + 1<<30,
		MemoryLimitBytes:     4 << 30,
		ScratchLimitBytes:    1 << 30,
		PidsLimit:            512,
		CPUQuotaMillis:       2_000,
	}
	resourceSHA, err := ResourceProfileSHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	manifest := Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                "case-grade-001",
		VariantID:             "variant-grade-v1",
		VisibleBundleSHA256:   submission.VisibleBundleSHA256,
		BaseTreeSHA256:        submission.BaseTreeSHA256,
		GraderContractSHA256:  GraderContractSHA256(),
		GraderBundleSHA256:    digest(bundle),
		GraderImageDigest:     "sha256:" + strings.Repeat("2", 64),
		GraderPlatform:        "linux/amd64",
		TestManifestSHA256:    strings.Repeat("3", 64),
		ResourceProfileSHA256: resourceSHA,
		Deadline:              time.Now().Add(time.Hour),
		ExecutionTimeout:      30 * time.Minute,
		ResourcePolicy:        policy,
		Build: BuildSpec{Required: true, Command: codingrunner.CommandSpec{
			ID: "build-python", Argv: []string{"python", "-m", "compileall", "src"}, Timeout: time.Minute,
		}},
		TestGroups: groups,
	}
	planSHA, err := GraderPlanSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.GraderPlanSHA256 = planSHA
	return manifest, bundle
}

func rebindPlan(t *testing.T, manifest *Manifest) {
	t.Helper()
	planSHA, err := GraderPlanSHA256(*manifest)
	if err != nil {
		t.Fatal(err)
	}
	manifest.GraderPlanSHA256 = planSHA
}

type fakeExecutor struct {
	mu              sync.Mutex
	groups          []string
	buildRun        BuildRun
	buildErr        error
	testRuns        map[string]TestRun
	testErrors      map[string]error
	mutate          bool
	mutateProtected bool
	requireFixture  bool
	attestation     ExecutorAttestation
	preflightErr    error
	preflightPlan   string
	corruptReceipt  bool
}

type lateExecutor struct{ attestation ExecutorAttestation }

func (executor lateExecutor) Preflight(_ context.Context, _ string) (ExecutorAttestation, error) {
	return executor.attestation, nil
}

func (executor lateExecutor) Build(ctx context.Context, _ string, command codingrunner.CommandSpec) (BuildRun, error) {
	<-ctx.Done()
	commandSHA, _ := CommandSHA256(command.ID, command.Argv, command.Timeout.Milliseconds())
	return BuildRun{
		CommandID: command.ID, CommandSHA256: commandSHA, ExecutorInstanceID: executor.attestation.ExecutorInstanceID,
		ReturnCode: 0, Completed: true,
	}, nil
}

func (executor lateExecutor) Test(_ context.Context, _ string, _ string, group TestGroupSpec) (TestRun, error) {
	commandSHA, _ := CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
	return TestRun{
		CommandID: group.Command.ID, CommandSHA256: commandSHA, ExecutorInstanceID: executor.attestation.ExecutorInstanceID,
		ReturnCode: 0, Passed: group.ExpectedTotal, Total: group.ExpectedTotal, Completed: true,
	}, nil
}

func (executor *fakeExecutor) Preflight(_ context.Context, expectedPlanSHA256 string) (ExecutorAttestation, error) {
	executor.preflightPlan = expectedPlanSHA256
	return executor.attestation, executor.preflightErr
}

func (executor *fakeExecutor) checkWorkspace(workspace string) error {
	if !executor.requireFixture {
		return nil
	}
	body, err := os.ReadFile(filepath.Join(workspace, "src", "app.py"))
	if err != nil || !strings.Contains(string(body), "value.rstrip()") {
		return errors.New("frozen patch missing from grader workspace")
	}
	if _, err := os.Stat(filepath.Join(workspace, ".dittobench-grader", "hidden_test.py")); !os.IsNotExist(err) {
		return errors.New("candidate workspace could access grader bundle")
	}
	return nil
}

func (executor *fakeExecutor) checkProtected(protected string) error {
	if !executor.requireFixture {
		return nil
	}
	if _, err := os.Stat(filepath.Join(protected, ".dittobench-grader", "hidden_test.py")); err != nil {
		return errors.New("protected grader bundle is unavailable")
	}
	return nil
}

func (executor *fakeExecutor) Build(_ context.Context, workspace string, command codingrunner.CommandSpec) (BuildRun, error) {
	if err := executor.checkWorkspace(workspace); err != nil {
		return BuildRun{}, err
	}
	if executor.mutate {
		_ = os.WriteFile(filepath.Join(workspace, "src", "app.py"), []byte("tampered\n"), 0o644)
	}
	result := executor.buildRun
	result.CommandID = command.ID
	result.CommandSHA256, _ = CommandSHA256(command.ID, command.Argv, command.Timeout.Milliseconds())
	result.ExecutorInstanceID = executor.attestation.ExecutorInstanceID
	if executor.corruptReceipt {
		result.CommandSHA256 = strings.Repeat("f", 64)
	}
	return result, executor.buildErr
}

func (executor *fakeExecutor) Test(_ context.Context, workspace string, protected string, group TestGroupSpec) (TestRun, error) {
	if err := executor.checkWorkspace(workspace); err != nil {
		return TestRun{}, err
	}
	if err := executor.checkProtected(protected); err != nil {
		return TestRun{}, err
	}
	if executor.mutateProtected {
		_ = os.WriteFile(filepath.Join(protected, ".dittobench-grader", "hidden_test.py"), []byte("tampered\n"), 0o600)
	}
	executor.mu.Lock()
	executor.groups = append(executor.groups, group.Group)
	executor.mu.Unlock()
	if err := executor.testErrors[group.Group]; err != nil {
		return TestRun{}, err
	}
	if run, exists := executor.testRuns[group.Group]; exists {
		run.CommandID = group.Command.ID
		run.CommandSHA256, _ = CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
		run.ExecutorInstanceID = executor.attestation.ExecutorInstanceID
		if executor.corruptReceipt {
			run.CommandSHA256 = strings.Repeat("f", 64)
		}
		return run, nil
	}
	commandSHA, _ := CommandSHA256(group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds())
	if executor.corruptReceipt {
		commandSHA = strings.Repeat("f", 64)
	}
	return TestRun{
		CommandID: group.Command.ID, CommandSHA256: commandSHA, ExecutorInstanceID: executor.attestation.ExecutorInstanceID,
		ReturnCode: 0, Passed: group.ExpectedTotal, Total: group.ExpectedTotal, Completed: true,
	}, nil
}

func attestationFor(manifest Manifest) ExecutorAttestation {
	return ExecutorAttestation{
		ExecutorInstanceID: "grader-executor-001", GraderImageDigest: manifest.GraderImageDigest,
		GraderPlatform:       manifest.GraderPlatform,
		GraderContractSHA256: manifest.GraderContractSHA256, GraderPlanSHA256: manifest.GraderPlanSHA256,
		ResourceProfileSHA256: manifest.ResourceProfileSHA256, NetworkDisabled: true,
		CandidateMountReadOnly: true, ProtectedMountHidden: true, ProcessGroupsIsolated: true,
	}
}

func configureExecutor(manifest Manifest, executor *fakeExecutor) *fakeExecutor {
	executor.attestation = attestationFor(manifest)
	if executor.testRuns == nil {
		executor.testRuns = map[string]TestRun{}
	}
	if executor.testErrors == nil {
		executor.testErrors = map[string]error{}
	}
	return executor
}

func passingExecutor(manifest Manifest) *fakeExecutor {
	return configureExecutor(manifest, &fakeExecutor{
		buildRun: BuildRun{ReturnCode: 0, Completed: true},
		testRuns: map[string]TestRun{}, testErrors: map[string]error{}, requireFixture: true,
	})
}

func TestGradeResolvesOnlyCompletePristineEvidence(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	executor := passingExecutor(manifest)
	result := Grade(t.Context(), manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), executor)
	if result.TerminalDomain != codingcontract.DomainResolved || result.FailureCode != nil ||
		result.RepairScoreMicros != codingcontract.ResolvedRepairScoreMicros || result.Evidence == nil {
		t.Fatalf("resolved result=%#v", result)
	}
	if result.ReplayedFinalTreeSHA256 != submission.FinalTreeSHA256 || result.ProtectedGraderTreeSHA256 == submission.FinalTreeSHA256 ||
		result.Evidence.GraderIntegrityBeforeSHA256 != result.Evidence.GraderIntegrityAfterSHA256 {
		t.Fatalf("resolved identities=%#v", result)
	}
	if err := result.Evidence.Validate(); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(executor.groups, executionOrder) {
		t.Fatalf("group order=%v", executor.groups)
	}
	if executor.preflightPlan != manifest.GraderPlanSHA256 ||
		result.Evidence.GraderPlanSHA256 != manifest.GraderPlanSHA256 ||
		result.Evidence.ResourceProfileSHA256 != manifest.ResourceProfileSHA256 {
		t.Fatalf("grader authority was not carried end to end: %#v", result.Evidence)
	}
	if len(result.ExecutionReceipts) != 1+len(executionOrder) ||
		result.ExecutionReceiptRootSHA256 != result.Evidence.ExecutionReceiptRootSHA256 {
		t.Fatalf("receipt evidence=%#v", result)
	}
	root := initialReceiptRoot
	for index, receipt := range result.ExecutionReceipts {
		if receipt.Sequence != uint32(index+1) || receipt.PreviousReceiptSHA256 != root {
			t.Fatalf("receipt chain broke at %d: %#v", index, receipt)
		}
		var err error
		root, err = digestCanonical(receipt)
		if err != nil {
			t.Fatal(err)
		}
	}
	if root != result.ExecutionReceiptRootSHA256 {
		t.Fatalf("receipt root=%s result=%s", root, result.ExecutionReceiptRootSHA256)
	}
}

func TestSharedGraderPlanResourceAndReceiptVectors(t *testing.T) {
	vectors := loadGraderWireVectors(t)
	var plan planProjection
	if err := json.Unmarshal(vectors.Plan, &plan); err != nil {
		t.Fatal(err)
	}
	var resource resourceProjection
	if err := json.Unmarshal(vectors.Resource, &resource); err != nil {
		t.Fatal(err)
	}
	planDigest, err := digestCanonical(plan)
	if err != nil || planDigest != vectors.Digests["grader_plan"] {
		t.Fatalf("shared plan digest=%s err=%v", planDigest, err)
	}
	resourceDigest, err := digestCanonical(resource)
	if err != nil || resourceDigest != vectors.Digests["grader_resource_profile"] ||
		resourceDigest != plan.ResourceProfileSHA256 {
		t.Fatalf("shared resource digest=%s err=%v", resourceDigest, err)
	}
	toLimits := func(value limitsProjection) codingrunner.Limits {
		return codingrunner.Limits{
			MaxBundleBytes: value.MaxBundleBytes, MaxWorkspaceBytes: value.MaxWorkspaceBytes,
			MaxFileBytes: value.MaxFileBytes, MaxPatchBytes: value.MaxPatchBytes, MaxEntries: value.MaxEntries,
			MaxToolCalls: value.MaxToolCalls, MaxReadBytes: value.MaxReadBytes,
			MaxResponseBytes: value.MaxResponseBytes, MaxSearchResults: value.MaxSearchResults,
			MaxReplayCacheBytes: value.MaxReplayCacheBytes, MaxTranscriptBytes: value.MaxTranscriptBytes,
		}
	}
	policy := ResourcePolicy{
		CandidateLimits: toLimits(resource.CandidateLimits), ProtectedLimits: toLimits(resource.ProtectedLimits),
		MaxCombinedDiskBytes: resource.MaxCombinedDiskBytes, MemoryLimitBytes: resource.MemoryLimitBytes,
		ScratchLimitBytes: resource.ScratchLimitBytes, PidsLimit: resource.PidsLimit, CPUQuotaMillis: resource.CPUQuotaMillis,
	}
	productionResourceDigest, err := ResourceProfileSHA256(policy)
	if err != nil || productionResourceDigest != resourceDigest {
		t.Fatalf("production resource digest=%s shared=%s err=%v", productionResourceDigest, resourceDigest, err)
	}
	toCommand := func(value commandProjection) codingrunner.CommandSpec {
		return codingrunner.CommandSpec{
			ID: value.ID, Argv: append([]string(nil), value.Argv...),
			Timeout: time.Duration(value.TimeoutMilliseconds) * time.Millisecond,
		}
	}
	groupsForManifest := make([]TestGroupSpec, len(plan.TestGroups))
	for index, group := range plan.TestGroups {
		groupsForManifest[index] = TestGroupSpec{
			Group: group.Group, Command: toCommand(group.Command), ExpectedTotal: group.ExpectedTotal,
		}
	}
	productionPlanDigest, err := GraderPlanSHA256(Manifest{
		CodingContractVersion: plan.CodingContractVersion, CaseID: plan.CaseID, VariantID: plan.VariantID,
		VisibleBundleSHA256: plan.VisibleBundleSHA256, BaseTreeSHA256: plan.BaseTreeSHA256,
		GraderContractSHA256: plan.GraderContractSHA256, GraderBundleSHA256: plan.GraderBundleSHA256,
		GraderImageDigest: plan.GraderImageDigest, GraderPlatform: plan.GraderPlatform,
		TestManifestSHA256: plan.TestManifestSHA256, ResourceProfileSHA256: plan.ResourceProfileSHA256,
		ExecutionTimeout: time.Duration(plan.ExecutionTimeoutMS) * time.Millisecond,
		Build:            BuildSpec{Required: plan.BuildRequired, Command: toCommand(plan.BuildCommand)}, TestGroups: groupsForManifest,
	})
	if err != nil || productionPlanDigest != planDigest {
		t.Fatalf("production plan digest=%s shared=%s err=%v", productionPlanDigest, planDigest, err)
	}

	groups := make(map[string]groupProjection, len(plan.TestGroups))
	for _, group := range plan.TestGroups {
		groups[group.Group] = group
	}
	type expectedReceipt struct {
		phase   string
		group   *string
		command commandProjection
		total   uint32
	}
	expected := make([]expectedReceipt, 0, 1+len(plan.ExecutionOrder))
	if plan.BuildRequired {
		expected = append(expected, expectedReceipt{phase: "build", command: plan.BuildCommand})
	}
	for _, name := range plan.ExecutionOrder {
		group := groups[name]
		groupName := name
		expected = append(expected, expectedReceipt{
			phase: "test", group: &groupName, command: group.Command, total: group.ExpectedTotal,
		})
	}
	if len(vectors.Receipts) != len(expected) {
		t.Fatalf("shared receipts=%d expected=%d", len(vectors.Receipts), len(expected))
	}
	previous := initialReceiptRoot
	executorID := ""
	for index, receipt := range vectors.Receipts {
		if executorID == "" {
			executorID = receipt.ExecutorInstanceID
		}
		want := expected[index]
		commandDigest, digestErr := digestCanonical(want.command)
		if digestErr != nil || receipt.Sequence != uint32(index+1) || receipt.Phase != want.phase ||
			!equalOptionalString(receipt.Group, want.group) || receipt.CommandID != want.command.ID ||
			receipt.Total != want.total || receipt.ExecutorInstanceID != executorID ||
			receipt.PreviousReceiptSHA256 != previous || receipt.CommandSHA256 != commandDigest {
			t.Fatalf("shared receipt %d is not plan-bound: %#v err=%v", index, receipt, digestErr)
		}
		previous, err = digestCanonical(receipt)
		if err != nil {
			t.Fatal(err)
		}
	}
	if previous != vectors.Digests["grader_execution_receipt_root"] {
		t.Fatalf("shared receipt root=%s", previous)
	}
}

func equalOptionalString(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func TestGradeFailsFastBeforePrivateGroups(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	for _, failedGroup := range []string{"fail_to_pass", "pass_to_pass"} {
		t.Run(failedGroup, func(t *testing.T) {
			executor := passingExecutor(manifest)
			group := manifest.TestGroups[slices.IndexFunc(manifest.TestGroups, func(value TestGroupSpec) bool {
				return value.Group == failedGroup
			})]
			executor.testRuns[failedGroup] = TestRun{
				ReturnCode: 1, Passed: 0, Total: group.ExpectedTotal, Completed: true,
			}
			result := Grade(
				t.Context(), manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), executor,
			)
			if result.TerminalDomain != codingcontract.DomainRepairFailure {
				t.Fatalf("result=%#v", result)
			}
			want := []string{"fail_to_pass"}
			if failedGroup == "pass_to_pass" {
				want = append(want, "pass_to_pass")
			}
			if !slices.Equal(executor.groups, want) || len(result.ExecutionReceipts) != 1+len(want) {
				t.Fatalf("executed=%v receipts=%#v", executor.groups, result.ExecutionReceipts)
			}
		})
	}
}

func TestGraderIdentityGoldenVector(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	result := Grade(t.Context(), manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), passingExecutor(manifest))
	if result.Evidence == nil {
		t.Fatalf("golden grade=%#v", result)
	}
	observed := map[string]string{
		"grader_contract_sha256":  manifest.GraderContractSHA256,
		"grader_plan_sha256":      manifest.GraderPlanSHA256,
		"resource_profile_sha256": manifest.ResourceProfileSHA256,
		"grader_bundle_sha256":    manifest.GraderBundleSHA256,
		"replayed_final_tree":     result.ReplayedFinalTreeSHA256,
		"protected_grader_tree":   result.ProtectedGraderTreeSHA256,
		"grader_integrity_before": result.Evidence.GraderIntegrityBeforeSHA256,
		"grader_integrity_after":  result.Evidence.GraderIntegrityAfterSHA256,
		"execution_receipt_root":  result.ExecutionReceiptRootSHA256,
	}
	want := map[string]string{
		"grader_contract_sha256":  "c002260a02a46d7187175e789c9b2b232f0d1072b214330347e136c466368055",
		"grader_plan_sha256":      "5979180d6c94528008015193290ded50589012cf8fa44b95439450717dcd7f0f",
		"resource_profile_sha256": "56557aa79689a061056b614f00146c32b6525f562fbbbf73ee212bbe6d33ef5e",
		"grader_bundle_sha256":    "6d621407b37aa6e0690ebd9a0d259213d1a3bf26ecdc23467db187b18aef0df0",
		"replayed_final_tree":     "2ee3794306b6198a0ad8b17cd497f5586e4a6d202a739e0ea3e34fcc599f193b",
		"protected_grader_tree":   "23ac8018d9d037b13de6cbe871899b3691f88acbcf5250ffb13928651a089367",
		"grader_integrity_before": "cf6ebef7f6fb98720d41ace45a9a3380f507cc737f64c5b9838da4fe52d7422f",
		"grader_integrity_after":  "cf6ebef7f6fb98720d41ace45a9a3380f507cc737f64c5b9838da4fe52d7422f",
		"execution_receipt_root":  "30de6e4f2c399c547f055cc1f58617a2ec3a2ef668055b7ae8c7badb9d90e7d7",
	}
	for key, expected := range want {
		if observed[key] != expected {
			t.Fatalf("grader golden identities changed: %#v", observed)
		}
	}
}

func TestGradeSeparatesRepairInfrastructureControlAndIntegrityFailures(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	baseManifest, graderBundle := graderFixture(t, submission, limits)
	tests := map[string]struct {
		mutateManifest   func(*Manifest)
		mutateSubmission func(*codingrunner.FrozenSubmission)
		executor         *fakeExecutor
		wantDomain       codingcontract.TerminalDomain
	}{
		"build failure": {
			executor:   configureExecutor(baseManifest, &fakeExecutor{buildRun: BuildRun{ReturnCode: 1, Completed: true}, requireFixture: true}),
			wantDomain: codingcontract.DomainRepairFailure,
		},
		"test failure": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.testRuns["hidden"] = TestRun{ReturnCode: 1, Passed: 1, Total: 3, Completed: true}
				return value
			}(),
			wantDomain: codingcontract.DomainRepairFailure,
		},
		"test timeout": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.testRuns["hidden"] = TestRun{ReturnCode: 124, Passed: 0, Total: 3, Completed: false, TimedOut: true}
				return value
			}(),
			wantDomain: codingcontract.DomainRepairFailure,
		},
		"executor failure": {
			executor:   configureExecutor(baseManifest, &fakeExecutor{buildErr: errors.New("sandbox unavailable"), requireFixture: true}),
			wantDomain: codingcontract.DomainValidatorInfrastructure,
		},
		"test count mismatch": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.testRuns["hidden"] = TestRun{ReturnCode: 0, Passed: 1, Total: 99, Completed: true}
				return value
			}(),
			wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"workspace mutation": {
			executor:   func() *fakeExecutor { value := passingExecutor(baseManifest); value.mutate = true; return value }(),
			wantDomain: codingcontract.DomainCandidateIntegrity,
		},
		"grader mutation": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.mutateProtected = true
				return value
			}(),
			wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"frozen submission mismatch": {
			mutateSubmission: func(value *codingrunner.FrozenSubmission) { value.FinalTreeSHA256 = strings.Repeat("a", 64) },
			executor:         passingExecutor(baseManifest), wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"selected visible bundle mismatch": {
			mutateSubmission: func(value *codingrunner.FrozenSubmission) { value.VisibleBundleSHA256 = strings.Repeat("a", 64) },
			executor:         passingExecutor(baseManifest), wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"selected base tree mismatch": {
			mutateSubmission: func(value *codingrunner.FrozenSubmission) { value.BaseTreeSHA256 = strings.Repeat("b", 64) },
			executor:         passingExecutor(baseManifest), wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"grader bundle mismatch": {
			mutateManifest: func(value *Manifest) { value.GraderBundleSHA256 = strings.Repeat("b", 64) },
			executor:       passingExecutor(baseManifest), wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"executor attestation mismatch": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.attestation.GraderImageDigest = "sha256:" + strings.Repeat("9", 64)
				return value
			}(),
			wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
		"executor receipt mismatch": {
			executor: func() *fakeExecutor {
				value := passingExecutor(baseManifest)
				value.corruptReceipt = true
				return value
			}(),
			wantDomain: codingcontract.DomainControlPlaneIntegrity,
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			manifest := baseManifest
			manifest.TestGroups = append([]TestGroupSpec(nil), baseManifest.TestGroups...)
			copySubmission := submission
			copySubmission.Changes = append([]codingrunner.FrozenChange(nil), submission.Changes...)
			copySubmission.ChangedPaths = append([]string(nil), submission.ChangedPaths...)
			copySubmission.Patch = append([]byte(nil), submission.Patch...)
			if test.mutateManifest != nil {
				test.mutateManifest(&manifest)
			}
			if test.mutateSubmission != nil {
				test.mutateSubmission(&copySubmission)
			}
			result := Grade(t.Context(), manifest, copySubmission, bytes.NewReader(visible), bytes.NewReader(graderBundle), test.executor)
			if result.TerminalDomain != test.wantDomain || result.RepairScoreMicros != 0 || result.FailureCode == nil {
				t.Fatalf("result=%#v want=%s", result, test.wantDomain)
			}
		})
	}
}

func TestGraderManifestRejectsNoncanonicalAuthority(t *testing.T) {
	submission, _, limits := frozenFixture(t)
	base, _ := graderFixture(t, submission, limits)
	tests := map[string]func(*Manifest){
		"wrong contract":         func(value *Manifest) { value.CodingContractVersion = 2 },
		"expired":                func(value *Manifest) { value.Deadline = time.Now().Add(-time.Second) },
		"far deadline":           func(value *Manifest) { value.Deadline = time.Now().Add(3 * time.Hour) },
		"zero execution timeout": func(value *Manifest) { value.ExecutionTimeout = 0 },
		"sub-millisecond execution timeout": func(value *Manifest) {
			value.ExecutionTimeout = time.Second + time.Nanosecond
		},
		"bad contract digest": func(value *Manifest) {
			value.GraderContractSHA256 = "bad"
		},
		"bad image":           func(value *Manifest) { value.GraderImageDigest = "latest" },
		"bad grader platform": func(value *Manifest) { value.GraderPlatform = "linux/arm64" },
		"test manifest drift": func(value *Manifest) {
			value.TestManifestSHA256 = strings.Repeat("8", 64)
		},
		"missing group": func(value *Manifest) { value.TestGroups = value.TestGroups[:4] },
		"unsorted groups": func(value *Manifest) {
			value.TestGroups[0], value.TestGroups[1] = value.TestGroups[1], value.TestGroups[0]
		},
		"zero total": func(value *Manifest) { value.TestGroups[0].ExpectedTotal = 0 },
		"duplicate command": func(value *Manifest) {
			value.TestGroups[0].Command.ID = value.Build.Command.ID
		},
		"shell command": func(value *Manifest) { value.TestGroups[0].Command.Argv = []string{"sh", "-c", "pytest"} },
		"valid command drift": func(value *Manifest) {
			value.TestGroups[0].Command.Argv = []string{"python", "-m", "unittest"}
		},
		"expected count drift": func(value *Manifest) { value.TestGroups[0].ExpectedTotal++ },
		"resource drift":       func(value *Manifest) { value.ResourcePolicy.MemoryLimitBytes++ },
		"invalid limits":       func(value *Manifest) { value.ResourcePolicy.CandidateLimits.MaxPatchBytes = 0 },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			manifest := cloneManifest(base)
			mutate(&manifest)
			if err := manifest.validate(time.Now()); err == nil {
				t.Fatal("invalid grader manifest was accepted")
			}
		})
	}
}

func TestResourcePolicyRejectsCombinedPeakAboveCeiling(t *testing.T) {
	submission, _, limits := frozenFixture(t)
	manifest, _ := graderFixture(t, submission, limits)
	policy := manifest.ResourcePolicy
	peak := policy.CandidateLimits.MaxWorkspaceBytes + policy.ProtectedLimits.MaxWorkspaceBytes +
		max(policy.CandidateLimits.MaxBundleBytes, policy.ProtectedLimits.MaxBundleBytes) + int64(policy.ScratchLimitBytes)
	policy.MaxCombinedDiskBytes = peak - 1
	if err := policy.validate(); err == nil {
		t.Fatal("resource policy accepted a combined peak above its disk ceiling")
	}
}

func TestGradeCannotResolveAfterSignedDeadline(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	manifest.ExecutionTimeout = 50 * time.Millisecond
	rebindPlan(t, &manifest)
	result := Grade(
		t.Context(), manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle),
		lateExecutor{attestation: attestationFor(manifest)},
	)
	if result.TerminalDomain != codingcontract.DomainRepairFailure || result.FailureCode == nil || *result.FailureCode != "grader_deadline" {
		t.Fatalf("late grade result=%#v", result)
	}
}

func TestParentDeadlineDuringExecutionIsValidatorInfrastructure(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	parent, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	result := Grade(
		parent, manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle),
		lateExecutor{attestation: attestationFor(manifest)},
	)
	if result.TerminalDomain != codingcontract.DomainValidatorInfrastructure || result.FailureCode == nil ||
		*result.FailureCode != "grader_parent_deadline" {
		t.Fatalf("parent-deadline grade result=%#v", result)
	}
}

func TestSetupTimeDoesNotConsumeExecutionTimeout(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	manifest.ExecutionTimeout = 50 * time.Millisecond
	rebindPlan(t, &manifest)
	result := Grade(
		t.Context(), manifest, submission,
		&slowReader{source: bytes.NewReader(visible), delay: 100 * time.Millisecond},
		bytes.NewReader(graderBundle), passingExecutor(manifest),
	)
	if result.TerminalDomain != codingcontract.DomainResolved {
		t.Fatalf("setup consumed execution budget: %#v", result)
	}
}

func TestSetupDeadlineIsValidatorInfrastructure(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	manifest.Deadline = time.Now().Add(50 * time.Millisecond)
	result := Grade(
		t.Context(), manifest, submission,
		&slowReader{source: bytes.NewReader(visible), delay: 100 * time.Millisecond},
		bytes.NewReader(graderBundle), passingExecutor(manifest),
	)
	if result.TerminalDomain != codingcontract.DomainValidatorInfrastructure || result.FailureCode == nil ||
		*result.FailureCode != "grader_setup_deadline" {
		t.Fatalf("setup deadline result=%#v", result)
	}
}

func TestGradeRejectsInvalidManifestAndMissingExecutorBeforeMaterialization(t *testing.T) {
	submission, visible, limits := frozenFixture(t)
	manifest, graderBundle := graderFixture(t, submission, limits)
	invalid := manifest
	invalid.TestGroups = invalid.TestGroups[:4]
	if result := Grade(t.Context(), invalid, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), passingExecutor(manifest)); result.TerminalDomain != codingcontract.DomainControlPlaneIntegrity {
		t.Fatalf("invalid manifest result=%#v", result)
	}
	if result := Grade(t.Context(), manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), nil); result.TerminalDomain != codingcontract.DomainValidatorInfrastructure {
		t.Fatalf("missing executor result=%#v", result)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	result := Grade(cancelled, manifest, submission, bytes.NewReader(visible), bytes.NewReader(graderBundle), passingExecutor(manifest))
	if result.TerminalDomain != codingcontract.DomainValidatorInfrastructure || result.FailureCode == nil || *result.FailureCode != "grader_cancelled" {
		t.Fatalf("cancelled grade result=%#v", result)
	}
}
