package main

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestScorerArgsPreserveImageCommand(t *testing.T) {
	got := scorerArgs([]string{"version", "-json"})
	want := []string{"/dittobench-api", "-port", "8000", "version", "-json"}
	if len(got) != len(want) {
		t.Fatalf("scorer args = %#v, want %#v", got, want)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("scorer args = %#v, want %#v", got, want)
		}
	}
}

func TestShouldBootstrapUpdaterTargetsOneValidator(t *testing.T) {
	const target = "5Cg3DiRfrgzB1XzN7VuqQNchTgZ8PzPbphMKmVvHobWSL118"
	bootstrap, err := shouldBootstrapUpdater(target, target)
	if err != nil {
		t.Fatal(err)
	}
	if !bootstrap {
		t.Fatal("target validator was not selected")
	}

	bootstrap, err = shouldBootstrapUpdater(target, "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118")
	if err != nil {
		t.Fatal(err)
	}
	if bootstrap {
		t.Fatal("non-target validator was selected")
	}
}

func TestShouldBootstrapUpdaterRequiresBothIdentities(t *testing.T) {
	if _, err := shouldBootstrapUpdater("", "validator"); err == nil {
		t.Fatal("expected empty target to fail")
	}
	if _, err := shouldBootstrapUpdater("target", ""); err == nil {
		t.Fatal("expected empty host validator hotkey to fail")
	}
}

func TestReplaceUpdaterAtomicallyPreservesTargetOwnership(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "source")
	targetDirectory := filepath.Join(directory, "host-scripts")
	if err := os.Mkdir(targetDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(targetDirectory, updaterName)
	if err := os.WriteFile(source, []byte("new updater\n"), 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("old updater\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}

	if err := replaceUpdater(source, target); err != nil {
		t.Fatal(err)
	}

	contents, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != "new updater\n" {
		t.Fatalf("unexpected updater contents %q", contents)
	}
	after, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if after.Mode().Perm() != 0o755 {
		t.Fatalf("updater mode = %o, want 755", after.Mode().Perm())
	}
	beforeStat, beforeOK := before.Sys().(*syscall.Stat_t)
	afterStat, afterOK := after.Sys().(*syscall.Stat_t)
	if !beforeOK || !afterOK {
		t.Fatal("replacement has no ownership metadata")
	}
	if beforeStat.Uid != afterStat.Uid || beforeStat.Gid != afterStat.Gid {
		t.Fatalf(
			"updater ownership changed from %d:%d to %d:%d",
			beforeStat.Uid,
			beforeStat.Gid,
			afterStat.Uid,
			afterStat.Gid,
		)
	}
}

func TestReplaceUpdaterRejectsSymlinkTarget(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "source")
	realTarget := filepath.Join(directory, "real-target")
	target := filepath.Join(directory, updaterName)
	if err := os.WriteFile(source, []byte("new updater\n"), 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(realTarget, []byte("old updater\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(realTarget, target); err != nil {
		t.Fatal(err)
	}

	if err := replaceUpdater(source, target); err == nil {
		t.Fatal("expected symlink target to be rejected")
	}
	contents, err := os.ReadFile(realTarget)
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != "old updater\n" {
		t.Fatalf("symlink destination changed to %q", contents)
	}
}

func TestReplaceUpdaterRepairsMatchingNonExecutableTarget(t *testing.T) {
	directory := t.TempDir()
	source := filepath.Join(directory, "source")
	target := filepath.Join(directory, updaterName)
	contents := []byte("same updater\n")
	if err := os.WriteFile(source, contents, 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, contents, 0o600); err != nil {
		t.Fatal(err)
	}

	if err := replaceUpdater(source, target); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o755 {
		t.Fatalf("updater mode = %o, want 755", info.Mode().Perm())
	}
}

func TestRequireReadOnlyTreeRejectsWritableEntry(t *testing.T) {
	directory := t.TempDir()
	file := filepath.Join(directory, "updater")
	if err := os.WriteFile(file, []byte("updater\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := requireReadOnlyTree(directory); err == nil {
		t.Fatal("expected writable tree to be rejected")
	}
}

func TestRequireReadOnlyTreeAcceptsNonWritableTree(t *testing.T) {
	directory := t.TempDir()
	file := filepath.Join(directory, "updater")
	if err := os.WriteFile(file, []byte("updater\n"), 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o755) })
	if err := requireReadOnlyTree(directory); err != nil {
		t.Fatal(err)
	}
}

func TestRequireReadOnlyTreeAcceptsUntraversableTree(t *testing.T) {
	directory := t.TempDir()
	file := filepath.Join(directory, "updater")
	if err := os.WriteFile(file, []byte("updater\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o755) })
	if err := requireReadOnlyTree(directory); err != nil {
		t.Fatal(err)
	}
}

func TestRequireReadOnlyTreeRejectsUnlistableTraversableTree(t *testing.T) {
	directory := t.TempDir()
	file := filepath.Join(directory, "updater")
	if err := os.WriteFile(file, []byte("updater\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o111); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o755) })
	if err := requireReadOnlyTree(directory); err == nil {
		t.Fatal("expected traversable tree with hidden entries to be rejected")
	}
}

func TestRequireReadOnlyTreeRejectsSymlink(t *testing.T) {
	directory := t.TempDir()
	target := filepath.Join(directory, "target")
	link := filepath.Join(directory, "link")
	if err := os.WriteFile(target, []byte("target\n"), 0o555); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(directory, 0o755) })
	if err := requireReadOnlyTree(directory); err == nil {
		t.Fatal("expected symlink tree to be rejected")
	}
}
