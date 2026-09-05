package codingrunner

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"
)

func hostedCapsule(t *testing.T, entries []tarEntry, mutate func(*snapshotManifest)) []byte {
	t.Helper()
	files := []snapshotFile{}
	byPath := map[string][]byte{}
	for _, entry := range entries {
		mode := entry.mode
		if mode == 0 {
			mode = 0644
		}
		files = append(files, snapshotFile{Mode: uint32(mode), Path: entry.name, SHA256: sha256Hex(entry.body), SizeBytes: int64(len(entry.body))})
		byPath[entry.name] = entry.body
	}
	slices.SortFunc(files, func(a, b snapshotFile) int {
		return slices.Compare(strings.Split(a.Path, "/"), strings.Split(b.Path, "/"))
	})
	tree, _ := canonicalStruct(files)
	manifest := snapshotManifest{Schema: "dittobench-coding-sanitized-snapshot-v2", Files: files, SourceTreeSHA256: sha256Hex(tree), SnapshotTreeSHA256: sha256Hex(tree), ExcludedRootEntries: []string{}}
	if mutate != nil {
		mutate(&manifest)
	}
	body, _ := canonicalStruct(manifest)
	var result bytes.Buffer
	writer := tar.NewWriter(&result)
	write := func(path string, mode int64, value []byte) {
		if err := writer.WriteHeader(&tar.Header{Name: path, Typeflag: tar.TypeReg, Mode: mode, Size: int64(len(value)), ModTime: time.Unix(0, 0), Format: tar.FormatPAX}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(value); err != nil {
			t.Fatal(err)
		}
	}
	write("manifest.json", 0600, body)
	for _, file := range files {
		write("workspace/"+file.Path, int64(file.Mode), byPath[file.Path])
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	for result.Len()%10240 != 0 {
		result.WriteByte(0)
	}
	return result.Bytes()
}

func TestHostedCapsuleCompilesIntoRealAuthoringAndPristineReplay(t *testing.T) {
	capsule := hostedCapsule(t, []tarEntry{
		{name: "src/parser.py", body: []byte("def parse(value):\n    return value.strip()\n")},
		{name: "tests/test_parser.py", body: []byte("def test_parser():\n    assert True\n")},
		{name: "obsolete.txt", body: []byte("remove me\n")},
	}, nil)
	compiled, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	authority := HostedAuthority{"10000000-0000-4000-8000-000000000001", "20000000-0000-4000-8000-000000000002", strings.Repeat("c", 64)}
	manifest := fixtureManifest(t, compiled.Bundle)
	manifest.CodingContractVersion = HostedContractVersion
	manifest.TicketID, manifest.CaseID = authority.EvaluationID, authority.AttemptID
	manifest.VisibleBundleSHA256, manifest.BaseTreeSHA256 = compiled.Identity.VisibleBundleSHA256, compiled.Identity.TreeSHA256
	session, err := NewHostedSession(t.Context(), authority, manifest, bytes.NewReader(compiled.Bundle), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	if _, err := os.Stat(filepath.Join(session.root, "manifest.json")); !os.IsNotExist(err) {
		t.Fatal("private manifest entered candidate workspace")
	}
	invokeOK(t, session, hostedTool(authority, "edit", "repo.apply_patch", map[string]any{
		"path": "src/parser.py", "expected_sha256": session.base["src/parser.py"].sha256,
		"replacements": []map[string]string{{"old_text": "return value.strip()", "new_text": "return value.rstrip()"}},
	}))
	frozen := session.Freeze()
	if frozen.Submission == nil {
		t.Fatal("native freeze failed")
	}
	workspace, err := ReplayHostedFrozenSubmission(t.Context(), authority, *frozen.Submission, bytes.NewReader(compiled.Bundle), manifest.Limits)
	if err != nil {
		t.Fatal(err)
	}
	defer workspace.Close()
	tree, err := workspace.TreeSHA256(t.Context())
	if err != nil || tree != frozen.Submission.FinalTreeSHA256 {
		t.Fatal("native replay drifted")
	}
}

func TestHostedSnapshotUsesPythonPathOrderingAndRedactsDiagnostics(t *testing.T) {
	capsule := hostedCapsule(t, []tarEntry{
		{name: "a.txt", body: []byte("PRIVATE_MARKER")},
		{name: "a/child", body: []byte("child")},
		{name: "dir/" + strings.Repeat("long", 28) + ".py", mode: 0755, body: []byte("long path")},
	}, nil)
	compiled, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(fmt.Sprintf("%v %+v %#v", compiled, compiled, compiled), "PRIVATE_MARKER") {
		t.Fatal("snapshot diagnostics leaked")
	}
	if _, err := json.Marshal(compiled); err == nil {
		t.Fatal("snapshot serialized")
	}
	second, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits())
	if err != nil || !bytes.Equal(compiled.Bundle, second.Bundle) {
		t.Fatal("projection is not deterministic")
	}
}

func TestHostedSnapshotRejectsDriftForbiddenFilesAndTrailers(t *testing.T) {
	base := []tarEntry{{name: "file.txt", body: []byte("private")}}
	for _, name := range []string{"../outside", ".git/config", "node_modules/module.js", "a/.env.secret", "target/main"} {
		capsule := hostedCapsule(t, []tarEntry{{name: name, body: []byte("PRIVATE_MARKER")}}, nil)
		if _, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits()); err != ErrHostedSnapshot {
			t.Fatal("unsafe path accepted")
		}
	}
	for _, mutate := range []func(*snapshotManifest){
		func(m *snapshotManifest) { m.SourceTreeSHA256 = strings.Repeat("a", 64) },
		func(m *snapshotManifest) { m.Files[0].SHA256 = strings.Repeat("b", 64) },
		func(m *snapshotManifest) { m.Files[0].Mode = 0600 },
		func(m *snapshotManifest) { m.ExcludedRootEntries = []string{"secret"} },
	} {
		capsule := hostedCapsule(t, base, mutate)
		if _, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits()); err != ErrHostedSnapshot {
			t.Fatal("manifest drift accepted")
		}
	}
	capsule := hostedCapsule(t, base, nil)
	if _, err := CompileHostedSnapshot(t.Context(), capsule, strings.Repeat("d", 64), DefaultLimits()); err != ErrHostedSnapshot {
		t.Fatal("wrong object accepted")
	}
	capsule[len(capsule)-1] = 1
	if _, err := CompileHostedSnapshot(t.Context(), capsule, sha256Hex(capsule), DefaultLimits()); err != ErrHostedSnapshot {
		t.Fatal("nonzero trailer accepted")
	}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, err := CompileHostedSnapshot(ctx, capsule, sha256Hex(capsule), DefaultLimits()); err != ErrHostedSnapshot {
		t.Fatal("cancelled conversion accepted")
	}
}

// Optional owner-local compatibility audit. Private bytes never enter Git or CI.
// This is not a provider, sandbox, model, grading, or scoring certification.
func TestHostedSnapshotPrivateCorpusCompatibility(t *testing.T) {
	root := os.Getenv("DITTO_CODING_PRIVATE_PAYLOAD_TEST_DIR")
	if root == "" {
		t.Skip("owner-local private payload not configured")
	}
	var payload struct {
		Schema string `json:"schema"`
		Tasks  []struct {
			Artifacts map[string]string `json:"artifacts"`
		} `json:"task_assets"`
	}
	body, err := readPrivateFixture(filepath.Join(root, "payload-authority.json"), 8<<20)
	if err != nil || len(body) > 8<<20 || json.Unmarshal(body, &payload) != nil || payload.Schema != "dittobench-coding-private-payload-v2" || len(payload.Tasks) != 250 {
		t.Fatal("private fixture authority is invalid")
	}
	seen := map[string]bool{}
	for _, task := range payload.Tasks {
		digest := task.Artifacts["visible_bundle"]
		if !isLowerSHA256(digest) {
			t.Fatal("private fixture object identity is invalid")
		}
		if seen[digest] {
			continue
		}
		seen[digest] = true
		path := filepath.Join(root, "objects", digest+".bin")
		capsule, err := readPrivateFixture(path, 128<<20)
		if err != nil {
			t.Fatal("private fixture object unavailable")
		}
		if _, err := CompileHostedSnapshot(t.Context(), capsule, digest, DefaultLimits()); err != nil {
			t.Fatalf("private snapshot %d is incompatible", len(seen))
		}
	}
	if len(seen) != 50 {
		t.Fatalf("expected 50 distinct snapshots, got %d", len(seen))
	}
	t.Logf("verified %d private snapshots across %d task arms", len(seen), len(payload.Tasks))
}

func readPrivateFixture(path string, maximum int64) ([]byte, error) {
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, ErrHostedSnapshot
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Size() > maximum {
		return nil, ErrHostedSnapshot
	}
	body, err := io.ReadAll(io.LimitReader(file, maximum+1))
	if err != nil || int64(len(body)) > maximum {
		return nil, ErrHostedSnapshot
	}
	return body, nil
}
