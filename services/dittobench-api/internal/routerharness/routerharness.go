// Package routerharness scaffolds the orchestration for the SN118 router
// competition track. Unlike the model-locked DittoBench path (where the
// validator injects DITTOBENCH_MODEL and DITTOBENCH_INFERENCE_BASE_URL and drops
// every caller-supplied provider selector, see harnessSandboxEnvForProvider in
// cmd/dittobench-api/main.go), the router track INVERTS the lock: the miner owns
// the model, provider, and routing. The miner submits a router service exposing
// a multi-protocol front door that becomes the LLM backend powering the big-four
// third-party coding harnesses:
//
//   - Claude Code  -> Anthropic Messages  POST /v1/messages
//   - Codex        -> OpenAI Responses     POST /v1/responses (HTTPS floor)
//   - opencode     -> OpenAI Chat          POST /v1/chat/completions
//   - Grok         -> OpenAI Chat          POST /v1/chat/completions
//
// One trusted centralized scorer runs this orchestration and publishes a router
// ledger (see internal/routerscore); validators only read and fold it. Every
// path here is SHADOW / not weight-eligible and is wired into no live scoring
// path.
//
// The stable harness keys below MUST match the Python ledger keys in
// ditto/api_models/router_ledger.py (RouterHarness: claude_code/codex/opencode/
// grok) so the centralized scorer's Go emission and the validator's Python
// classifier key per-harness state identically.
package routerharness

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"regexp"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Harness is one of the four third-party coding harnesses the miner router must
// power. The string values are the stable ledger keys.
type Harness string

const (
	HarnessClaudeCode Harness = "claude_code"
	HarnessCodex      Harness = "codex"
	HarnessOpencode   Harness = "opencode"
	HarnessGrok       Harness = "grok"
)

// Harnesses returns the four harness keys in stable ledger order (the same
// weight order internal/routerscore.DefaultHarnessWeights uses).
func Harnesses() []Harness {
	return []Harness{HarnessClaudeCode, HarnessCodex, HarnessOpencode, HarnessGrok}
}

// Valid reports whether h is one of the four stable keys.
func (h Harness) Valid() bool {
	switch h {
	case HarnessClaudeCode, HarnessCodex, HarnessOpencode, HarnessGrok:
		return true
	default:
		return false
	}
}

// Version pins per harness. opencode has the only established runtime precedent
// (the screener installs `npm install -g opencode-ai@<pin>`), so its pin is a
// concrete npm version-sentinel; the other three are placeholder pins carried by
// the shadow scaffold until their runtimes land.
const (
	// OpencodeVersion mirrors the screener's opencode-ai npm pin. Probe compares
	// the CLI's reported version against this sentinel.
	OpencodeVersion   = "0.3.112"
	ClaudeCodeVersion = "1.0.0"
	CodexVersion      = "0.9.0"
	GrokVersion       = "0.1.0"
)

// CodexWireAPIVar is the scaffold marker that records the Responses wire
// protocol Codex must speak. Codex itself selects the wire API through its
// config.toml model-provider block (wire_api = "responses"); the router-track
// launcher templates that file from this env marker, so surfacing it in the
// sandbox env keeps the Responses floor explicit and testable.
const (
	CodexWireAPIVar       = "DITTOBENCH_CODEX_WIRE_API"
	CodexWireAPIResponses = "responses"
)

// errNotImplemented is returned by the not-yet-runnable adapter methods. It is
// deliberately explicit so a caller can distinguish "shadow scaffold, no runtime
// yet" from a real operational failure.
var errNotImplemented = errors.New("routerharness: adapter not implemented (shadow scaffold)")

// ErrNotImplemented exposes the sentinel for callers that classify scaffold
// gaps (e.g. errors.Is checks in the centralized scorer's telemetry).
var ErrNotImplemented = errNotImplemented

// BaseURLEnv describes how one harness is pointed at the miner's router: which
// env var carries the router base URL, which router path the harness's traffic
// lands on, the (placeholder) auth token var, any vars that must be present but
// empty, and any static extra vars (Codex's wire_api marker). It is the router
// track's analogue of the locked provider env, except it points at the miner's
// router instead of the validator gateway and applies no lock.
type BaseURLEnv struct {
	Harness    Harness           `json:"harness"`
	BaseURLVar string            `json:"base_url_var"`
	RouterPath string            `json:"router_path"`
	TokenVar   string            `json:"token_var"`
	EmptyVars  []string          `json:"empty_vars,omitempty"`
	ExtraVars  map[string]string `json:"extra_vars,omitempty"`
}

// Env renders the concrete env vars that point this harness at routerBaseURL
// with the supplied placeholder token. The token authorizes nothing on the
// scorer side; the miner's router owns real provider credentials.
func (b BaseURLEnv) Env(routerBaseURL, token string) map[string]string {
	env := make(map[string]string, 2+len(b.EmptyVars)+len(b.ExtraVars))
	if b.BaseURLVar != "" {
		env[b.BaseURLVar] = routerBaseURL
	}
	if b.TokenVar != "" {
		env[b.TokenVar] = token
	}
	for _, key := range b.EmptyVars {
		env[key] = ""
	}
	for key, value := range b.ExtraVars {
		env[key] = value
	}
	return env
}

// BaseURLEnv returns the env/path mapping for a harness. The mapping is real for
// every harness even where the adapter's Install/RunTask are still stubbed.
func (h Harness) BaseURLEnv() BaseURLEnv {
	switch h {
	case HarnessClaudeCode:
		// Claude Code reads ANTHROPIC_BASE_URL and authenticates with
		// ANTHROPIC_AUTH_TOKEN; ANTHROPIC_API_KEY must be empty so the token path
		// (not the key path) is used.
		return BaseURLEnv{
			Harness:    HarnessClaudeCode,
			BaseURLVar: "ANTHROPIC_BASE_URL",
			RouterPath: "/v1/messages",
			TokenVar:   "ANTHROPIC_AUTH_TOKEN",
			EmptyVars:  []string{"ANTHROPIC_API_KEY"},
		}
	case HarnessCodex:
		// Codex reads OPENAI_BASE_URL and, per the HTTPS floor, speaks the OpenAI
		// Responses wire protocol (wire_api = "responses"). WebSocket is an
		// optional upgrade the scaffold does not require.
		return BaseURLEnv{
			Harness:    HarnessCodex,
			BaseURLVar: "OPENAI_BASE_URL",
			RouterPath: "/v1/responses",
			TokenVar:   "OPENAI_API_KEY",
			ExtraVars:  map[string]string{CodexWireAPIVar: CodexWireAPIResponses},
		}
	case HarnessOpencode, HarnessGrok:
		// opencode and Grok both drive the plain OpenAI Chat Completions surface.
		return BaseURLEnv{
			Harness:    h,
			BaseURLVar: "OPENAI_BASE_URL",
			RouterPath: "/v1/chat/completions",
			TokenVar:   "OPENAI_API_KEY",
		}
	default:
		return BaseURLEnv{Harness: h}
	}
}

// Task is the minimal unit the scaffold runs against a router. The real coding
// task shape is owned by the dataset; this carries only what an adapter needs to
// drive one harness invocation.
type Task struct {
	ID     string `json:"id"`
	Prompt string `json:"prompt"`
}

// RunArtifact is one harness run's observed outcome. UpstreamTokenCostMicros and
// the upstream token counts are the router's UPSTREAM provider cost for the
// task — the dominant input to internal/routerscore's token axis (what the
// miner's compression bought), not anything the harness self-reports.
type RunArtifact struct {
	Harness                 Harness             `json:"harness"`
	Operational             bool                `json:"operational"`
	Version                 string              `json:"version"`
	UpstreamUsage           protocol.TokenUsage `json:"upstream_usage"`
	UpstreamTokenCostMicros uint64              `json:"upstream_token_cost_micros"`
	Notes                   string              `json:"notes,omitempty"`
}

// HarnessAdapter installs, probes, and runs one harness against a miner router.
//
//   - Install prepares the harness runtime (e.g. the opencode npm install).
//   - Probe reports whether the harness binary is available and its version,
//     reusing the screener's version-sentinel idea. Absence is (false, "", nil),
//     not an error.
//   - RunTask drives one task end-to-end and returns whether the router powered
//     the harness plus the run artifact.
type HarnessAdapter interface {
	Harness() Harness
	BaseURLEnv() BaseURLEnv
	Install(ctx context.Context) error
	Probe(ctx context.Context) (available bool, version string, err error)
	RunTask(ctx context.Context, task Task) (operational bool, artifact RunArtifact, err error)
}

// commandRunner runs an external command and returns its combined output. It is
// injectable so Probe (and the opencode Install) are unit-testable without a
// real toolchain.
type commandRunner func(ctx context.Context, name string, args ...string) ([]byte, error)

func defaultCommandRunner(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).CombinedOutput()
}

// Option configures an adapter (primarily to inject a commandRunner in tests).
type Option func(*adapterConfig)

type adapterConfig struct {
	run commandRunner
}

// WithCommandRunner overrides the external-command runner used by Install/Probe.
func WithCommandRunner(run commandRunner) Option {
	return func(c *adapterConfig) {
		if run != nil {
			c.run = run
		}
	}
}

func newAdapterConfig(opts ...Option) adapterConfig {
	cfg := adapterConfig{run: defaultCommandRunner}
	for _, opt := range opts {
		opt(&cfg)
	}
	return cfg
}

var semverPattern = regexp.MustCompile(`\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?`)

// parseVersion extracts the first semver-shaped token from CLI --version output
// (e.g. "opencode 0.3.112" -> "0.3.112").
func parseVersion(output []byte) string {
	return semverPattern.FindString(string(output))
}

// execProbe is the shared version-sentinel probe: it runs `<bin> <versionArgs>`
// and parses a semver from the output. A run error or empty parse is reported as
// unavailable (err nil); only the parsed version drives availability.
type execProbe struct {
	bin         string
	versionArgs []string
	run         commandRunner
}

func (p execProbe) probe(ctx context.Context) (bool, string, error) {
	out, err := p.run(ctx, p.bin, p.versionArgs...)
	if err != nil {
		// A missing or non-runnable binary is "not available", not a fault.
		return false, "", nil
	}
	version := parseVersion(out)
	if version == "" {
		return false, "", nil
	}
	return true, version, nil
}

// Adapters returns the default adapter for every harness in stable order.
func Adapters(opts ...Option) []HarnessAdapter {
	return []HarnessAdapter{
		NewClaudeCodeAdapter(opts...),
		NewCodexAdapter(opts...),
		NewOpencodeAdapter(opts...),
		NewGrokAdapter(opts...),
	}
}

// AdapterFor returns the default adapter for one harness, or an error for an
// unknown key.
func AdapterFor(h Harness, opts ...Option) (HarnessAdapter, error) {
	switch h {
	case HarnessClaudeCode:
		return NewClaudeCodeAdapter(opts...), nil
	case HarnessCodex:
		return NewCodexAdapter(opts...), nil
	case HarnessOpencode:
		return NewOpencodeAdapter(opts...), nil
	case HarnessGrok:
		return NewGrokAdapter(opts...), nil
	default:
		return nil, fmt.Errorf("routerharness: unknown harness %q", h)
	}
}
