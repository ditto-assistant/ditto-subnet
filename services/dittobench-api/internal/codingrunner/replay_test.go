package codingrunner

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func recanonicalizeFrozenPatch(t *testing.T, submission *FrozenSubmission) {
	t.Helper()
	patch, err := canonicalStruct(patchDocument{
		Schema: "dittobench-coding-frozen-patch-v1", CodingContractVersion: ContractVersion,
		CaseID: submission.CaseID, BaseTreeSHA256: submission.BaseTreeSHA256,
		VisibleBundleSHA256: submission.VisibleBundleSHA256, Changes: submission.Changes,
	})
	if err != nil {
		t.Fatal(err)
	}
	submission.Patch = patch
	submission.FrozenPatchSHA256 = sha256Hex(patch)
}

func frozenReplayFixture(t *testing.T) (FrozenSubmission, []byte, Limits) {
	t.Helper()
	session, bundle := newFixtureSession(t, nil)
	invokeOK(t, session, toolRequest("replay-edit", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	}))
	frozen := session.Freeze()
	if frozen.Submission == nil {
		t.Fatalf("fixture freeze=%#v", frozen)
	}
	return *cloneFreezeResult(frozen).Submission, bundle, session.manifest.Limits
}

func replayFrozenSession(t *testing.T, session *Session, bundle []byte) (*ReplayWorkspace, FrozenSubmission) {
	t.Helper()
	frozen := session.Freeze()
	if frozen.Submission == nil || frozen.Failure != nil {
		t.Fatalf("fixture freeze=%#v", frozen)
	}
	submission := *frozen.Submission
	workspace, err := ReplayFrozenSubmission(t.Context(), submission, bytes.NewReader(bundle), session.manifest.Limits)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = workspace.Close() })
	return workspace, submission
}

func TestReplayPreservesNoopAndEmptyFileTransitions(t *testing.T) {
	t.Run("no-op", func(t *testing.T) {
		session, bundle := newFixtureSession(t, nil)
		defer session.Close()
		workspace, submission := replayFrozenSession(t, session, bundle)
		if submission.ChangedPaths == nil || submission.Changes == nil || len(submission.ChangedPaths) != 0 ||
			len(submission.Changes) != 0 || submission.FinalTreeSHA256 != submission.BaseTreeSHA256 {
			t.Fatalf("no-op submission lost canonical empties: %#v", submission)
		}
		tree, err := workspace.TreeSHA256(t.Context())
		if err != nil || tree != submission.BaseTreeSHA256 {
			t.Fatalf("no-op replay tree=%s err=%v", tree, err)
		}
	})

	t.Run("empty created file", func(t *testing.T) {
		session, bundle := newFixtureSession(t, nil)
		defer session.Close()
		invokeOK(t, session, toolRequest("empty-create", "repo.create_file", map[string]any{
			"path": "src/helper.py", "content": "",
		}))
		workspace, submission := replayFrozenSession(t, session, bundle)
		if len(submission.Changes) != 1 || submission.Changes[0].AfterContent == nil ||
			len(submission.Changes[0].AfterContent) != 0 {
			t.Fatalf("empty created content lost presence: %#v", submission.Changes)
		}
		root, _ := workspace.TrustedPath()
		body, err := os.ReadFile(filepath.Join(root, "src", "helper.py"))
		if err != nil || body == nil || len(body) != 0 {
			t.Fatalf("empty created replay body=%v err=%v", body, err)
		}
	})

	t.Run("empty modified file", func(t *testing.T) {
		session, bundle := newFixtureSession(t, nil)
		defer session.Close()
		invokeOK(t, session, toolRequest("empty-edit", "repo.apply_patch", map[string]any{
			"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
			"replacements": []map[string]string{{
				"old_text": "def parse(value):\n    return value.strip()\n", "new_text": "",
			}},
		}))
		workspace, submission := replayFrozenSession(t, session, bundle)
		if len(submission.Changes) != 1 || submission.Changes[0].AfterContent == nil ||
			len(submission.Changes[0].AfterContent) != 0 {
			t.Fatalf("empty modified content lost presence: %#v", submission.Changes)
		}
		root, _ := workspace.TrustedPath()
		body, err := os.ReadFile(filepath.Join(root, "src", "parser.py"))
		if err != nil || body == nil || len(body) != 0 {
			t.Fatalf("empty modified replay body=%v err=%v", body, err)
		}
	})
}

func TestReplayFrozenSubmissionAndDelayedGraderInjection(t *testing.T) {
	submission, bundle, limits := frozenReplayFixture(t)
	workspace, err := ReplayFrozenSubmission(t.Context(), submission, bytes.NewReader(bundle), limits)
	if err != nil {
		t.Fatal(err)
	}
	defer workspace.Close()
	root, err := workspace.TrustedPath()
	if err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(filepath.Join(root, "src", "parser.py"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "return value.rstrip()") {
		t.Fatalf("frozen patch was not replayed: %s", body)
	}
	tree, err := workspace.TreeSHA256(t.Context())
	if err != nil || tree != submission.FinalTreeSHA256 {
		t.Fatalf("replay tree=%s want=%s err=%v", tree, submission.FinalTreeSHA256, err)
	}

	graderBundle := makeBundle(t, false,
		tarEntry{name: ".dittobench-grader", typeflag: tar.TypeDir, mode: 0o700},
		tarEntry{name: ".dittobench-grader/hidden_test.py", body: []byte("assert True\n"), mode: 0o600},
	)
	protected, err := workspace.MaterializeProtectedBundle(t.Context(), sha256Hex(graderBundle), bytes.NewReader(graderBundle), limits)
	if err != nil {
		t.Fatal(err)
	}
	defer protected.Close()
	protectedRoot, err := protected.TrustedPath()
	if err != nil {
		t.Fatal(err)
	}
	if protectedRoot == root {
		t.Fatal("grader material shared the candidate workspace")
	}
	if _, err := os.Stat(filepath.Join(root, ".dittobench-grader", "hidden_test.py")); !os.IsNotExist(err) {
		t.Fatalf("candidate workspace could see grader file: %v", err)
	}
	if _, err := os.Stat(filepath.Join(protectedRoot, ".dittobench-grader", "hidden_test.py")); err != nil {
		t.Fatal(err)
	}
	protectedTree, err := protected.TreeSHA256(t.Context())
	if err != nil || protectedTree != protected.InitialTreeSHA256() || protectedTree == submission.FinalTreeSHA256 {
		t.Fatalf("protected grader tree=%s err=%v", protectedTree, err)
	}
	if err := workspace.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatalf("replay workspace remained after close: %v", err)
	}
}

func TestReplayFrozenSubmissionRejectsEveryIdentityMismatch(t *testing.T) {
	submission, bundle, limits := frozenReplayFixture(t)
	tests := map[string]func(*FrozenSubmission){
		"visible digest": func(value *FrozenSubmission) { value.VisibleBundleSHA256 = strings.Repeat("a", 64) },
		"base tree":      func(value *FrozenSubmission) { value.BaseTreeSHA256 = strings.Repeat("b", 64) },
		"final tree":     func(value *FrozenSubmission) { value.FinalTreeSHA256 = strings.Repeat("c", 64) },
		"patch digest":   func(value *FrozenSubmission) { value.FrozenPatchSHA256 = strings.Repeat("d", 64) },
		"path root":      func(value *FrozenSubmission) { value.ChangedPathRoot = strings.Repeat("e", 64) },
		"after content": func(value *FrozenSubmission) {
			value.Changes[0].AfterContent = []byte("tampered")
		},
		"before digest": func(value *FrozenSubmission) {
			changed := strings.Repeat("f", 64)
			value.Changes[0].BeforeSHA256 = &changed
		},
		"patch bytes": func(value *FrozenSubmission) { value.Patch = []byte("different") },
		"unprotected": func(value *FrozenSubmission) { value.ProtectedPathsIntact = false },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			copyValue := *cloneFreezeResult(FreezeResult{Submission: &submission}).Submission
			mutate(&copyValue)
			if workspace, err := ReplayFrozenSubmission(t.Context(), copyValue, bytes.NewReader(bundle), limits); err == nil {
				workspace.Close()
				t.Fatal("tampered frozen submission was accepted")
			}
		})
	}
}

func TestReplayRejectsTransitionsTheAuthoringRunnerCannotProduce(t *testing.T) {
	t.Run("binary modified file", func(t *testing.T) {
		submission, _, limits := frozenReplayFixture(t)
		body := []byte{0xff, 0xfe, 0xfd}
		digest := sha256Hex(body)
		submission.Changes[0].AfterContent = body
		submission.Changes[0].AfterSHA256 = &digest
		recanonicalizeFrozenPatch(t, &submission)
		if err := validateFrozenSubmission(submission, limits); err == nil {
			t.Fatal("binary modified-file transition was accepted")
		}
	})

	t.Run("noncanonical added-file mode", func(t *testing.T) {
		session, _ := newFixtureSession(t, nil)
		defer session.Close()
		invokeOK(t, session, toolRequest("replay-create", "repo.create_file", map[string]any{
			"path": "src/helper.py", "content": "HELPER = True\n",
		}))
		frozen := session.Freeze()
		if frozen.Submission == nil {
			t.Fatalf("fixture freeze=%#v", frozen)
		}
		submission := *cloneFreezeResult(frozen).Submission
		index := slices.IndexFunc(submission.Changes, func(change FrozenChange) bool { return change.Kind == "added" })
		if index < 0 {
			t.Fatal("fixture did not contain an added file")
		}
		submission.Changes[index].Mode = 0o600
		recanonicalizeFrozenPatch(t, &submission)
		if err := validateFrozenSubmission(submission, session.manifest.Limits); err == nil {
			t.Fatal("noncanonical added-file mode was accepted")
		}
	})
}

func TestReplayRejectsWrongVisibleAndGraderBundleBytes(t *testing.T) {
	submission, bundle, limits := frozenReplayFixture(t)
	wrongVisible := append([]byte(nil), bundle...)
	wrongVisible[len(wrongVisible)/2] ^= 0x01
	if workspace, err := ReplayFrozenSubmission(t.Context(), submission, bytes.NewReader(wrongVisible), limits); err == nil {
		workspace.Close()
		t.Fatal("wrong visible capsule was accepted")
	}
	workspace, err := ReplayFrozenSubmission(context.Background(), submission, bytes.NewReader(bundle), limits)
	if err != nil {
		t.Fatal(err)
	}
	defer workspace.Close()
	grader := makeBundle(t, false, tarEntry{name: "grader.py", body: []byte("pass\n")})
	if _, err := workspace.MaterializeProtectedBundle(t.Context(), strings.Repeat("0", 64), bytes.NewReader(grader), limits); err == nil {
		t.Fatal("wrong grader capsule digest was accepted")
	}
}

func TestReplayCleanupRetriesAndRemainsRetryableAfterFailure(t *testing.T) {
	submission, bundle, limits := frozenReplayFixture(t)
	workspace, err := ReplayFrozenSubmission(t.Context(), submission, bytes.NewReader(bundle), limits)
	if err != nil {
		t.Fatal(err)
	}
	root, _ := workspace.TrustedPath()
	attempts := 0
	workspace.remove = func(target string) error {
		attempts++
		if attempts < 3 {
			return errors.New("transient cleanup failure")
		}
		return os.RemoveAll(target)
	}
	if err := workspace.Close(); err != nil || attempts != 3 {
		t.Fatalf("retrying cleanup attempts=%d err=%v", attempts, err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatalf("workspace remained after retrying cleanup: %v", err)
	}

	workspace, err = ReplayFrozenSubmission(t.Context(), submission, bytes.NewReader(bundle), limits)
	if err != nil {
		t.Fatal(err)
	}
	root, _ = workspace.TrustedPath()
	workspace.remove = func(string) error { return errors.New("persistent cleanup failure") }
	if err := workspace.Close(); err == nil {
		t.Fatal("persistent cleanup failure was ignored")
	}
	if retryRoot, err := workspace.TrustedPath(); err != nil || retryRoot != root {
		t.Fatalf("failed cleanup became unretryable: root=%s err=%v", retryRoot, err)
	}
	workspace.remove = os.RemoveAll
	if err := workspace.Close(); err != nil {
		t.Fatal(err)
	}
}
