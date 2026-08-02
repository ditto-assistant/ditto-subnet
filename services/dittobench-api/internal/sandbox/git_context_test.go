package sandbox

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResolveGitContextSelectsMonorepoSubdirectory(t *testing.T) {
	root := t.TempDir()
	want := filepath.Join(root, "miners", "dittobench-starter-kit")
	if err := os.MkdirAll(want, 0o755); err != nil {
		t.Fatal(err)
	}

	got, err := resolveGitContext(root, "miners/dittobench-starter-kit")
	if err != nil {
		t.Fatal(err)
	}
	canonicalWant, err := filepath.EvalSymlinks(want)
	if err != nil {
		t.Fatal(err)
	}
	if got != canonicalWant {
		t.Fatalf("context = %q, want %q", got, canonicalWant)
	}
}

func TestResolveGitContextRejectsEscapesAndFiles(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "escape")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "file"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}

	for _, subdir := range []string{"../outside", "/tmp", "escape", "file"} {
		t.Run(strings.ReplaceAll(subdir, "/", "_"), func(t *testing.T) {
			if _, err := resolveGitContext(root, subdir); err == nil {
				t.Fatalf("accepted unsafe git_subdir %q", subdir)
			}
		})
	}
}
