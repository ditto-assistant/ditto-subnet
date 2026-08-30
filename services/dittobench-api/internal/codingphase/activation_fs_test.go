package codingphase

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJournalDirectoryCreationIsPrivateDurableAndNoFollow(t *testing.T) {
	parent := t.TempDir()
	if err := os.Chmod(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	leaf := "relay-" + strings.Repeat("a", 64)
	if err := ensureJournalDirectory(parent, leaf); err != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(filepath.Join(parent, leaf))
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0o700 {
		t.Fatalf("journal root info=%#v err=%v", info, err)
	}
	if err := ensureJournalDirectory(parent, leaf); err != nil {
		t.Fatal(err)
	}
	symlinkParent := t.TempDir()
	if err := os.Chmod(symlinkParent, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(t.TempDir(), filepath.Join(symlinkParent, leaf)); err != nil {
		t.Fatal(err)
	}
	if err := ensureJournalDirectory(symlinkParent, leaf); err == nil {
		t.Fatal("symlink journal root was accepted")
	}
	unsafeParent := t.TempDir()
	if err := os.Chmod(unsafeParent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := ensureJournalDirectory(unsafeParent, leaf); err == nil {
		t.Fatal("unsafe journal parent was accepted")
	}
}
