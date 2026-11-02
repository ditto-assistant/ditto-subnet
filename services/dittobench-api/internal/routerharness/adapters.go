package routerharness

import (
	"context"
	"fmt"
)

// opencodeAdapter is the most fleshed-out adapter: opencode is the only harness
// with an established runtime precedent (the screener installs it with
// `npm install -g opencode-ai@<pin>`), so Install performs that install and
// Probe compares the CLI's reported version against the OpencodeVersion
// sentinel. RunTask still returns errNotImplemented: the scaffold has no live
// miner router to drive end-to-end yet.
type opencodeAdapter struct {
	run    commandRunner
	pin    string
	probeC execProbe
}

// NewOpencodeAdapter builds the opencode adapter with the OpencodeVersion pin.
func NewOpencodeAdapter(opts ...Option) *opencodeAdapter {
	cfg := newAdapterConfig(opts...)
	return &opencodeAdapter{
		run: cfg.run,
		pin: OpencodeVersion,
		probeC: execProbe{
			bin:         "opencode",
			versionArgs: []string{"--version"},
			run:         cfg.run,
		},
	}
}

func (a *opencodeAdapter) Harness() Harness       { return HarnessOpencode }
func (a *opencodeAdapter) BaseURLEnv() BaseURLEnv { return HarnessOpencode.BaseURLEnv() }

// Install pins opencode via npm, mirroring the screener's version-sentinel
// install. The pinned spec keeps the scored runtime reproducible.
func (a *opencodeAdapter) Install(ctx context.Context) error {
	spec := "opencode-ai@" + a.pin
	if out, err := a.run(ctx, "npm", "install", "-g", spec); err != nil {
		return fmt.Errorf("routerharness: opencode install %s: %w: %s", spec, err, string(out))
	}
	return nil
}

func (a *opencodeAdapter) Probe(ctx context.Context) (bool, string, error) {
	return a.probeC.probe(ctx)
}

// RunTask is not implemented in the shadow scaffold: there is no live miner
// router to power the harness end-to-end yet.
func (a *opencodeAdapter) RunTask(ctx context.Context, task Task) (bool, RunArtifact, error) {
	return false, RunArtifact{Harness: HarnessOpencode}, errNotImplemented
}

// stubAdapter backs the three harnesses without a runtime precedent yet. Probe
// and BaseURLEnv are real; Install and RunTask surface errNotImplemented so the
// gap is explicit and machine-classifiable.
type stubAdapter struct {
	harness Harness
	probeC  execProbe
}

func newStubAdapter(harness Harness, bin string, opts ...Option) *stubAdapter {
	cfg := newAdapterConfig(opts...)
	return &stubAdapter{
		harness: harness,
		probeC: execProbe{
			bin:         bin,
			versionArgs: []string{"--version"},
			run:         cfg.run,
		},
	}
}

func (a *stubAdapter) Harness() Harness       { return a.harness }
func (a *stubAdapter) BaseURLEnv() BaseURLEnv { return a.harness.BaseURLEnv() }

func (a *stubAdapter) Install(ctx context.Context) error {
	return fmt.Errorf("routerharness: %s install: %w", a.harness, errNotImplemented)
}

func (a *stubAdapter) Probe(ctx context.Context) (bool, string, error) {
	return a.probeC.probe(ctx)
}

func (a *stubAdapter) RunTask(ctx context.Context, task Task) (bool, RunArtifact, error) {
	return false, RunArtifact{Harness: a.harness}, fmt.Errorf("routerharness: %s run: %w", a.harness, errNotImplemented)
}

// NewClaudeCodeAdapter builds the Claude Code adapter (real Probe/BaseURLEnv,
// stubbed Install/RunTask). It probes the `claude` CLI.
func NewClaudeCodeAdapter(opts ...Option) HarnessAdapter {
	return newStubAdapter(HarnessClaudeCode, "claude", opts...)
}

// NewCodexAdapter builds the Codex adapter (real Probe/BaseURLEnv, stubbed
// Install/RunTask). It probes the `codex` CLI.
func NewCodexAdapter(opts ...Option) HarnessAdapter {
	return newStubAdapter(HarnessCodex, "codex", opts...)
}

// NewGrokAdapter builds the Grok adapter (real Probe/BaseURLEnv, stubbed
// Install/RunTask). It probes the `grok` CLI.
func NewGrokAdapter(opts ...Option) HarnessAdapter {
	return newStubAdapter(HarnessGrok, "grok", opts...)
}
