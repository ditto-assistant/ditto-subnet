package sandbox

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestMaterializeGitSourcePinsTheRequestedCommit(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not installed")
	}
	repo := t.TempDir()
	git := func(args ...string) string {
		t.Helper()
		out, err := exec.Command("git", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %s: %v", args, out, err)
		}
		return strings.TrimSpace(string(out))
	}
	git("init", "-q", repo)
	git("-C", repo, "config", "user.email", "test@example.com")
	git("-C", repo, "config", "user.name", "Test")
	file := filepath.Join(repo, "Dockerfile")
	if err := os.WriteFile(file, []byte("FROM scratch\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git("-C", repo, "add", "Dockerfile")
	git("-C", repo, "commit", "-qm", "first")
	pinned := git("-C", repo, "rev-parse", "HEAD")
	if err := os.WriteFile(file, []byte("FROM alpine\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git("-C", repo, "commit", "-qam", "second")
	latest := git("-C", repo, "rev-parse", "HEAD")
	if latest == pinned {
		t.Fatal("test branch did not advance")
	}

	workdir := t.TempDir()
	err := materializeGitSource(
		context.Background(),
		Source{GitURL: repo, GitRef: pinned},
		workdir,
		append(os.Environ(), "GIT_TERMINAL_PROMPT=0"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if got := git("-C", workdir, "rev-parse", "HEAD"); got != pinned {
		t.Fatalf("checked out %q, want %q", got, pinned)
	}
	content, err := os.ReadFile(filepath.Join(workdir, "Dockerfile"))
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "FROM scratch\n" {
		t.Fatalf("checkout followed mutable branch: %q", content)
	}
	git("-C", workdir, "fetch", "--depth", "1", "origin", latest)
	git("-C", workdir, "checkout", "--detach", "FETCH_HEAD")
	if err := verifyGitHead(context.Background(), workdir, os.Environ(), pinned); err == nil || !strings.Contains(err.Error(), "does not match pinned git_ref") {
		t.Fatalf("changed checkout was not rejected: %v", err)
	}
}

func TestMaterializeGitSourceRejectsMutableRefs(t *testing.T) {
	for _, ref := range []string{"", "main", "v1.0.0", "abc1234"} {
		err := materializeGitSource(
			context.Background(),
			Source{GitURL: "https://example.com/miner.git", GitRef: ref},
			t.TempDir(),
			os.Environ(),
		)
		if err == nil || !strings.Contains(err.Error(), "full 40-character lowercase commit SHA") {
			t.Fatalf("ref %q: expected immutable commit rejection, got %v", ref, err)
		}
	}
}
