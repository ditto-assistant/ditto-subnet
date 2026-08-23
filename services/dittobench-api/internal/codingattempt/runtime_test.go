package codingattempt

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
)

type artifactKey struct {
	phase codingartifacts.DeliveryPhase
	kind  codingartifacts.Kind
}

type artifactSourceFunc func(context.Context, codingartifacts.Capability) (io.ReadCloser, error)

func (function artifactSourceFunc) Open(
	ctx context.Context,
	capability codingartifacts.Capability,
) (io.ReadCloser, error) {
	return function(ctx, capability)
}

type seedProjectorFunc func(io.Reader, codingseed.Binding) (codingseed.Projection, error)

func (function seedProjectorFunc) Project(
	reader io.Reader,
	binding codingseed.Binding,
) (codingseed.Projection, error) {
	return function(reader, binding)
}

type revokerFunc func(context.Context) error

func (function revokerFunc) Revoke(ctx context.Context) error {
	return function(ctx)
}

type trackingReadCloser struct {
	io.Reader
	closed   bool
	closeErr error
}

func (reader *trackingReadCloser) Close() error {
	reader.closed = true
	return reader.closeErr
}

type fixtureSource struct {
	mu     sync.Mutex
	values map[artifactKey][]byte
	errors map[artifactKey]error
	calls  []artifactKey
}

func (source *fixtureSource) Open(_ context.Context, capability codingartifacts.Capability) (io.ReadCloser, error) {
	source.mu.Lock()
	defer source.mu.Unlock()
	key := artifactKey{phase: capability.Phase, kind: capability.Kind}
	if source.errors != nil && source.errors[key] != nil {
		return nil, source.errors[key]
	}
	value, ok := source.values[key]
	if !ok {
		return nil, errors.New("fixture artifact unavailable")
	}
	source.calls = append(source.calls, key)
	return io.NopCloser(bytes.NewReader(append([]byte(nil), value...))), nil
}

func (source *fixtureSource) reset() {
	source.mu.Lock()
	defer source.mu.Unlock()
	source.calls = nil
}

type fixtureRevoker struct {
	calls      int
	err        error
	contextErr error
}

func (revoker *fixtureRevoker) Revoke(ctx context.Context) error {
	revoker.calls++
	revoker.contextErr = ctx.Err()
	return revoker.err
}

type fixtureExecutor struct {
	manifest codinggrader.Manifest
}

func (executor *fixtureExecutor) Execute(context.Context, string, codingrunner.CommandSpec) (codingrunner.CommandResult, error) {
	return codingrunner.CommandResult{}, nil
}

func (executor *fixtureExecutor) Preflight(_ context.Context, expectedPlanSHA256 string) (codinggrader.ExecutorAttestation, error) {
	if expectedPlanSHA256 != executor.manifest.GraderPlanSHA256 {
		return codinggrader.ExecutorAttestation{}, errors.New("plan mismatch")
	}
	return codinggrader.ExecutorAttestation{
		ExecutorInstanceID: "attempt-executor-001", GraderImageDigest: executor.manifest.GraderImageDigest,
		GraderPlatform:        executor.manifest.GraderPlatform,
		GraderContractSHA256:  executor.manifest.GraderContractSHA256,
		GraderPlanSHA256:      executor.manifest.GraderPlanSHA256,
		ResourceProfileSHA256: executor.manifest.ResourceProfileSHA256,
		NetworkDisabled:       true, CandidateMountReadOnly: true,
		ProtectedMountHidden: true, ProcessGroupsIsolated: true,
	}, nil
}

func (executor *fixtureExecutor) Build(_ context.Context, _ string, command codingrunner.CommandSpec) (codinggrader.BuildRun, error) {
	digest, _ := codinggrader.CommandSHA256(command.ID, command.Argv, command.Timeout.Milliseconds())
	return codinggrader.BuildRun{
		CommandID: command.ID, CommandSHA256: digest, ExecutorInstanceID: "attempt-executor-001",
		ReturnCode: 0, Completed: true,
	}, nil
}

func (executor *fixtureExecutor) Test(
	_ context.Context,
	workspace string,
	protected string,
	group codinggrader.TestGroupSpec,
) (codinggrader.TestRun, error) {
	if _, err := os.Stat(filepath.Join(protected, ".dittobench-grader", "hidden_test.py")); err != nil {
		return codinggrader.TestRun{}, errors.New("protected grader missing")
	}
	if _, err := os.Stat(filepath.Join(workspace, ".dittobench-grader", "hidden_test.py")); !os.IsNotExist(err) {
		return codinggrader.TestRun{}, errors.New("grader visible in candidate workspace")
	}
	digest, _ := codinggrader.CommandSHA256(
		group.Command.ID, group.Command.Argv, group.Command.Timeout.Milliseconds(),
	)
	return codinggrader.TestRun{
		CommandID: group.Command.ID, CommandSHA256: digest, ExecutorInstanceID: "attempt-executor-001",
		ReturnCode: 0, Passed: group.ExpectedTotal, Total: group.ExpectedTotal, Completed: true,
	}, nil
}

type fixture struct {
	now            time.Time
	binding        Binding
	visible        []byte
	memory         []byte
	resource       []byte
	grader         []byte
	policy         codinggrader.ResourcePolicy
	runnerManifest codingrunner.Manifest
	graderManifest codinggrader.Manifest
	authoringSpec  AuthoringSpec
	gradingSpec    GradingSpec
	source         *fixtureSource
	executor       *fixtureExecutor
	projector      *codingseed.Projector
	runtime        *Runtime
	resourceSHA256 string
	memorySHA256   string
	visibleSHA256  string
	graderSHA256   string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	now := time.Now().UTC().Truncate(time.Second)
	deadline := now.Add(time.Hour)
	visible := tarBytes(t, map[string]string{
		"src/app.py":            "def normalize(value):\n    return value.strip()\n",
		"tests/test_visible.py": "def test_visible():\n    assert True\n",
	})
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(visible), limits)
	if err != nil {
		t.Fatal(err)
	}
	protectedLimits := codingrunner.DefaultLimits()
	protectedLimits.MaxBundleBytes = 8 << 20
	protectedLimits.MaxWorkspaceBytes = 16 << 20
	protectedLimits.MaxFileBytes = 4 << 20
	protectedLimits.MaxPatchBytes = 4 << 20
	policy := codinggrader.ResourcePolicy{
		CandidateLimits: limits, ProtectedLimits: protectedLimits,
		MaxCombinedDiskBytes: limits.MaxWorkspaceBytes + protectedLimits.MaxWorkspaceBytes + limits.MaxBundleBytes + 1<<30,
		MemoryLimitBytes:     4 << 30, ScratchLimitBytes: 1 << 30,
		PidsLimit: 512, CPUQuotaMillis: 2_000,
	}
	resourceSHA, err := codinggrader.ResourceProfileSHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	resource := resourceBytes(t, policy)
	repository, validFrom := "repository-attempt-001", "repository-epoch-1"
	memory := memoryBytes(t, []codingcontract.VisibleMemory{{
		MemoryID: "memory-attempt-001", RepositoryCapabilityID: &repository,
		FactGroupID: nil, Scope: "repository", Type: "project_experience",
		Content:        "Preserve incomplete parser input between calls.",
		ValidFromEpoch: &validFrom, ValidUntilEpoch: nil, Supersedes: []string{}, ConfidenceMicros: 900_000,
	}})
	grader := tarBytes(t, map[string]string{
		".dittobench-grader/hidden_test.py": "assert True\n",
		".dittobench-grader/runner.json":    "{}\n",
	})
	binding := Binding{
		TicketID: "33333333-3333-4333-8333-333333333333", CaseID: "case-attempt-001",
		ProfileCapabilityID: "profile-attempt-001", Deadline: deadline,
	}
	runnerManifest := codingrunner.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		TicketID:              binding.TicketID, CaseID: binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		Deadline: deadline, EditablePaths: []string{"src/app.py"},
		CreatablePaths: []string{}, DeletablePaths: []string{},
		TestCommands: []codingrunner.CommandSpec{}, BuildCommands: []codingrunner.CommandSpec{}, Limits: limits,
	}
	groups := []string{"adversarial", "fail_to_pass", "hidden", "integrity", "pass_to_pass"}
	testGroups := make([]codinggrader.TestGroupSpec, len(groups))
	for index, group := range groups {
		testGroups[index] = codinggrader.TestGroupSpec{
			Group: group,
			Command: codingrunner.CommandSpec{
				ID: "test-" + group, Argv: []string{"python", "-m", "pytest"}, Timeout: time.Minute,
			},
			ExpectedTotal: uint32(index + 1),
		}
	}
	graderManifest := codinggrader.Manifest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                binding.CaseID, VariantID: "variant-v1",
		VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256,
		GraderContractSHA256: codinggrader.GraderContractSHA256(),
		GraderBundleSHA256:   digest(grader), GraderImageDigest: "sha256:" + strings.Repeat("2", 64),
		GraderPlatform: "linux/amd64", TestManifestSHA256: strings.Repeat("3", 64),
		ResourceProfileSHA256: resourceSHA, Deadline: deadline, ExecutionTimeout: 30 * time.Minute,
		ResourcePolicy: policy,
		Build: codinggrader.BuildSpec{Required: false, Command: codingrunner.CommandSpec{
			ID: "build-python", Argv: []string{"python", "-m", "compileall", "src"}, Timeout: time.Minute,
		}},
		TestGroups: testGroups,
	}
	graderManifest.GraderPlanSHA256, err = codinggrader.GraderPlanSHA256(graderManifest)
	if err != nil {
		t.Fatal(err)
	}
	visibleSHA := identity.VisibleBundleSHA256
	memorySHA := digest(memory)
	graderSHA := digest(grader)
	source := &fixtureSource{values: map[artifactKey][]byte{
		{codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile}: resource,
		{codingartifacts.PhaseAuthoring, codingartifacts.KindVisibleBundle}:   visible,
		{codingartifacts.PhaseAuthoring, codingartifacts.KindMemoryBundle}:    memory,
		{codingartifacts.PhaseGrading, codingartifacts.KindResourceProfile}:   resource,
		{codingartifacts.PhaseGrading, codingartifacts.KindVisibleBundle}:     visible,
		{codingartifacts.PhaseGrading, codingartifacts.KindGraderBundle}:      grader,
	}}
	executor := &fixtureExecutor{manifest: graderManifest}
	projector, err := codingseed.New(codingseed.Config{
		MaxBundleBytes: codingcontract.MaxCanonicalJSONBytes,
		SeedTimeout:    time.Second, Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	runtime, err := NewRuntime(RuntimeConfig{
		Artifacts: source, Executor: executor, SeedProjector: projector, Now: func() time.Time { return now },
	})
	if err != nil {
		t.Fatal(err)
	}
	value := &fixture{
		now: now, binding: binding, visible: visible, memory: memory, resource: resource, grader: grader,
		policy: policy, runnerManifest: runnerManifest, graderManifest: graderManifest,
		source: source, executor: executor, projector: projector, runtime: runtime,
		resourceSHA256: resourceSHA, memorySHA256: memorySHA,
		visibleSHA256: visibleSHA, graderSHA256: graderSHA,
	}
	value.authoringSpec = AuthoringSpec{
		Binding: binding,
		VisibleBundle: capability(binding, codingartifacts.PhaseAuthoring, codingartifacts.KindVisibleBundle,
			codingartifacts.AudienceWorkspaceMaterializer, visibleSHA, len(visible)),
		MemoryBundle: capability(binding, codingartifacts.PhaseAuthoring, codingartifacts.KindMemoryBundle,
			codingartifacts.AudienceMemorySeedProjector, memorySHA, len(memory)),
		ResourceProfile: capability(binding, codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile,
			codingartifacts.AudienceResourceSupervisor, resourceSHA, len(resource)),
		RunnerManifest: runnerManifest, CandidateLimits: policy.CandidateLimits,
		MemoryBundleSHA256: memorySHA, ResourceProfileSHA256: resourceSHA,
	}
	value.gradingSpec = GradingSpec{
		Binding: binding, FreezeID: "44444444-4444-4444-8444-444444444444", AuthoringEvidenceSHA256: strings.Repeat("4", 64),
		VisibleBundle: capability(binding, codingartifacts.PhaseGrading, codingartifacts.KindVisibleBundle,
			codingartifacts.AudienceWorkspaceMaterializer, visibleSHA, len(visible)),
		ResourceProfile: capability(binding, codingartifacts.PhaseGrading, codingartifacts.KindResourceProfile,
			codingartifacts.AudienceResourceSupervisor, resourceSHA, len(resource)),
		GraderBundle: capability(binding, codingartifacts.PhaseGrading, codingartifacts.KindGraderBundle,
			codingartifacts.AudienceProtectedGrader, graderSHA, len(grader)),
		GraderManifest: graderManifest,
	}
	return value
}

func TestBeginAuthoringConsumesOnlyAuthoringArtifactsAndFreezesAfterRevoke(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	seed := session.SeedProjection().Request()
	if seed.MemoryBundleSHA256 != fixture.memorySHA256 || len(seed.Memories) != 1 {
		t.Fatalf("seed=%#v", seed)
	}
	seed.Memories[0].Content = "mutated"
	if session.SeedProjection().Request().Memories[0].Content == "mutated" {
		t.Fatal("session seed projection was mutable")
	}
	callTool(t, session.Handler(), codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                fixture.binding.CaseID, ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
		CallID: "read-authoring", Name: "repo.read_file", Arguments: json.RawMessage(`{"path":"src/app.py"}`),
	})
	if _, err := session.WriteTranscript(io.Discard); err == nil {
		t.Fatal("unfrozen transcript was readable")
	}
	revoker := &fixtureRevoker{}
	frozen, err := session.Freeze(t.Context(), revoker)
	if err != nil || frozen.Submission == nil || revoker.calls != 1 {
		t.Fatalf("frozen=%#v revoke_calls=%d err=%v", frozen, revoker.calls, err)
	}
	if retained := session.SeedProjection().Request(); retained.CaseID != "" || retained.Memories != nil {
		t.Fatalf("frozen session retained seed projection: %#v", retained)
	}
	var transcript bytes.Buffer
	identity, err := session.WriteTranscript(&transcript)
	if err != nil || identity.Events != 1 || identity.SizeBytes != int64(transcript.Len()) {
		t.Fatalf("identity=%#v bytes=%d err=%v", identity, transcript.Len(), err)
	}
	again, err := session.Freeze(t.Context(), revoker)
	if err != nil || again.Submission == frozen.Submission || revoker.calls != 1 {
		t.Fatalf("cached=%#v revoke_calls=%d err=%v", again, revoker.calls, err)
	}
	again.Submission.ChangedPaths = nil
	third, err := session.Freeze(t.Context(), revoker)
	if err != nil || third.Submission == nil || third.Submission.ChangedPaths == nil {
		t.Fatalf("cached freeze was mutable: %#v err=%v", third, err)
	}
	if _, err := session.WriteTranscript(nil); err == nil {
		t.Fatal("nil transcript destination accepted")
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	want := []artifactKey{
		{codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile},
		{codingartifacts.PhaseAuthoring, codingartifacts.KindVisibleBundle},
		{codingartifacts.PhaseAuthoring, codingartifacts.KindMemoryBundle},
	}
	if !equalKeys(fixture.source.calls, want) {
		t.Fatalf("artifact calls=%v want=%v", fixture.source.calls, want)
	}
}

func TestCloseBeforeFreezeFailsClosed(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Close(); !errors.Is(err, ErrClosedBeforeFreeze) {
		t.Fatalf("close error=%v", err)
	}
	if err := session.Close(); !errors.Is(err, ErrClosedBeforeFreeze) {
		t.Fatalf("repeated close changed error=%v", err)
	}
	if _, err := session.Freeze(t.Context(), &fixtureRevoker{}); err == nil {
		t.Fatal("closed authoring session froze")
	}
}

func TestAuthoringSessionDiagnosticsAndJSONAreRedacted(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := json.Marshal(session); err == nil {
		t.Fatal("authoring session serialized")
	}
	rendered := fmt.Sprintf("%#v", session)
	if strings.Contains(rendered, "Preserve incomplete parser") || strings.Contains(rendered, os.TempDir()) {
		t.Fatalf("authoring session diagnostics leaked private state: %s", rendered)
	}
	if err := session.Close(); !errors.Is(err, ErrClosedBeforeFreeze) {
		t.Fatalf("close error=%v", err)
	}
}

func TestRevokeFailureStillFreezesAndNeverOpensGrader(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	revoker := &fixtureRevoker{err: errors.New("synthetic revoke failure")}
	frozen, err := session.Freeze(t.Context(), revoker)
	if err == nil || frozen.Submission == nil {
		t.Fatalf("freeze=%#v err=%v", frozen, err)
	}
	for _, call := range fixture.source.calls {
		if call.kind == codingartifacts.KindGraderBundle {
			t.Fatal("grader opened after failed revocation")
		}
	}
	_ = session.Close()
}

func TestParentCancellationCannotSkipOuterRevocation(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	revoker := &fixtureRevoker{}
	frozen, err := session.Freeze(cancelled, revoker)
	if err != nil || frozen.Submission == nil || revoker.calls != 1 || revoker.contextErr != nil {
		t.Fatalf("freeze=%#v revoker=%#v err=%v", frozen, revoker, err)
	}
	_ = session.Close()
}

func TestFreezeDoesNotHoldLifecycleLockAcrossRevoker(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var frozen codingrunner.FreezeResult
	var freezeErr error
	go func() {
		frozen, freezeErr = session.Freeze(t.Context(), revokerFunc(func(context.Context) error {
			_ = session.String()
			_ = session.LogValue()
			return nil
		}))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("re-entrant lifecycle diagnostics deadlocked freeze")
	}
	if freezeErr != nil || frozen.Submission == nil {
		t.Fatalf("freeze=%#v err=%v", frozen, freezeErr)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestCloseCannotDestroySessionDuringFreeze(t *testing.T) {
	fixture := newFixture(t)
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	started := make(chan struct{})
	release := make(chan struct{})
	done := make(chan struct{})
	var frozen codingrunner.FreezeResult
	var freezeErr error
	go func() {
		frozen, freezeErr = session.Freeze(t.Context(), revokerFunc(func(context.Context) error {
			close(started)
			<-release
			return nil
		}))
		close(done)
	}()
	<-started
	if err := session.Close(); !errors.Is(err, ErrFreezeInProgress) {
		t.Fatalf("close during freeze error=%v", err)
	}
	if _, err := session.Freeze(t.Context(), &fixtureRevoker{}); !errors.Is(err, ErrFreezeInProgress) {
		t.Fatalf("concurrent freeze error=%v", err)
	}
	close(release)
	<-done
	if freezeErr != nil || frozen.Submission == nil {
		t.Fatalf("freeze=%#v err=%v", frozen, freezeErr)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestGradeUsesFreshVisibleAndProtectedArtifactsWithoutMemory(t *testing.T) {
	fixture := newFixture(t)
	frozen := freezeFixture(t, fixture)
	fixture.source.reset()
	fixture.gradingSpec.FrozenPatchSHA256 = frozen.FrozenPatchSHA256
	fixture.gradingSpec.FrozenSubmissionKey = "sha256/" + frozen.FrozenPatchSHA256
	replayed, replayErr := codingrunner.ReplayFrozenSubmission(
		t.Context(), frozen, bytes.NewReader(fixture.visible), fixture.policy.CandidateLimits,
	)
	if replayErr != nil {
		t.Fatalf("fixture frozen replay failed: %v", replayErr)
	}
	_ = replayed.Close()
	result, err := fixture.runtime.Grade(t.Context(), fixture.gradingSpec, frozen)
	if err != nil || result.TerminalDomain != codingcontract.DomainResolved || result.Evidence == nil {
		failureCode := ""
		if result.FailureCode != nil {
			failureCode = *result.FailureCode
		}
		t.Fatalf("result=%#v failure_code=%q err=%v", result, failureCode, err)
	}
	want := []artifactKey{
		{codingartifacts.PhaseGrading, codingartifacts.KindResourceProfile},
		{codingartifacts.PhaseGrading, codingartifacts.KindVisibleBundle},
		{codingartifacts.PhaseGrading, codingartifacts.KindGraderBundle},
	}
	if !equalKeys(fixture.source.calls, want) {
		t.Fatalf("artifact calls=%v want=%v", fixture.source.calls, want)
	}
}

func TestProtectedGraderIsOpenedOnlyAfterSuccessfulFrozenReplay(t *testing.T) {
	fixture := newFixture(t)
	frozen := freezeFixture(t, fixture)
	fixture.gradingSpec.FrozenPatchSHA256 = frozen.FrozenPatchSHA256
	fixture.gradingSpec.FrozenSubmissionKey = "sha256/" + frozen.FrozenPatchSHA256
	frozen.FinalTreeSHA256 = strings.Repeat("f", 64)
	fixture.source.reset()
	result, err := fixture.runtime.Grade(t.Context(), fixture.gradingSpec, frozen)
	if err != nil || result.TerminalDomain != codingcontract.DomainControlPlaneIntegrity ||
		result.FailureCode == nil || *result.FailureCode != "frozen_replay_invalid" {
		t.Fatalf("result=%#v err=%v", result, err)
	}
	want := []artifactKey{
		{codingartifacts.PhaseGrading, codingartifacts.KindResourceProfile},
		{codingartifacts.PhaseGrading, codingartifacts.KindVisibleBundle},
	}
	if !equalKeys(fixture.source.calls, want) {
		t.Fatalf("protected grader opened before replay: %v", fixture.source.calls)
	}
}

func TestProtectedGraderOpenUsesLeaseContextAndClosesBrokenReader(t *testing.T) {
	fixture := newFixture(t)
	frozen := freezeFixture(t, fixture)
	fixture.gradingSpec.FrozenPatchSHA256 = frozen.FrozenPatchSHA256
	fixture.gradingSpec.FrozenSubmissionKey = "sha256/" + frozen.FrozenPatchSHA256
	broken := &trackingReadCloser{
		Reader: bytes.NewReader(fixture.grader),
	}
	var openerDeadline time.Time
	source := artifactSourceFunc(func(
		ctx context.Context,
		capability codingartifacts.Capability,
	) (io.ReadCloser, error) {
		if capability.Kind == codingartifacts.KindGraderBundle {
			openerDeadline, _ = ctx.Deadline()
			return broken, codingartifacts.ErrArtifactUnavailable
		}
		return fixture.source.Open(ctx, capability)
	})
	runtime, err := NewRuntime(RuntimeConfig{Artifacts: source, Executor: fixture.executor, SeedProjector: fixture.projector, Now: func() time.Time {
		return fixture.now
	}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := runtime.Grade(t.Context(), fixture.gradingSpec, frozen)
	if err != nil || result.TerminalDomain != codingcontract.DomainValidatorInfrastructure ||
		result.FailureCode == nil || *result.FailureCode != "grader_bundle_unavailable" {
		t.Fatalf("result=%#v err=%v", result, err)
	}
	if !openerDeadline.Equal(fixture.binding.Deadline) {
		t.Fatalf("opener deadline=%v want=%v", openerDeadline, fixture.binding.Deadline)
	}
	if !broken.closed {
		t.Fatal("reader returned with an open error was not closed")
	}
}

func TestProtectedGraderCloseFailureOverridesResolvedResult(t *testing.T) {
	fixture := newFixture(t)
	frozen := freezeFixture(t, fixture)
	fixture.gradingSpec.FrozenPatchSHA256 = frozen.FrozenPatchSHA256
	fixture.gradingSpec.FrozenSubmissionKey = "sha256/" + frozen.FrozenPatchSHA256
	broken := &trackingReadCloser{
		Reader:   bytes.NewReader(fixture.grader),
		closeErr: errors.New("synthetic protected reader cleanup failure"),
	}
	source := artifactSourceFunc(func(
		ctx context.Context,
		capability codingartifacts.Capability,
	) (io.ReadCloser, error) {
		if capability.Kind == codingartifacts.KindGraderBundle {
			return broken, nil
		}
		return fixture.source.Open(ctx, capability)
	})
	runtime, err := NewRuntime(RuntimeConfig{Artifacts: source, Executor: fixture.executor, SeedProjector: fixture.projector, Now: func() time.Time {
		return fixture.now
	}})
	if err != nil {
		t.Fatal(err)
	}
	result, err := runtime.Grade(t.Context(), fixture.gradingSpec, frozen)
	if err != nil || result.TerminalDomain != codingcontract.DomainValidatorInfrastructure ||
		result.FailureCode == nil || *result.FailureCode != "grader_cleanup" {
		t.Fatalf("result=%#v err=%v", result, err)
	}
	if !broken.closed {
		t.Fatal("protected grader reader was not closed")
	}
}

func TestPhaseAndDigestDriftFailBeforeArtifactOpen(t *testing.T) {
	for name, mutate := range map[string]func(*AuthoringSpec){
		"memory phase":    func(spec *AuthoringSpec) { spec.MemoryBundle.Phase = codingartifacts.PhaseGrading },
		"memory digest":   func(spec *AuthoringSpec) { spec.MemoryBundleSHA256 = strings.Repeat("f", 64) },
		"resource digest": func(spec *AuthoringSpec) { spec.ResourceProfile.SHA256 = strings.Repeat("f", 64) },
		"candidate limits": func(spec *AuthoringSpec) {
			spec.CandidateLimits.MaxToolCalls--
		},
		"deadline": func(spec *AuthoringSpec) { spec.Binding.Deadline = spec.Binding.Deadline.Add(time.Second) },
		"expired capability": func(spec *AuthoringSpec) {
			spec.MemoryBundle.ExpiresAt = spec.Binding.Deadline.Add(-time.Hour - time.Second)
		},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newFixture(t)
			spec := fixture.authoringSpec
			mutate(&spec)
			if _, err := fixture.runtime.BeginAuthoring(t.Context(), spec); err == nil {
				t.Fatal("drifted authoring spec accepted")
			}
			if len(fixture.source.calls) != 0 {
				t.Fatalf("artifacts opened before validation: %v", fixture.source.calls)
			}
		})
	}
}

func TestResourceProfileTamperingStopsBeforeVisibleOrMemory(t *testing.T) {
	fixture := newFixture(t)
	key := artifactKey{codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile}
	fixture.source.values[key] = bytes.Replace(fixture.resource, []byte(`"pids_limit":512`), []byte(`"pids_limit":513`), 1)
	if _, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec); err == nil {
		t.Fatal("tampered resource profile accepted")
	}
	if len(fixture.source.calls) != 1 || fixture.source.calls[0] != key {
		t.Fatalf("unexpected artifact calls: %v", fixture.source.calls)
	}
}

func TestMemoryProjectionFailureClosesRawReader(t *testing.T) {
	fixture := newFixture(t)
	broken := &trackingReadCloser{Reader: strings.NewReader(`{"memories":null}`)}
	source := artifactSourceFunc(func(
		ctx context.Context,
		capability codingartifacts.Capability,
	) (io.ReadCloser, error) {
		if capability.Kind == codingartifacts.KindMemoryBundle {
			return broken, nil
		}
		return fixture.source.Open(ctx, capability)
	})
	runtime, err := NewRuntime(RuntimeConfig{
		Artifacts: source, Executor: fixture.executor, SeedProjector: fixture.projector,
		Now: func() time.Time { return fixture.now },
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := runtime.BeginAuthoring(t.Context(), fixture.authoringSpec); err == nil {
		t.Fatal("invalid memory artifact was projected")
	}
	if !broken.closed {
		t.Fatal("invalid memory artifact reader was not closed")
	}
}

func TestBeginAuthoringRejectsUnboundProjectionAndClosesRawReader(t *testing.T) {
	fixture := newFixture(t)
	memory := &trackingReadCloser{Reader: bytes.NewReader(fixture.memory)}
	source := artifactSourceFunc(func(
		ctx context.Context,
		capability codingartifacts.Capability,
	) (io.ReadCloser, error) {
		if capability.Kind == codingartifacts.KindMemoryBundle {
			return memory, nil
		}
		return fixture.source.Open(ctx, capability)
	})
	runtime, err := NewRuntime(RuntimeConfig{
		Artifacts: source, Executor: fixture.executor,
		SeedProjector: seedProjectorFunc(func(
			io.Reader,
			codingseed.Binding,
		) (codingseed.Projection, error) {
			return codingseed.Projection{}, nil
		}),
		Now: func() time.Time { return fixture.now },
	})
	if err != nil {
		t.Fatal(err)
	}
	if session, err := runtime.BeginAuthoring(t.Context(), fixture.authoringSpec); err == nil || session != nil {
		t.Fatalf("unbound projection accepted: session=%#v err=%v", session, err)
	}
	if !memory.closed {
		t.Fatal("memory reader was not closed after unbound projection")
	}
}

func TestArtifactErrorDomainIsPreservedWithoutCapabilityLeak(t *testing.T) {
	fixture := newFixture(t)
	key := artifactKey{codingartifacts.PhaseAuthoring, codingartifacts.KindResourceProfile}
	fixture.source.errors = map[artifactKey]error{
		key: fmt.Errorf("redacted source: %w", codingartifacts.ErrArtifactUnavailable),
	}
	_, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if !errors.Is(err, codingartifacts.ErrArtifactUnavailable) {
		t.Fatalf("artifact error domain lost: %v", err)
	}
	if strings.Contains(err.Error(), fixture.authoringSpec.ResourceProfile.URL) {
		t.Fatalf("artifact capability leaked: %v", err)
	}
}

func TestGradingRejectsMemoryOrFreezeDriftBeforeOpen(t *testing.T) {
	fixture := newFixture(t)
	submission := codingrunner.FrozenSubmission{FrozenPatchSHA256: strings.Repeat("a", 64)}
	for name, mutate := range map[string]func(*GradingSpec){
		"memory as grader": func(spec *GradingSpec) { spec.GraderBundle = fixture.authoringSpec.MemoryBundle },
		"freeze digest":    func(spec *GradingSpec) { spec.FrozenPatchSHA256 = strings.Repeat("b", 64) },
		"object key":       func(spec *GradingSpec) { spec.FrozenSubmissionKey = "sha256/" + strings.Repeat("c", 64) },
	} {
		t.Run(name, func(t *testing.T) {
			fixture.source.reset()
			spec := fixture.gradingSpec
			spec.FrozenPatchSHA256 = submission.FrozenPatchSHA256
			spec.FrozenSubmissionKey = "sha256/" + submission.FrozenPatchSHA256
			mutate(&spec)
			if _, err := fixture.runtime.Grade(t.Context(), spec, submission); err == nil {
				t.Fatal("drifted grading spec accepted")
			}
			if len(fixture.source.calls) != 0 {
				t.Fatalf("artifacts opened before validation: %v", fixture.source.calls)
			}
		})
	}
}

func TestResourceDecoderRejectsDuplicateAndOversizedJSON(t *testing.T) {
	fixture := newFixture(t)
	extended := bytes.TrimSuffix(fixture.resource, []byte("\n"))
	extended = append(extended[:len(extended)-1], []byte(",\"future_policy_hint\":{\"ignored\":true}}\n")...)
	decoded, err := decodeResourceProfile(bytes.NewReader(extended), fixture.resourceSHA256)
	if err != nil || decoded != fixture.policy {
		t.Fatalf("forward-compatible resource profile failed: %#v err=%v", decoded, err)
	}
	duplicate := bytes.Replace(fixture.resource, []byte(`"schema":`), []byte(`"schema":"wrong","schema":`), 1)
	if _, err := decodeResourceProfile(bytes.NewReader(duplicate), fixture.resourceSHA256); err == nil {
		t.Fatal("duplicate resource field accepted")
	}
	if _, err := decodeResourceProfile(bytes.NewReader(bytes.Repeat([]byte(" "), maximumResourceProfileBytes+1)), fixture.resourceSHA256); err == nil {
		t.Fatal("oversized resource profile accepted")
	}
}

func TestRuntimeRejectsTypedNilDependenciesAndInvalidArtifactReaders(t *testing.T) {
	var nilSource *fixtureSource
	projector, projectorErr := codingseed.New(codingseed.Config{
		MaxBundleBytes: codingcontract.MaxCanonicalJSONBytes, SeedTimeout: time.Second,
	})
	if projectorErr != nil {
		t.Fatal(projectorErr)
	}
	if _, err := NewRuntime(RuntimeConfig{Artifacts: nilSource, Executor: &fixtureExecutor{}, SeedProjector: projector}); err == nil {
		t.Fatal("typed-nil artifact source accepted")
	}
	var nilExecutor *fixtureExecutor
	if _, err := NewRuntime(RuntimeConfig{
		Artifacts: artifactSourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return nil, errors.New("unused")
		}),
		Executor: nilExecutor, SeedProjector: projector,
	}); err == nil {
		t.Fatal("typed-nil executor accepted")
	}
	var nilProjector *codingseed.Projector
	if _, err := NewRuntime(RuntimeConfig{
		Artifacts: artifactSourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return nil, errors.New("unused")
		}),
		Executor: &fixtureExecutor{}, SeedProjector: nilProjector,
	}); err == nil {
		t.Fatal("typed-nil seed projector accepted")
	}

	broken := &trackingReadCloser{Reader: strings.NewReader("unused")}
	reader, err := openArtifact(
		t.Context(),
		artifactSourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return broken, codingartifacts.ErrArtifactUnavailable
		}),
		codingartifacts.Capability{},
	)
	if reader != nil || !errors.Is(err, codingartifacts.ErrArtifactUnavailable) || !broken.closed {
		t.Fatalf("reader=%#v closed=%v err=%v", reader, broken.closed, err)
	}
	reader, err = openArtifact(
		t.Context(),
		artifactSourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return nil, nil
		}),
		codingartifacts.Capability{},
	)
	if reader != nil || err == nil {
		t.Fatalf("nil reader accepted: reader=%#v err=%v", reader, err)
	}
	var typedNilReader *trackingReadCloser
	reader, err = openArtifact(
		t.Context(),
		artifactSourceFunc(func(context.Context, codingartifacts.Capability) (io.ReadCloser, error) {
			return typedNilReader, nil
		}),
		codingartifacts.Capability{},
	)
	if reader != nil || err == nil {
		t.Fatalf("typed-nil reader accepted: reader=%#v err=%v", reader, err)
	}
}

func callTool(t *testing.T, handler http.Handler, request codingrunner.ToolRequest) codingrunner.ToolResponse {
	t.Helper()
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	httpRequest := httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body))
	handler.ServeHTTP(recorder, httpRequest)
	if recorder.Code != http.StatusOK {
		t.Fatalf("tool status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response codingrunner.ToolResponse
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	return response
}

func applyFixturePatch(t *testing.T, handler http.Handler, binding Binding) {
	t.Helper()
	read := callTool(t, handler, codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		CallID: "grade-read", Name: "repo.read_file", Arguments: json.RawMessage(`{"path":"src/app.py"}`),
	})
	var readResult struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readResult); err != nil {
		t.Fatal(err)
	}
	arguments, err := json.Marshal(map[string]any{
		"path": "src/app.py", "expected_sha256": readResult.SHA256,
		"replacements": []map[string]string{{
			"old_text": "return value.strip()", "new_text": "return value.rstrip()",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	response := callTool(t, handler, codingrunner.ToolRequest{
		CodingContractVersion: codingrunner.ContractVersion,
		CaseID:                binding.CaseID, ProfileCapabilityID: binding.ProfileCapabilityID,
		CallID: "grade-edit", Name: "repo.apply_patch", Arguments: arguments,
	})
	if !response.OK {
		t.Fatalf("edit response=%#v", response)
	}
}

func freezeFixture(t *testing.T, fixture *fixture) codingrunner.FrozenSubmission {
	t.Helper()
	session, err := fixture.runtime.BeginAuthoring(t.Context(), fixture.authoringSpec)
	if err != nil {
		t.Fatal(err)
	}
	applyFixturePatch(t, session.Handler(), fixture.binding)
	frozen, err := session.Freeze(t.Context(), &fixtureRevoker{})
	if err != nil || frozen.Submission == nil {
		t.Fatalf("freeze=%#v err=%v", frozen, err)
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	return *frozen.Submission
}

func resourceBytes(t *testing.T, policy codinggrader.ResourcePolicy) []byte {
	t.Helper()
	value := resourceWire{
		Schema:          "dittobench-coding-grader-resource-v1",
		CandidateLimits: limitsValue(policy.CandidateLimits), ProtectedLimits: limitsValue(policy.ProtectedLimits),
		MaxCombinedDiskBytes: policy.MaxCombinedDiskBytes, MemoryLimitBytes: policy.MemoryLimitBytes,
		ScratchLimitBytes: policy.ScratchLimitBytes, PidsLimit: policy.PidsLimit, CPUQuotaMillis: policy.CPUQuotaMillis,
	}
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return append(body, '\n')
}

func memoryBytes(t *testing.T, memories []codingcontract.VisibleMemory) []byte {
	t.Helper()
	raw, err := json.Marshal(struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: memories})
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projection any
	if err := decoder.Decode(&projection); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projection); err != nil {
		t.Fatal(err)
	}
	return output.Bytes()
}

func limitsValue(limits codingrunner.Limits) limitsWire {
	return limitsWire{
		MaxBundleBytes: limits.MaxBundleBytes, MaxWorkspaceBytes: limits.MaxWorkspaceBytes,
		MaxFileBytes: limits.MaxFileBytes, MaxPatchBytes: limits.MaxPatchBytes,
		MaxEntries: limits.MaxEntries, MaxToolCalls: limits.MaxToolCalls,
		MaxReadBytes: limits.MaxReadBytes, MaxResponseBytes: limits.MaxResponseBytes,
		MaxSearchResults: limits.MaxSearchResults, MaxReplayCacheBytes: limits.MaxReplayCacheBytes,
		MaxTranscriptBytes: limits.MaxTranscriptBytes,
	}
}

func capability(
	binding Binding,
	phase codingartifacts.DeliveryPhase,
	kind codingartifacts.Kind,
	audience codingartifacts.Audience,
	sha string,
	size int,
) codingartifacts.Capability {
	return codingartifacts.Capability{
		TicketID: binding.TicketID, Phase: phase, Kind: kind, Audience: audience,
		SHA256: sha, SizeBytes: int64(size), URL: "https://storage.invalid/capability",
		ExpiresAt: binding.Deadline.Add(-time.Minute), TicketDeadline: binding.Deadline,
	}
}

func tarBytes(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var output bytes.Buffer
	archive := tar.NewWriter(&output)
	for path, body := range files {
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

func digest(value []byte) string {
	sum := sha256.Sum256(value)
	return hex.EncodeToString(sum[:])
}

func equalKeys(left, right []artifactKey) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
