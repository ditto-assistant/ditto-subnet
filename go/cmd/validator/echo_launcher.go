package main

import (
	"context"
	"os"
	"os/exec"
)

// echoLauncher launches a sibling `echo-harness` binary in-process via
// `go run`, dodging Docker entirely. The smoke target and the hermetic
// e2e test use this path so CI does not need a docker daemon.
//
// In production the validator uses dockerLauncher; the choice is made
// in chooseLauncher based on Config.SelfTest and the commitment's Image
// field.
type echoLauncher struct{}

func (echoLauncher) describe(hotkey string) string {
	return "echo-harness(" + hotkey + ")"
}

// build prefers the env-overridden binary path so the validator-smoke
// target can pre-build the echo harness once and reuse it. Falls back
// to `go run ./cmd/echo-harness` from the current module root so
// developers can invoke `--self-test` without prep.
func (echoLauncher) build(ctx context.Context, _ string) (*exec.Cmd, error) {
	if path := os.Getenv("DITTO_ECHO_HARNESS_BIN"); path != "" {
		return exec.CommandContext(ctx, path), nil
	}
	return exec.CommandContext(ctx, "go", "run", "github.com/heyditto/ditto-subnet/go/cmd/echo-harness"), nil
}
