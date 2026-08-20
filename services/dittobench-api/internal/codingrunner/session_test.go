package codingrunner

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

type tarEntry struct {
	name     string
	body     []byte
	mode     int64
	typeflag byte
	linkname string
}

func makeBundle(t *testing.T, gzipBody bool, entries ...tarEntry) []byte {
	t.Helper()
	var raw bytes.Buffer
	var output ioWriteCloser = nopWriteCloser{Buffer: &raw}
	if gzipBody {
		output = gzip.NewWriter(&raw)
	}
	archive := tar.NewWriter(output)
	for _, entry := range entries {
		typeflag := entry.typeflag
		if typeflag == 0 {
			typeflag = tar.TypeReg
		}
		mode := entry.mode
		if mode == 0 {
			mode = 0o644
		}
		header := &tar.Header{
			Name: entry.name, Mode: mode, Size: int64(len(entry.body)), Typeflag: typeflag, Linkname: entry.linkname,
		}
		if typeflag == tar.TypeDir || typeflag == tar.TypeSymlink {
			header.Size = 0
		}
		if err := archive.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if header.Size > 0 {
			if _, err := archive.Write(entry.body); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := output.Close(); err != nil {
		t.Fatal(err)
	}
	return raw.Bytes()
}

type ioWriteCloser interface {
	Write([]byte) (int, error)
	Close() error
}

type nopWriteCloser struct{ *bytes.Buffer }

func (nopWriteCloser) Close() error { return nil }

func fixtureBundle(t *testing.T, gzipBody bool) []byte {
	t.Helper()
	return makeBundle(t, gzipBody,
		tarEntry{name: "src", typeflag: tar.TypeDir, mode: 0o755},
		tarEntry{name: "tests", typeflag: tar.TypeDir, mode: 0o755},
		tarEntry{name: "src/parser.py", body: []byte("def parse(value):\n    return value.strip()\n")},
		tarEntry{name: "tests/test_parser.py", body: []byte("def test_parser():\n    assert True\n")},
		tarEntry{name: "obsolete.txt", body: []byte("remove me\n")},
	)
}

func inspectBaseTree(t *testing.T, bundle []byte, limits Limits) string {
	t.Helper()
	root := t.TempDir()
	if err := extractVisibleBundle(context.Background(), root, bytes.NewReader(bundle), limits); err != nil {
		t.Fatal(err)
	}
	state, err := snapshot(context.Background(), root, limits)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := treeSHA256(state)
	if err != nil {
		t.Fatal(err)
	}
	return digest
}

func fixtureManifest(t *testing.T, bundle []byte) Manifest {
	t.Helper()
	limits := DefaultLimits()
	return Manifest{
		CodingContractVersion: ContractVersion,
		TicketID:              "ticket-001",
		CaseID:                "case-001",
		ProfileCapabilityID:   "profile-001",
		VisibleBundleSHA256:   sha256Hex(bundle),
		BaseTreeSHA256:        inspectBaseTree(t, bundle, limits),
		Deadline:              time.Now().Add(time.Hour),
		EditablePaths:         []string{"src/parser.py"},
		CreatablePaths:        []string{"src/helper.py"},
		DeletablePaths:        []string{"obsolete.txt"},
		TestCommands: []CommandSpec{{
			ID: "visible-tests", Argv: []string{"python", "-m", "pytest", "tests/test_parser.py"}, Timeout: time.Minute,
		}},
		BuildCommands: []CommandSpec{{
			ID: "python-compile", Argv: []string{"python", "-m", "compileall", "src"}, Timeout: time.Minute,
		}},
		Limits: limits,
	}
}

type recordingExecutor struct {
	mu     sync.Mutex
	seen   []CommandSpec
	result CommandResult
	mutate func(string) error
	err    error
}

type blockingExecutor struct {
	started chan struct{}
}

func (executor *blockingExecutor) Execute(ctx context.Context, _ string, _ CommandSpec) (CommandResult, error) {
	close(executor.started)
	<-ctx.Done()
	return CommandResult{TimedOut: true}, ctx.Err()
}

func (executor *recordingExecutor) Execute(_ context.Context, workspace string, command CommandSpec) (CommandResult, error) {
	executor.mu.Lock()
	executor.seen = append(executor.seen, command)
	executor.mu.Unlock()
	if executor.mutate != nil {
		if err := executor.mutate(workspace); err != nil {
			return CommandResult{}, err
		}
	}
	result := executor.result
	result.Stdout = strings.ReplaceAll(result.Stdout, "$WORKSPACE", workspace)
	result.Stderr = strings.ReplaceAll(result.Stderr, "$WORKSPACE", workspace)
	return result, executor.err
}

func newFixtureSession(t *testing.T, executor CommandExecutor) (*Session, []byte) {
	t.Helper()
	bundle := fixtureBundle(t, false)
	session, err := NewSession(context.Background(), fixtureManifest(t, bundle), bytes.NewReader(bundle), executor)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	return session, bundle
}

func toolRequest(callID, name string, arguments any) ToolRequest {
	body, err := json.Marshal(arguments)
	if err != nil {
		panic(err)
	}
	return ToolRequest{
		CodingContractVersion: ContractVersion,
		CaseID:                "case-001",
		ProfileCapabilityID:   "profile-001",
		CallID:                callID,
		Name:                  name,
		Arguments:             body,
	}
}

func invokeOK(t *testing.T, session *Session, request ToolRequest) ToolResponse {
	t.Helper()
	response, err := session.Invoke(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if !response.OK || response.Error != nil {
		t.Fatalf("tool failed: %#v", response)
	}
	return response
}

func TestSessionAppliesTypedChangesAndFreezesReplayablePatch(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	read := invokeOK(t, session, toolRequest("call-001", "repo.read_file", map[string]any{"path": "src/parser.py"}))
	var readResult struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(read.Result, &readResult); err != nil {
		t.Fatal(err)
	}
	invokeOK(t, session, toolRequest("call-002", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": readResult.SHA256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	}))
	invokeOK(t, session, toolRequest("call-003", "repo.create_file", map[string]any{
		"path": "src/helper.py", "content": "HELPER = True\n",
	}))
	invokeOK(t, session, toolRequest("call-004", "repo.delete_file", map[string]any{
		"path": "obsolete.txt", "expected_sha256": session.base["obsolete.txt"].sha256,
	}))
	status := invokeOK(t, session, toolRequest("call-005", "git.status", map[string]any{}))
	if !bytes.Contains(status.Result, []byte(`"clean":false`)) {
		t.Fatalf("unexpected status: %s", status.Result)
	}
	if _, err := session.WriteTranscript(io.Discard); err == nil {
		t.Fatal("unfrozen transcript export was accepted")
	}

	frozen := session.Freeze()
	if frozen.Failure != nil || frozen.Submission == nil {
		t.Fatalf("freeze failed: %#v", frozen)
	}
	submission := frozen.Submission
	if !submission.ProtectedPathsIntact || submission.AuthoringEventRoot != status.EventSHA256 ||
		!isLowerSHA256(submission.FrozenPatchSHA256) || sha256Hex(submission.Patch) != submission.FrozenPatchSHA256 {
		t.Fatalf("invalid frozen identities: %#v", submission)
	}
	var transcript bytes.Buffer
	transcriptIdentity, err := session.WriteTranscript(&transcript)
	if err != nil {
		t.Fatal(err)
	}
	if transcriptIdentity.SHA256 != submission.AuthoringTranscriptSHA256 ||
		transcriptIdentity.SizeBytes != submission.AuthoringTranscriptBytes || transcriptIdentity.Events != status.Sequence ||
		sha256Hex(transcript.Bytes()) != transcriptIdentity.SHA256 {
		t.Fatalf("transcript identity mismatch: %#v submission=%#v", transcriptIdentity, submission)
	}
	previous := initialEventRoot
	lines := bytes.Split(bytes.TrimSuffix(transcript.Bytes(), []byte{'\n'}), []byte{'\n'})
	for index, line := range lines {
		var event eventRecord
		if err := json.Unmarshal(line, &event); err != nil {
			t.Fatal(err)
		}
		if event.Sequence != uint64(index+1) || event.PreviousEventSHA256 != previous {
			t.Fatalf("event chain broke at %d: %#v", index, event)
		}
		previous = sha256Hex(append(append([]byte(nil), line...), '\n'))
	}
	if previous != submission.AuthoringEventRoot {
		t.Fatalf("transcript root=%s submission=%s", previous, submission.AuthoringEventRoot)
	}
	wantPaths := []string{"obsolete.txt", "src/helper.py", "src/parser.py"}
	if !slicesEqual(submission.ChangedPaths, wantPaths) {
		t.Fatalf("changed paths=%v want=%v", submission.ChangedPaths, wantPaths)
	}
	var patch patchDocument
	if err := json.Unmarshal(submission.Patch, &patch); err != nil {
		t.Fatal(err)
	}
	if patch.Schema != "dittobench-coding-frozen-patch-v1" || len(patch.Changes) != 3 {
		t.Fatalf("unexpected frozen patch: %#v", patch)
	}
	replayed := cloneState(session.base)
	for _, change := range patch.Changes {
		switch change.Kind {
		case "added", "modified":
			replayed[change.Path] = fileState{
				sha256: *change.AfterSHA256, size: int64(len(change.AfterContent)), mode: fs.FileMode(change.Mode), kind: "file",
			}
		case "deleted":
			delete(replayed, change.Path)
		default:
			t.Fatalf("unknown change kind %q", change.Kind)
		}
	}
	replayedTree, err := treeSHA256(replayed)
	if err != nil {
		t.Fatal(err)
	}
	if replayedTree != submission.FinalTreeSHA256 {
		t.Fatalf("replayed tree=%s want=%s", replayedTree, submission.FinalTreeSHA256)
	}

	submission.ChangedPaths[0] = "mutated"
	second := session.Freeze()
	if second.Submission == nil || second.Submission.ChangedPaths[0] != "obsolete.txt" {
		t.Fatal("cached freeze result was caller-mutable")
	}
	if _, err := session.Invoke(context.Background(), toolRequest("call-006", "git.status", map[string]any{})); err == nil {
		t.Fatal("frozen capability accepted a new tool call")
	}
}

func cloneState(value map[string]fileState) map[string]fileState {
	result := make(map[string]fileState, len(value))
	for key, state := range value {
		result[key] = state
	}
	return result
}

func slicesEqual[T comparable](left, right []T) bool {
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

func TestCallReplayAndConcurrencyAreSerialized(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	request := toolRequest("same-call", "repo.read_file", map[string]any{"path": "src/parser.py"})
	responses := make(chan ToolResponse, 20)
	errorsCh := make(chan error, 20)
	var wait sync.WaitGroup
	for range 20 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			response, err := session.Invoke(context.Background(), request)
			responses <- response
			errorsCh <- err
		}()
	}
	wait.Wait()
	close(responses)
	close(errorsCh)
	for err := range errorsCh {
		if err != nil {
			t.Fatal(err)
		}
	}
	var first ToolResponse
	for response := range responses {
		if first.CallID == "" {
			first = response
		}
		if response.Sequence != 1 || response.EventSHA256 != first.EventSHA256 || !bytes.Equal(response.Result, first.Result) {
			t.Fatalf("idempotent replay diverged: %#v vs %#v", response, first)
		}
	}
	if len(session.calls) != 1 {
		t.Fatalf("replay consumed %d calls", len(session.calls))
	}
	cacheAfterFirst, transcriptAfterFirst := session.replayCacheBytes, session.transcriptBytes
	equivalent := request
	equivalent.Arguments = json.RawMessage("{ \"path\" : \"src/parser.py\" }")
	equivalentResponse, err := session.Invoke(context.Background(), equivalent)
	if err != nil || equivalentResponse.Sequence != first.Sequence || equivalentResponse.EventSHA256 != first.EventSHA256 {
		t.Fatalf("typed-equivalent replay diverged: %#v err=%v", equivalentResponse, err)
	}
	if session.replayCacheBytes != cacheAfterFirst || session.transcriptBytes != transcriptAfterFirst ||
		session.replayCacheBytes > session.manifest.Limits.MaxReplayCacheBytes {
		t.Fatal("idempotent replay consumed retention budget")
	}
	sequences := make(chan uint64, 12)
	uniqueErrors := make(chan error, 12)
	for index := range 12 {
		wait.Add(1)
		go func(index int) {
			defer wait.Done()
			response, err := session.Invoke(context.Background(), toolRequest(
				"unique-"+twoDigits(index), "repo.read_file", map[string]any{"path": "src/parser.py"},
			))
			if err != nil {
				uniqueErrors <- err
				return
			}
			sequences <- response.Sequence
		}(index)
	}
	wait.Wait()
	close(sequences)
	close(uniqueErrors)
	for err := range uniqueErrors {
		t.Fatal(err)
	}
	observed := make([]int, 0, 12)
	for sequence := range sequences {
		observed = append(observed, int(sequence))
	}
	sort.Ints(observed)
	want := []int{2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}
	if !slicesEqual(observed, want) {
		t.Fatalf("sequences=%v want=%v", observed, want)
	}
	changed := request
	changed.Arguments = json.RawMessage(`{"path":"tests/test_parser.py"}`)
	if _, err := session.Invoke(context.Background(), changed); err == nil {
		t.Fatal("call_id reuse with changed bytes was accepted")
	}
	frozen := session.Freeze()
	if frozen.Failure == nil || frozen.Failure.Kind != "candidate_integrity" || frozen.Failure.Code != "call_id_conflict" {
		t.Fatalf("call_id conflict did not latch integrity: %#v", frozen)
	}
}

func twoDigits(value int) string {
	return string([]byte{'0' + byte(value/10), '0' + byte(value%10)})
}

func TestManifestIsDeepCopied(t *testing.T) {
	bundle := fixtureBundle(t, false)
	manifest := fixtureManifest(t, bundle)
	session, err := NewSession(context.Background(), manifest, bytes.NewReader(bundle), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	manifest.EditablePaths[0] = "tests/test_parser.py"
	manifest.TestCommands[0].Argv[0] = "sh"
	if _, allowed := session.editable["src/parser.py"]; !allowed || session.tests["visible-tests"].Argv[0] != "python" {
		t.Fatal("caller mutation changed runner authority")
	}
}

func TestCommandOutputIsScrubbedAndMutationFailsClosed(t *testing.T) {
	executor := &recordingExecutor{result: CommandResult{
		ReturnCode: 0, Stdout: "$WORKSPACE/tests/test_parser.py\n", Stderr: "$WORKSPACE/private\n", Duration: 25 * time.Millisecond,
	}}
	session, _ := newFixtureSession(t, executor)
	response := invokeOK(t, session, toolRequest("command-001", "tests.run", map[string]any{"command_id": "visible-tests"}))
	var commandResult struct {
		Stdout string `json:"stdout"`
		Stderr string `json:"stderr"`
	}
	if err := json.Unmarshal(response.Result, &commandResult); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(commandResult.Stdout+commandResult.Stderr, session.root) ||
		!strings.Contains(commandResult.Stdout, "<workspace>/tests") {
		t.Fatalf("workspace path was not scrubbed: %s", response.Result)
	}
	executor.mu.Lock()
	seen := append([]CommandSpec(nil), executor.seen...)
	executor.mu.Unlock()
	if len(seen) != 1 || seen[0].Argv[0] != "python" || seen[0].ID != "visible-tests" {
		t.Fatalf("executor saw non-manifest command: %#v", seen)
	}

	mutating := &recordingExecutor{mutate: func(root string) error {
		// This path is normally editable, so only the permanent command-mutation
		// latch prevents it from becoming a valid frozen patch.
		return os.WriteFile(filepath.Join(root, "src", "parser.py"), []byte("candidate side effect\n"), 0o644)
	}}
	mutatingSession, _ := newFixtureSession(t, mutating)
	failed, err := mutatingSession.Invoke(context.Background(), toolRequest("command-002", "build.run", map[string]any{"command_id": "python-compile"}))
	if err != nil || failed.OK || failed.Error == nil || failed.Error.Code != "command_mutation" {
		t.Fatalf("command mutation did not fail closed: response=%#v err=%v", failed, err)
	}
	frozen := mutatingSession.Freeze()
	if frozen.Failure == nil || frozen.Failure.Kind != "candidate_integrity" || frozen.Failure.Code != "command_mutation" {
		t.Fatalf("mutated workspace freeze=%#v", frozen)
	}

	scratchMutation := &recordingExecutor{result: CommandResult{ReturnCode: 0, WorkspaceMutated: true}}
	scratchSession, _ := newFixtureSession(t, scratchMutation)
	failed, err = scratchSession.Invoke(context.Background(), toolRequest(
		"command-003", "tests.run", map[string]any{"command_id": "visible-tests"},
	))
	if err != nil || failed.OK || failed.Error == nil || failed.Error.Code != "command_mutation" {
		t.Fatalf("scratch mutation signal did not fail closed: response=%#v err=%v", failed, err)
	}
	frozen = scratchSession.Freeze()
	if frozen.Failure == nil || frozen.Failure.Kind != "candidate_integrity" || frozen.Failure.Code != "command_mutation" {
		t.Fatalf("scratch-mutated freeze=%#v", frozen)
	}
}

func TestFreezeCancelsActiveCommandBeforeWaitingForLock(t *testing.T) {
	executor := &blockingExecutor{started: make(chan struct{})}
	session, _ := newFixtureSession(t, executor)
	invokeDone := make(chan error, 1)
	go func() {
		response, err := session.Invoke(context.Background(), toolRequest(
			"blocking-command", "tests.run", map[string]any{"command_id": "visible-tests"},
		))
		if err == nil && response.OK {
			err = errors.New("cancelled command unexpectedly succeeded")
		}
		invokeDone <- err
	}()
	select {
	case <-executor.started:
	case <-time.After(time.Second):
		t.Fatal("command did not start")
	}
	freezeDone := make(chan FreezeResult, 1)
	go func() { freezeDone <- session.Freeze() }()
	select {
	case <-invokeDone:
	case <-time.After(time.Second):
		t.Fatal("freeze did not cancel active command")
	}
	select {
	case frozen := <-freezeDone:
		if frozen.Submission == nil || frozen.Failure != nil {
			t.Fatalf("cancelled authoring session could not freeze: %#v", frozen)
		}
	case <-time.After(time.Second):
		t.Fatal("freeze remained blocked after command cancellation")
	}
}

func TestFreezeClassifiesProtectedSymlinkDirectoryAndPatchLimits(t *testing.T) {
	tests := map[string]struct {
		mutate func(*Session) error
		kind   string
		code   string
	}{
		"protected file": {
			mutate: func(session *Session) error {
				return os.WriteFile(filepath.Join(session.root, "tests", "test_parser.py"), []byte("changed\n"), 0o644)
			},
			kind: "candidate_integrity", code: "protected_path",
		},
		"symlink": {
			mutate: func(session *Session) error {
				return os.Symlink("../obsolete.txt", filepath.Join(session.root, "src", "link.py"))
			},
			kind: "candidate_integrity", code: "symlink",
		},
		"empty directory": {
			mutate: func(session *Session) error {
				return os.Mkdir(filepath.Join(session.root, "untracked"), 0o755)
			},
			kind: "candidate_integrity", code: "directory_change",
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			session, _ := newFixtureSession(t, nil)
			if err := test.mutate(session); err != nil {
				t.Fatal(err)
			}
			frozen := session.Freeze()
			if frozen.Failure == nil || frozen.Failure.Kind != test.kind || frozen.Failure.Code != test.code {
				t.Fatalf("freeze=%#v", frozen)
			}
		})
	}

	bundle := fixtureBundle(t, false)
	manifest := fixtureManifest(t, bundle)
	manifest.Limits.MaxPatchBytes = 128
	session, err := NewSession(context.Background(), manifest, bytes.NewReader(bundle), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	response := invokeOK(t, session, toolRequest("patch-limit", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": strings.Repeat("x", 256)}},
	}))
	if !response.OK {
		t.Fatal("edit should fit file limit before freeze")
	}
	frozen := session.Freeze()
	if frozen.Failure == nil || frozen.Failure.Kind != "repair_failure" || frozen.Failure.Code != "patch_limit" {
		t.Fatalf("patch limit freeze=%#v", frozen)
	}
}

func TestVisibleCapsuleVerificationRejectsUnsafeArchives(t *testing.T) {
	valid := fixtureBundle(t, true)
	manifest := fixtureManifest(t, valid)
	if session, err := NewSession(context.Background(), manifest, bytes.NewReader(valid), nil); err != nil {
		t.Fatalf("gzip capsule rejected: %v", err)
	} else {
		_ = session.Close()
	}
	tests := map[string][]byte{
		"traversal": makeBundle(t, false, tarEntry{name: "../escape.py", body: []byte("bad")}),
		"absolute":  makeBundle(t, false, tarEntry{name: "/escape.py", body: []byte("bad")}),
		"git":       makeBundle(t, false, tarEntry{name: ".git/config", body: []byte("bad")}),
		"symlink":   makeBundle(t, false, tarEntry{name: "link", typeflag: tar.TypeSymlink, linkname: "/etc/passwd"}),
		"duplicate": makeBundle(t, false, tarEntry{name: "same", body: []byte("one")}, tarEntry{name: "same", body: []byte("two")}),
	}
	for name, bundle := range tests {
		t.Run(name, func(t *testing.T) {
			bad := fixtureManifest(t, fixtureBundle(t, false))
			bad.VisibleBundleSHA256 = sha256Hex(bundle)
			if _, err := NewSession(context.Background(), bad, bytes.NewReader(bundle), nil); err == nil {
				t.Fatal("unsafe visible capsule was accepted")
			}
		})
	}

	bundle := fixtureBundle(t, false)
	badDigest := fixtureManifest(t, bundle)
	badDigest.VisibleBundleSHA256 = strings.Repeat("a", 64)
	if _, err := NewSession(context.Background(), badDigest, bytes.NewReader(bundle), nil); err == nil {
		t.Fatal("visible capsule digest mismatch was accepted")
	}
	badTree := fixtureManifest(t, bundle)
	badTree.BaseTreeSHA256 = strings.Repeat("b", 64)
	if _, err := NewSession(context.Background(), badTree, bytes.NewReader(bundle), nil); err == nil {
		t.Fatal("visible capsule tree mismatch was accepted")
	}
	unsafeMode := makeBundle(t, false, tarEntry{name: "unsafe.py", body: []byte("x"), mode: 0o666})
	unsafeManifest := fixtureManifest(t, bundle)
	unsafeManifest.VisibleBundleSHA256 = sha256Hex(unsafeMode)
	if _, err := NewSession(context.Background(), unsafeManifest, bytes.NewReader(unsafeMode), nil); err == nil {
		t.Fatal("world-writable capsule file was accepted")
	}
	directoryOnly := makeBundle(t, false, tarEntry{name: "empty", typeflag: tar.TypeDir, mode: 0o755})
	directoryManifest := fixtureManifest(t, bundle)
	directoryManifest.VisibleBundleSHA256 = sha256Hex(directoryOnly)
	if _, err := NewSession(context.Background(), directoryManifest, bytes.NewReader(directoryOnly), nil); err == nil {
		t.Fatal("directory-only capsule was accepted")
	}
	entryLimited := fixtureManifest(t, bundle)
	entryLimited.Limits.MaxEntries = 4
	if _, err := NewSession(context.Background(), entryLimited, bytes.NewReader(bundle), nil); err == nil {
		t.Fatal("capsule above entry limit was accepted")
	}
	bundleLimited := fixtureManifest(t, bundle)
	bundleLimited.Limits.MaxBundleBytes = int64(len(bundle) - 1)
	if _, err := NewSession(context.Background(), bundleLimited, bytes.NewReader(bundle), nil); err == nil {
		t.Fatal("capsule above byte limit was accepted")
	}
	cancelledContext, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := NewSession(cancelledContext, fixtureManifest(t, bundle), bytes.NewReader(bundle), nil); err == nil {
		t.Fatal("cancelled capsule stream was accepted")
	}
}

func TestCapsuleAndCreatedFileModesIgnoreProcessUmask(t *testing.T) {
	bundle := fixtureBundle(t, false)
	manifest := fixtureManifest(t, bundle)
	previous := unix.Umask(0o077)
	defer unix.Umask(previous)
	session, err := NewSession(context.Background(), manifest, bytes.NewReader(bundle), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	invokeOK(t, session, toolRequest("umask-create", "repo.create_file", map[string]any{
		"path": "src/helper.py", "content": "value = True\n",
	}))
	for filePath, expected := range map[string]fs.FileMode{"src/parser.py": 0o644, "src/helper.py": 0o644} {
		info, err := os.Lstat(filepath.Join(session.root, filepath.FromSlash(filePath)))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode()&fs.ModePerm != expected {
			t.Fatalf("%s mode=%#o want=%#o", filePath, info.Mode()&fs.ModePerm, expected)
		}
	}
	if session.manifest.BaseTreeSHA256 != manifest.BaseTreeSHA256 {
		t.Fatalf("base tree changed under umask: %s != %s", session.manifest.BaseTreeSHA256, manifest.BaseTreeSHA256)
	}
}

func TestFreezeRejectsNoncanonicalCommandSideEffects(t *testing.T) {
	tests := map[string]struct {
		mutate   func(*Session) error
		wantCode string
	}{
		"binary modified file": {
			mutate: func(session *Session) error {
				return os.WriteFile(filepath.Join(session.root, "src", "parser.py"), []byte{0xff, 0xfe}, 0o644)
			},
			wantCode: "binary_change",
		},
		"noncanonical created-file mode": {
			mutate: func(session *Session) error {
				return os.WriteFile(filepath.Join(session.root, "src", "helper.py"), []byte("value = True\n"), 0o600)
			},
			wantCode: "mode_change",
		},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			session, _ := newFixtureSession(t, nil)
			defer session.Close()
			if err := test.mutate(session); err != nil {
				t.Fatal(err)
			}
			frozen := session.Freeze()
			if frozen.Submission != nil || frozen.Failure == nil || frozen.Failure.Kind != "candidate_integrity" ||
				frozen.Failure.Code != test.wantCode {
				t.Fatalf("freeze=%#v", frozen)
			}
		})
	}
}

func TestExpiredCapabilityStillProducesFrozenEvidence(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	session.now = func() time.Time { return session.manifest.Deadline.Add(time.Second) }
	if _, err := session.Invoke(context.Background(), toolRequest("expired", "git.status", map[string]any{})); err == nil {
		t.Fatal("expired capability accepted a call")
	}
	frozen := session.Freeze()
	if frozen.Submission == nil || frozen.Failure != nil {
		t.Fatalf("expired session could not be frozen: %#v", frozen)
	}
}

func TestCloseRemovesWorkspaceAndClosedFreezeIsStable(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	root := session.root
	transcriptPath := session.transcriptPath
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatalf("workspace still exists after close: %v", err)
	}
	if _, err := os.Stat(transcriptPath); !os.IsNotExist(err) {
		t.Fatalf("transcript still exists after close: %v", err)
	}
	first := session.Freeze()
	second := session.Freeze()
	if first.Failure == nil || first.Failure.Code != "workspace_closed" || second.Failure == nil ||
		first.Failure.Code != second.Failure.Code || first.Failure.AuthoringEventRoot != second.Failure.AuthoringEventRoot {
		t.Fatalf("closed freeze was not stable: %#v %#v", first, second)
	}
}

func TestReadSearchRangeAndDeniedToolsRemainBounded(t *testing.T) {
	session, _ := newFixtureSession(t, nil)
	tree := invokeOK(t, session, toolRequest("inspect-tree", "repo.list_tree", map[string]any{"path": ".", "depth": 3}))
	if !bytes.Contains(tree.Result, []byte(`"src/parser.py"`)) || bytes.Contains(tree.Result, []byte(session.root)) {
		t.Fatalf("tree result=%s", tree.Result)
	}
	search := invokeOK(t, session, toolRequest("inspect-search", "repo.search", map[string]any{
		"path": "src", "query": "return", "max_results": 10,
	}))
	if !bytes.Contains(search.Result, []byte(`"line":2`)) || !bytes.Contains(search.Result, []byte(`"column":5`)) {
		t.Fatalf("search result=%s", search.Result)
	}
	rangeResult := invokeOK(t, session, toolRequest("inspect-range", "repo.read_range", map[string]any{
		"path": "src/parser.py", "start_line": 2, "end_line": 2,
	}))
	if !bytes.Contains(rangeResult.Result, []byte(`return value.strip()`)) || !bytes.Contains(rangeResult.Result, []byte(`"end_line":2`)) {
		t.Fatalf("range result=%s", rangeResult.Result)
	}

	denied, err := session.Invoke(t.Context(), toolRequest("protected-edit", "repo.apply_patch", map[string]any{
		"path": "tests/test_parser.py", "expected_sha256": session.base["tests/test_parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "assert True", "new_text": "assert False"}},
	}))
	if err != nil || denied.OK || denied.Error == nil || denied.Error.Code != "protected_path" {
		t.Fatalf("protected edit response=%#v err=%v", denied, err)
	}
	noExecutor, err := session.Invoke(t.Context(), toolRequest("no-executor", "tests.run", map[string]any{"command_id": "visible-tests"}))
	if err != nil || noExecutor.OK || noExecutor.Error == nil {
		t.Fatalf("missing executor response=%#v err=%v", noExecutor, err)
	}
	unknown, err := session.Invoke(t.Context(), toolRequest("unknown-tool", "repo.shell", map[string]any{}))
	if err != nil || unknown.OK || unknown.Error == nil || unknown.Error.Code != "unknown_tool" {
		t.Fatalf("unknown tool response=%#v err=%v", unknown, err)
	}
}

func TestRunnerIdentityGoldenVector(t *testing.T) {
	session, bundle := newFixtureSession(t, nil)
	invokeOK(t, session, toolRequest("golden-001", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	}))
	invokeOK(t, session, toolRequest("golden-002", "repo.create_file", map[string]any{
		"path": "src/helper.py", "content": "HELPER = True\n",
	}))
	invokeOK(t, session, toolRequest("golden-003", "repo.delete_file", map[string]any{
		"path": "obsolete.txt", "expected_sha256": session.base["obsolete.txt"].sha256,
	}))
	frozen := session.Freeze()
	if frozen.Submission == nil {
		t.Fatalf("golden freeze=%#v", frozen)
	}
	observed := map[string]string{
		"visible_bundle_sha256": sha256Hex(bundle),
		"base_tree_sha256":      frozen.Submission.BaseTreeSHA256,
		"final_tree_sha256":     frozen.Submission.FinalTreeSHA256,
		"frozen_patch_sha256":   frozen.Submission.FrozenPatchSHA256,
		"changed_path_root":     frozen.Submission.ChangedPathRoot,
		"authoring_event_root":  frozen.Submission.AuthoringEventRoot,
	}
	want := map[string]string{
		"visible_bundle_sha256": "acedfdc930cb871079e12388038864f10ad04c4d3ba2fcdd07e38da29d3d4110",
		"base_tree_sha256":      "7375213792a627a0cbf2ae9a78452be01fc9619ef0b4dc0a7c9c58d6a7a47f09",
		"final_tree_sha256":     "d92e03250230c3b3d6ff1f8e61f072006e87ff896244a4936f36ead628776aaa",
		"frozen_patch_sha256":   "8263d99c8b907782d598c9b69cb6f186f395e0a648f32a257dcde8360f694bb5",
		"changed_path_root":     "4fecc10bcf2d09fda7a2b6e5034e6392cf27b6040891491b09a4863967edcf32",
		"authoring_event_root":  "1e6febf28bb48e438810178b61bd5415c8994e462a400044645e5d7b620c3c25",
	}
	for key, expected := range want {
		if observed[key] != expected {
			t.Fatalf("runner golden identities changed: %#v", observed)
		}
	}
}
