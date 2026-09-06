package codinghostedworker

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

type fixture struct {
	mu                sync.Mutex
	events            []string
	fail              string
	blockRun          bool
	runStarted        chan struct{}
	handler           http.Handler
	source            codingsource.HostedBinding
	inputs            *testInput
	patch, transcript []byte
	retentionCalls    int
	retained          bool
	bridge            *Bridge
}

func (f *fixture) event(name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, name)
	if f.fail == name {
		return fmt.Errorf("PRIVATE failure")
	}
	return nil
}
func (f *fixture) Authoring(context.Context, codingsource.HostedBinding) (*codinghostedinput.Prepared, error) {
	panic("test uses the private dependency seam")
}
func (f *fixture) Inference(context.Context, codingsource.HostedBinding) (Bridge, error) {
	if f.bridge != nil {
		return *f.bridge, f.event("bridge")
	}
	return Bridge{GrantID: "synthetic", Revoke: func(context.Context) error { return f.event("bridge-revoke") }, Close: func(context.Context) error { return f.event("bridge-close") }}, f.event("bridge")
}
func (f *fixture) CheckAuthoring(ctx context.Context, _ codingsource.HostedBinding) error {
	if err := f.event("check"); err != nil {
		return err
	}
	return ctx.Err()
}
func (f *fixture) Retain(_ context.Context, _ codingsource.HostedBinding, e Evidence) (string, error) {
	f.retentionCalls++
	if err := f.event("retain"); err != nil {
		return "", err
	}
	if e.Freeze.Submission == nil {
		return "", ErrAttempt
	}
	var transcript bytes.Buffer
	id, err := e.WriteTranscript(&transcript)
	if err != nil {
		return "", err
	}
	if id.SHA256 != e.Freeze.Submission.AuthoringTranscriptSHA256 {
		return "", ErrAttempt
	}
	if f.retained && (!bytes.Equal(f.patch, e.Freeze.Submission.Patch) || !bytes.Equal(f.transcript, transcript.Bytes())) {
		return "", ErrAttempt
	}
	f.patch = bytes.Clone(e.Freeze.Submission.Patch)
	f.transcript = bytes.Clone(transcript.Bytes())
	f.retained = true
	return strings.Repeat("e", 64), nil
}
func (f *fixture) CommitFreeze(_ context.Context, s codingsource.HostedBinding, patch []byte, evidence string) (codingrunner.HostedReplayAuthority, error) {
	if err := f.event("commit"); err != nil {
		return codingrunner.HostedReplayAuthority{}, err
	}
	if !f.retained || !bytes.Equal(patch, f.patch) || evidence != strings.Repeat("e", 64) {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	a := codingrunner.HostedReplayAuthority{HostedAuthority: codingrunner.HostedAuthority{EvaluationID: s.EvaluationID, AttemptID: s.AttemptID, AssignmentSHA256: s.AssignmentSHA256}, FrozenPatchSHA256: fmt.Sprintf("%x", sha256.Sum256(patch))}
	if f.fail == "wrong-ack" {
		a.FrozenPatchSHA256 = strings.Repeat("f", 64)
	}
	return a, nil
}
func (f *fixture) Abort(context.Context, codingsource.HostedBinding) error { return f.event("abort") }

type testHarness struct{ f *fixture }

func (h testHarness) SourceBinding() codingsource.HostedBinding { return h.f.source }
func (h testHarness) Activate(context.Context) error            { return h.f.event("activate") }
func (h testHarness) Health(context.Context) (codingcertifier.HealthResponse, error) {
	return codingcertifier.HealthResponse{Status: "ok", SupportedCodingContractVersions: []int{2}, Capabilities: []string{"scoped_memory_seed_v2", "coding_runner_tools_v2", "case_scoped_inference_v2"}}, h.f.event("health")
}
func (h testHarness) Seed(_ context.Context, s codingcontract.HostedSeedRequest) (codingcertifier.SeedResponse, error) {
	return codingcertifier.SeedResponse{CaseID: s.CaseID, ProfileCapabilityID: s.ProfileCapabilityID, MemoryBundleSHA256: s.MemoryBundleSHA256, MemoryCount: len(s.Memories)}, h.f.event("seed")
}
func (h testHarness) Run(ctx context.Context, r codingcontract.HostedRunRequest) (codingcertifier.RunResponse, error) {
	err := h.f.event("run")
	if err != nil {
		return codingcertifier.RunResponse{}, err
	}
	close(h.f.runStarted)
	if h.f.blockRun {
		<-ctx.Done()
		return codingcertifier.RunResponse{}, ctx.Err()
	}
	// Use the real native workspace HTTP tool path to edit the candidate file.
	args, _ := json.Marshal(map[string]any{"path": "app.py", "expected_sha256": fmt.Sprintf("%x", sha256.Sum256([]byte("return 1\n"))), "replacements": []map[string]string{{"old_text": "return 1", "new_text": "return 2"}}})
	wire, _ := json.Marshal(codingrunner.ToolRequest{CodingContractVersion: 2, CaseID: r.CaseID, ProfileCapabilityID: r.ProfileCapabilityID, CallID: "edit", Name: "repo.apply_patch", Arguments: args})
	recorder := httptest.NewRecorder()
	h.f.handler.ServeHTTP(recorder, httptest.NewRequest("POST", "/tool", bytes.NewReader(wire)))
	var response codingrunner.ToolResponse
	if recorder.Code != 200 || json.Unmarshal(recorder.Body.Bytes(), &response) != nil || !response.OK {
		return codingcertifier.RunResponse{}, ErrAttempt
	}
	return codingcertifier.RunResponse{CaseID: r.CaseID}, nil
}
func (h testHarness) Destroy(context.Context) error { return h.f.event("destroy") }

type testRoute struct{ f *fixture }

func (r testRoute) URL() string {
	return "http://host.docker.internal:9000/v2/coding/workspace/synthetic/tool"
}
func (r testRoute) Revoke(context.Context) error { return r.f.event("workspace-revoke") }
func (r testRoute) Close() error                 { return r.f.event("workspace-close") }

type testRelay struct{ f *fixture }

func (r testRelay) URL() string {
	return "http://host.docker.internal:9000/v2/coding/inference/synthetic"
}
func (r testRelay) Revoke(context.Context) error { return r.f.event("relay-revoke") }

type testInput struct {
	f         *fixture
	manifest  codingrunner.Manifest
	bundle    []byte
	authority codinghostedinput.Expected
	session   *codingrunner.Session
	wall      uint64
}

func (i *testInput) Authority() (codinghostedinput.Expected, error) { return i.authority, nil }
func (i *testInput) SeedRequest() (codingcontract.HostedSeedRequest, error) {
	return codingcontract.HostedSeedRequest{CodingContractVersion: 2, TicketID: i.manifest.TicketID, CaseID: i.manifest.CaseID, ProfileCapabilityID: i.manifest.ProfileCapabilityID, Memories: []codingcontract.VisibleMemory{}, MemoryBundleSHA256: fmt.Sprintf("%x", sha256.Sum256([]byte("{\"memories\":[]}\n")))}, nil
}
func (i *testInput) RunRequest(workspace, inference string) (codingcontract.HostedRunRequest, error) {
	wall := i.wall
	if wall == 0 {
		wall = 60
	}
	return codingcontract.HostedRunRequest{CodingContractVersion: 2, TicketID: i.manifest.TicketID, CaseID: i.manifest.CaseID, ProfileCapabilityID: i.manifest.ProfileCapabilityID, RepositoryEpoch: "epoch", VisibleBundleSHA256: i.manifest.VisibleBundleSHA256, Issue: codingcontract.Issue{Title: "Fix", Description: "Return two", Constraints: []string{}}, RuntimePolicy: codingcontract.RuntimePolicy{EditablePaths: []string{"app.py"}, TestCommandIDs: []string{}, BuildCommandIDs: []string{}}, WorkspaceCapabilityURL: workspace, InferenceBaseURL: inference, Budgets: codingcontract.Budgets{ModelInputTokens: 100, ModelOutputTokens: 100, WorkspaceToolCalls: 10, WallTimeSeconds: wall}}, nil
}
func (i *testInput) Begin(ctx context.Context, _ *codingexecutor.PhaseFactory) (*codingrunner.Session, error) {
	var err error
	i.session, err = codingrunner.NewHostedSession(ctx, codingrunner.HostedAuthority{EvaluationID: i.authority.EvaluationID, AttemptID: i.authority.AttemptID, AssignmentSHA256: i.authority.AssignmentSHA256}, i.manifest, bytes.NewReader(i.bundle), nil)
	return i.session, err
}
func (i *testInput) Close() { _ = i.f.event("inputs-close") }

func setup(t *testing.T) (*Attempt, *fixture) {
	t.Helper()
	f := &fixture{runStarted: make(chan struct{})}
	deadline := time.Now().Add(5 * time.Minute).Truncate(time.Second)
	binding := codingharness.HostedBinding{EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: "20000000-0000-4000-8000-000000000002", WorkerID: "30000000-0000-4000-8000-000000000003", AssignmentSHA256: strings.Repeat("a", 64), AgentArtifactSHA256: strings.Repeat("b", 64), ProfileCapabilityID: "hosted-20000000-0000-4000-8000-000000000002", Deadline: deadline}
	f.source = codingsource.HostedBinding{EvaluationID: binding.EvaluationID, AttemptID: binding.AttemptID, WorkerID: binding.WorkerID, AssignmentSHA256: binding.AssignmentSHA256, AgentArtifactSHA256: binding.AgentArtifactSHA256, ProfileCapabilityID: binding.ProfileCapabilityID, HarnessInstanceID: "synthetic-instance", Deadline: deadline}
	var bundle bytes.Buffer
	tw := tar.NewWriter(&bundle)
	body := []byte("return 1\n")
	if err := tw.WriteHeader(&tar.Header{Name: "app.py", Mode: 0600, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(body); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	limits := codingrunner.DefaultLimits()
	identity, err := codingrunner.InspectBundle(t.Context(), bytes.NewReader(bundle.Bytes()), limits)
	if err != nil {
		t.Fatal(err)
	}
	f.inputs = &testInput{f: f, bundle: bundle.Bytes(), manifest: codingrunner.Manifest{CodingContractVersion: 2, TicketID: binding.EvaluationID, CaseID: binding.AttemptID, ProfileCapabilityID: binding.ProfileCapabilityID, VisibleBundleSHA256: identity.VisibleBundleSHA256, BaseTreeSHA256: identity.TreeSHA256, Deadline: deadline, EditablePaths: []string{"app.py"}, CreatablePaths: []string{}, DeletablePaths: []string{}, TestCommands: []codingrunner.CommandSpec{}, BuildCommands: []codingrunner.CommandSpec{}, Limits: limits}, authority: codinghostedinput.Expected{EvaluationID: binding.EvaluationID, AttemptID: binding.AttemptID, WorkerID: binding.WorkerID, AssignmentSHA256: binding.AssignmentSHA256, DeadlineUnix: deadline.Unix()}}
	w := &Worker{dependencies{control: f, acquire: func(context.Context, codingharness.HostedBinding) (harness, error) {
		return testHarness{f}, f.event("acquire")
	}, load: func(context.Context, codingsource.HostedBinding) (input, error) { return f.inputs, f.event("load") }, workspace: func(_ context.Context, _ codingsource.HostedBinding, h http.Handler) (codingcertifier.PublishedCapability, error) {
		f.handler = h
		return testRoute{f}, f.event("workspace")
	}, relay: func(context.Context, codingsource.HostedBinding, Bridge) (inference, error) {
		return testRelay{f}, f.event("relay")
	}}}
	a, err := w.Attempt(binding)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		f.mu.Lock()
		f.fail = ""
		f.mu.Unlock()
		_ = a.Cleanup()
		if f.inputs.session != nil {
			_ = f.inputs.session.Close()
		}
	})
	return a, f
}

func TestAuthoringEditsFreezesRetainsAndCommitsAfterCleanup(t *testing.T) {
	a, f := setup(t)
	authority, err := a.Run(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	replay, err := codingrunner.ReplayHostedFrozenSubmission(t.Context(), authority, *a.freeze.Submission, bytes.NewReader(f.inputs.bundle), f.inputs.manifest.Limits)
	if err != nil {
		t.Fatal(err)
	}
	defer replay.Close()
	root, err := replay.TrustedPath()
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.ReadFile(filepath.Join(root, "app.py"))
	if err != nil || string(file) != "return 2\n" {
		t.Fatal("committed patch did not replay the actual edit")
	}
	for _, step := range []string{"workspace-revoke", "relay-revoke", "bridge-close", "destroy"} {
		if slices.Index(f.events, step) > slices.Index(f.events, "retain") {
			t.Fatal("evidence preceded cleanup", f.events)
		}
	}
	if slices.Index(f.events, "retain") > slices.Index(f.events, "commit") {
		t.Fatal("freeze preceded retention")
	}
	if authority.FrozenPatchSHA256 != a.freeze.Submission.FrozenPatchSHA256 {
		t.Fatal("wrong committed patch")
	}
	if _, err := a.Run(t.Context()); err == nil {
		t.Fatal("candidate rerun allowed")
	}
	if _, err := a.RetryFinalization(t.Context()); err != nil {
		t.Fatal("exact finalization replay failed")
	}
	if f.retentionCalls != 1 {
		t.Fatal("evidence regenerated")
	}
}

func TestAllCleanupBoundariesRunAndFailedCleanupCannotFreeze(t *testing.T) {
	for _, failure := range []string{"workspace-revoke", "relay-revoke", "bridge-revoke", "bridge-close", "destroy"} {
		t.Run(failure, func(t *testing.T) {
			a, f := setup(t)
			f.fail = failure
			if _, err := a.Run(t.Context()); err == nil {
				t.Fatal("cleanup failure accepted")
			}
			for _, step := range []string{"workspace-revoke", "relay-revoke", "bridge-revoke", "destroy", "abort"} {
				if !slices.Contains(f.events, step) {
					t.Fatal("cleanup skipped", step)
				}
			}
			if a.freeze != nil || f.retained || slices.Contains(f.events, "commit") {
				t.Fatal("unsafe freeze")
			}
			f.fail = ""
			if err := a.Cleanup(); err != nil {
				t.Fatal(err)
			}
			if _, err := a.RetryFinalization(t.Context()); err == nil {
				t.Fatal("failed attempt became gradeable")
			}
		})
	}
}

func TestAmbiguousRetentionAndCommitRetryWithoutRerun(t *testing.T) {
	for _, failure := range []string{"retain", "commit", "wrong-ack"} {
		t.Run(failure, func(t *testing.T) {
			a, f := setup(t)
			f.fail = failure
			if _, err := a.Run(t.Context()); err == nil {
				t.Fatal("ambiguous finalization accepted")
			}
			frozen := bytes.Clone(a.freeze.Submission.Patch)
			f.fail = ""
			if _, err := a.RetryFinalization(t.Context()); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(frozen, f.patch) {
				t.Fatal("patch changed")
			}
			if len(slices.DeleteFunc(slices.Clone(f.events), func(s string) bool { return s != "run" })) != 1 {
				t.Fatal("candidate rerun")
			}
		})
	}
}

func TestFailuresDoNotCommitAndRetainPartialHandles(t *testing.T) {
	for _, failure := range []string{"acquire", "activate", "health", "load", "seed", "workspace", "bridge", "relay", "run"} {
		t.Run(failure, func(t *testing.T) {
			a, f := setup(t)
			f.fail = failure
			if _, err := a.Run(t.Context()); err == nil {
				t.Fatal("failure accepted")
			}
			if slices.Contains(f.events, "commit") {
				t.Fatal("failure committed")
			}
			if failure == "bridge" && !slices.Contains(f.events, "bridge-close") {
				t.Fatal("partial bridge leaked")
			}
			if failure == "relay" && !slices.Contains(f.events, "relay-revoke") {
				t.Fatal("partial relay leaked")
			}
		})
	}
}

func TestAuthorityMismatchStopsBeforeWorkspace(t *testing.T) {
	a, f := setup(t)
	f.inputs.authority.AssignmentSHA256 = strings.Repeat("f", 64)
	if _, err := a.Run(t.Context()); err == nil {
		t.Fatal("authority mismatch accepted")
	}
	if slices.Contains(f.events, "seed") {
		t.Fatal("mismatched inputs reached miner")
	}
}

func TestLifecycleRevocationCancelsAuthoring(t *testing.T) {
	a, f := setup(t)
	f.blockRun = true
	done := make(chan error, 1)
	go func() { _, err := a.Run(t.Context()); done <- err }()
	<-f.runStarted
	f.mu.Lock()
	f.fail = "check"
	f.mu.Unlock()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("revocation accepted")
		}
	case <-time.After(8 * time.Second):
		t.Fatal("revocation did not cancel run")
	}
	if slices.Contains(f.events, "commit") {
		t.Fatal("revoked attempt committed")
	}
}

func TestCleanupBeforeRunCannotLaunchAndPrivateValuesDoNotSerialize(t *testing.T) {
	a, f := setup(t)
	if err := a.Cleanup(); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Run(t.Context()); err == nil || slices.Contains(f.events, "activate") {
		t.Fatal("closed attempt launched")
	}
	for _, v := range []any{a, Bridge{Token: []byte("PRIVATE")}, Evidence{}} {
		if _, err := json.Marshal(v); err == nil {
			t.Fatal("private value serialized")
		}
		if strings.Contains(fmt.Sprintf("%#v", v), "PRIVATE") {
			t.Fatal("private value logged")
		}
	}
}

func TestRunWallBudgetCancelsBeforeAssignmentDeadline(t *testing.T) {
	a, f := setup(t)
	f.blockRun = true
	f.inputs.wall = 1
	start := time.Now()
	if _, err := a.Run(t.Context()); err == nil {
		t.Fatal("over-budget run succeeded")
	}
	if time.Since(start) > 5*time.Second {
		t.Fatal("wall-time budget not enforced")
	}
	if slices.Contains(f.events, "commit") {
		t.Fatal("timed-out run committed")
	}
}
