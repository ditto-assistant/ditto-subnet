package sandbox

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// A fake docker binary echoes the endpoint it was invoked against, so each
// helper proves which daemon it would have reached.
func installEndpointEchoDocker(t *testing.T) {
	t.Helper()
	directory := t.TempDir()
	script := "#!/bin/sh\nprintf '%s' \"$DOCKER_HOST\"\n"
	if err := os.WriteFile(filepath.Join(directory, "docker"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory)
	t.Setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
}

func TestLocalDockerSelectsTheConfiguredEndpointForEveryCall(t *testing.T) {
	installEndpointEchoDocker(t)
	ctx := context.Background()

	ordinary := NewLocalDocker()
	if got := ordinary.inspectImageID(ctx, "image"); got != "tcp://127.0.0.1:2375" {
		t.Fatalf("ordinary runtime reached %q", got)
	}
	if out, err := ordinary.dockerOutput(ctx, "info"); err != nil || string(out) != "tcp://127.0.0.1:2375" {
		t.Fatalf("ordinary runtime output=%q err=%v", out, err)
	}

	dedicated := NewLocalDocker()
	dedicated.DockerHost = "unix:///run/ditto-coding-executor/docker.sock"
	if got := dedicated.inspectImageID(ctx, "image"); got != dedicated.DockerHost {
		t.Fatalf("dedicated runtime inspected through %q", got)
	}
	if out, err := dedicated.dockerOutput(ctx, "info"); err != nil || string(out) != dedicated.DockerHost {
		t.Fatalf("dedicated runtime output=%q err=%v", out, err)
	}
	// mappedPort parses after the last colon of whatever docker printed.
	if out, err := dedicated.mappedPort(ctx, "container"); err != nil || out != "///run/ditto-coding-executor/docker.sock" {
		t.Fatalf("dedicated mapped port output=%q err=%v", out, err)
	}
}

// Every Docker CLI invocation in this package must go through LocalDocker.docker
// so a dedicated endpoint can never silently fall back to the process daemon.
func TestSandboxHasNoDirectDockerExec(t *testing.T) {
	const helper = "func (d *LocalDocker) docker(ctx context.Context, args ...string) *exec.Cmd {\n" +
		"\tcommand := exec.CommandContext(ctx, \"docker\", args...)\n"
	direct := regexp.MustCompile(`exec\.Command(Context)?\(\s*[A-Za-z]+,\s*"docker"`)
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	helpers := 0
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		body, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		text := string(body)
		helpers += strings.Count(text, helper)
		if loc := direct.FindStringIndex(strings.Replace(text, helper, "", 1)); loc != nil {
			t.Fatalf("%s invokes docker outside LocalDocker.docker at byte %d", name, loc[0])
		}
	}
	if helpers != 1 {
		t.Fatalf("docker helper count=%d", helpers)
	}
}
