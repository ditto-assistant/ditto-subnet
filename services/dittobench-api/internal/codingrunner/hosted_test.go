package codingrunner

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func hostedFixture(t *testing.T, executor CommandExecutor) (*Session, HostedAuthority, []byte) {
	t.Helper()
	bundle := fixtureBundle(t, false)
	authority := HostedAuthority{
		EvaluationID:     "10000000-0000-4000-8000-000000000001",
		AttemptID:        "20000000-0000-4000-8000-000000000002",
		AssignmentSHA256: strings.Repeat("a", 64),
	}
	manifest := fixtureManifest(t, bundle)
	manifest.CodingContractVersion = HostedContractVersion
	manifest.TicketID, manifest.CaseID = authority.EvaluationID, authority.AttemptID
	if err := manifest.Validate(time.Now()); err == nil {
		t.Fatal("v1 manifest validator accepted v2")
	}
	if err := ValidateHostedManifest(authority, manifest, time.Now()); err != nil {
		t.Fatal(err)
	}
	session, err := NewHostedSession(t.Context(), authority, manifest, bytes.NewReader(bundle), executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session, authority, bundle
}

func hostedTool(authority HostedAuthority, callID, name string, args any) ToolRequest {
	request := toolRequest(callID, name, args)
	request.CodingContractVersion = HostedContractVersion
	request.CaseID = authority.AttemptID
	return request
}

func TestHostedToolsFreezeAndPristineReplayUseNativeV2Identities(t *testing.T) {
	session, authority, bundle := hostedFixture(t, nil)
	request := hostedTool(authority, "read", "repo.read_file", map[string]any{"path": "src/parser.py"})
	body, _ := json.Marshal(request)
	response := httptest.NewRecorder()
	session.Handler().ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body)))
	if response.Code != http.StatusOK || response.Header().Get("Cache-Control") != "no-store" || !strings.Contains(response.Body.String(), "return value.strip()") {
		t.Fatalf("hosted read failed: %d %s", response.Code, response.Body.String())
	}
	edit := hostedTool(authority, "edit", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	})
	first := invokeOK(t, session, edit)
	replay := invokeOK(t, session, edit)
	if !reflect.DeepEqual(first, replay) {
		t.Fatal("retry changed the event")
	}
	frozen := session.Freeze()
	if frozen.Submission == nil || frozen.Submission.CodingContractVersion != HostedContractVersion {
		t.Fatalf("freeze: %#v", frozen)
	}
	if !reflect.DeepEqual(frozen, session.Freeze()) {
		t.Fatal("freeze changed on replay")
	}
	var patch map[string]any
	if err := json.Unmarshal(frozen.Submission.Patch, &patch); err != nil {
		t.Fatal(err)
	}
	if patch["schema"] != "dittobench-coding-frozen-patch-v2" || patch["assignment_sha256"] != authority.AssignmentSHA256 || patch["evaluation_id"] != authority.EvaluationID {
		t.Fatal("unbound v2 patch")
	}
	var transcript bytes.Buffer
	identity, err := session.WriteTranscript(&transcript)
	if err != nil || identity.Events != 2 || !bytes.Contains(transcript.Bytes(), []byte(`"coding_contract_version":2`)) {
		t.Fatalf("transcript: %#v %v", identity, err)
	}
	if _, err := session.Invoke(t.Context(), hostedTool(authority, "after-freeze", "git.status", map[string]any{})); err == nil {
		t.Fatal("frozen capability remained active")
	}
	workspace, err := ReplayHostedFrozenSubmission(t.Context(), authority, *frozen.Submission, bytes.NewReader(bundle), session.manifest.Limits)
	if err != nil {
		t.Fatal(err)
	}
	defer workspace.Close()
	root, err := workspace.TrustedPath()
	if err != nil || root == session.root {
		t.Fatal("grading reused authoring workspace")
	}
	actual, err := os.ReadFile(filepath.Join(root, "src/parser.py"))
	if err != nil || !strings.Contains(string(actual), "return value.rstrip()") {
		t.Fatal("patch was not replayed")
	}
	if err := ValidateFrozenSubmission(*frozen.Submission, session.manifest.Limits); err == nil {
		t.Fatal("v1 accepted v2 freeze")
	}
	if workspace, err := ReplayFrozenSubmission(t.Context(), *frozen.Submission, bytes.NewReader(bundle), session.manifest.Limits); err == nil {
		workspace.Close()
		t.Fatal("v1 replay accepted v2")
	}
}

func TestHostedReplayRejectsOtherAssignmentsAndRelabeledV1(t *testing.T) {
	session, authority, bundle := hostedFixture(t, nil)
	frozen := session.Freeze()
	for _, changed := range []HostedAuthority{
		{EvaluationID: authority.EvaluationID, AttemptID: authority.AttemptID, AssignmentSHA256: strings.Repeat("b", 64)},
		{EvaluationID: "30000000-0000-4000-8000-000000000003", AttemptID: authority.AttemptID, AssignmentSHA256: authority.AssignmentSHA256},
		{EvaluationID: authority.EvaluationID, AttemptID: "30000000-0000-4000-8000-000000000003", AssignmentSHA256: authority.AssignmentSHA256},
	} {
		if _, err := ReplayHostedFrozenSubmission(t.Context(), changed, *frozen.Submission, bytes.NewReader(bundle), session.manifest.Limits); err == nil {
			t.Fatal("accepted authority drift")
		}
	}
	old, _ := newFixtureSession(t, nil)
	v1 := old.Freeze()
	if err := ValidateHostedFrozenSubmission(authority, *v1.Submission, session.manifest.Limits); err == nil {
		t.Fatal("accepted v1 freeze")
	}
	v1.Submission.CodingContractVersion = HostedContractVersion
	v1.Submission.CaseID = authority.AttemptID
	if err := ValidateHostedFrozenSubmission(authority, *v1.Submission, session.manifest.Limits); err == nil {
		t.Fatal("accepted relabeled v1 patch")
	}
	if bytes.Contains(old.Freeze().Submission.Patch, []byte("assignment_sha256")) {
		t.Fatal("v1 patch changed")
	}
}

func TestHostedHTTPRejectsV1BeforeConsumingAnEvent(t *testing.T) {
	session, authority, _ := hostedFixture(t, nil)
	request := hostedTool(authority, "same-id", "git.status", map[string]any{})
	request.CodingContractVersion = ContractVersion
	body, _ := json.Marshal(request)
	response := httptest.NewRecorder()
	session.Handler().ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/tool", bytes.NewReader(body)))
	if response.Code != http.StatusBadRequest || session.sequence != 0 {
		t.Fatal("cross-version request consumed an event")
	}
	request.CodingContractVersion = HostedContractVersion
	if result := invokeOK(t, session, request); result.Sequence != 1 {
		t.Fatal("valid v2 call failed")
	}
	old, _ := newFixtureSession(t, nil)
	if _, err := old.Invoke(t.Context(), request); err == nil {
		t.Fatal("v1 accepted v2 tool")
	}
	if _, err := parseToolRequest(body); err != nil {
		t.Fatal("legacy parser changed")
	}
}

func TestHostedKeepsCommandAndProtectedPathEnforcement(t *testing.T) {
	executor := &recordingExecutor{result: CommandResult{ReturnCode: 0}}
	session, authority, _ := hostedFixture(t, executor)
	invokeOK(t, session, hostedTool(authority, "tests", "tests.run", map[string]any{"command_id": "visible-tests"}))
	if len(executor.seen) != 1 || executor.seen[0].ID != "visible-tests" {
		t.Fatal("manifest command was not executed")
	}
	result, err := session.Invoke(t.Context(), hostedTool(authority, "deny", "repo.apply_patch", map[string]any{
		"path": "tests/test_parser.py", "expected_sha256": session.base["tests/test_parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "True", "new_text": "False"}},
	}))
	if err != nil || result.OK {
		t.Fatal("protected path edit was accepted")
	}
	session.manifest.Limits.MaxToolCalls = 2
	if _, err := session.Invoke(t.Context(), hostedTool(authority, "over-budget", "git.status", map[string]any{})); err == nil {
		t.Fatal("call budget bypassed")
	}
}

type unreadBundle struct{ read bool }

func (reader *unreadBundle) Read([]byte) (int, error) { reader.read = true; return 0, io.EOF }

func TestHostedConstructionRejectsInvalidAuthorityBeforeReadingInputs(t *testing.T) {
	bundle := fixtureBundle(t, false)
	manifest := fixtureManifest(t, bundle)
	reader := &unreadBundle{}
	if _, err := NewHostedSession(context.Background(), HostedAuthority{}, manifest, reader, nil); err == nil || reader.read {
		t.Fatal("invalid authority read the bundle")
	}
	authority := HostedAuthority{EvaluationID: "10000000-0000-4000-8000-000000000001", AttemptID: "20000000-0000-4000-8000-000000000002", AssignmentSHA256: strings.Repeat("a", 64)}
	manifest.TicketID, manifest.CaseID = authority.EvaluationID, authority.AttemptID
	if _, err := NewHostedSession(t.Context(), authority, manifest, reader, nil); err == nil || reader.read {
		t.Fatal("hosted constructor accepted v1")
	}
	manifest.CodingContractVersion = HostedContractVersion
	if _, err := NewSession(t.Context(), manifest, reader, nil); err == nil || reader.read {
		t.Fatal("v1 constructor accepted v2")
	}
	manifest.Deadline = time.Now().Add(90 * time.Minute)
	if _, err := NewHostedSession(t.Context(), authority, manifest, reader, nil); err == nil || reader.read {
		t.Fatal("hosted constructor accepted a deadline over one hour")
	}
}

func TestLegacyPatchCanonicalBytesRemainUnchanged(t *testing.T) {
	patch, err := canonicalStruct(patchDocument{
		Schema: "dittobench-coding-frozen-patch-v1", CodingContractVersion: ContractVersion,
		CaseID: "legacy-case", BaseTreeSHA256: strings.Repeat("a", 64),
		VisibleBundleSHA256: strings.Repeat("b", 64), Changes: []FrozenChange{},
	})
	want := `{"base_tree_sha256":"` + strings.Repeat("a", 64) + `","case_id":"legacy-case","changes":[],"coding_contract_version":1,"schema":"dittobench-coding-frozen-patch-v1","visible_bundle_sha256":"` + strings.Repeat("b", 64) + `"}` + "\n"
	if err != nil || string(patch) != want {
		t.Fatal("legacy patch byte identity changed")
	}
}
