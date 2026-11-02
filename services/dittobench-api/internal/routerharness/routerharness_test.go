package routerharness

import (
	"context"
	"errors"
	"testing"
)

func TestHarnessKeysMatchLedger(t *testing.T) {
	// These four keys MUST match ditto/api_models/router_ledger.py RouterHarness.
	want := map[Harness]bool{
		"claude_code": true,
		"codex":       true,
		"opencode":    true,
		"grok":        true,
	}
	got := Harnesses()
	if len(got) != len(want) {
		t.Fatalf("Harnesses() returned %d keys, want %d: %v", len(got), len(want), got)
	}
	seen := map[Harness]bool{}
	for _, h := range got {
		if !want[h] {
			t.Errorf("unexpected harness key %q", h)
		}
		if !h.Valid() {
			t.Errorf("harness %q reported not Valid()", h)
		}
		if seen[h] {
			t.Errorf("harness %q repeated", h)
		}
		seen[h] = true
	}
	if Harness("gemini").Valid() {
		t.Error("non-router harness reported Valid()")
	}
	// Stable order matches DefaultHarnessWeights order in routerscore.
	if got[0] != HarnessClaudeCode || got[3] != HarnessGrok {
		t.Fatalf("unexpected order: %v", got)
	}
}

func TestBaseURLEnvMapping(t *testing.T) {
	const (
		routerURL = "https://miner-router.example/api"
		token     = "router-placeholder"
	)
	tests := []struct {
		harness    Harness
		baseURLVar string
		routerPath string
		tokenVar   string
		empty      []string
		wireAPI    bool
	}{
		{HarnessClaudeCode, "ANTHROPIC_BASE_URL", "/v1/messages", "ANTHROPIC_AUTH_TOKEN", []string{"ANTHROPIC_API_KEY"}, false},
		{HarnessCodex, "OPENAI_BASE_URL", "/v1/responses", "OPENAI_API_KEY", nil, true},
		{HarnessOpencode, "OPENAI_BASE_URL", "/v1/chat/completions", "OPENAI_API_KEY", nil, false},
		{HarnessGrok, "OPENAI_BASE_URL", "/v1/chat/completions", "OPENAI_API_KEY", nil, false},
	}
	for _, tc := range tests {
		t.Run(string(tc.harness), func(t *testing.T) {
			b := tc.harness.BaseURLEnv()
			if b.Harness != tc.harness {
				t.Fatalf("Harness = %q, want %q", b.Harness, tc.harness)
			}
			if b.BaseURLVar != tc.baseURLVar {
				t.Errorf("BaseURLVar = %q, want %q", b.BaseURLVar, tc.baseURLVar)
			}
			if b.RouterPath != tc.routerPath {
				t.Errorf("RouterPath = %q, want %q", b.RouterPath, tc.routerPath)
			}
			if b.TokenVar != tc.tokenVar {
				t.Errorf("TokenVar = %q, want %q", b.TokenVar, tc.tokenVar)
			}

			env := b.Env(routerURL, token)
			if env[tc.baseURLVar] != routerURL {
				t.Errorf("%s = %q, want router URL %q", tc.baseURLVar, env[tc.baseURLVar], routerURL)
			}
			if env[tc.tokenVar] != token {
				t.Errorf("%s = %q, want token %q", tc.tokenVar, env[tc.tokenVar], token)
			}
			// Empty vars must be present (so the harness prefers the token path)
			// yet blank.
			for _, key := range tc.empty {
				value, ok := env[key]
				if !ok {
					t.Errorf("empty var %q missing from env", key)
				}
				if value != "" {
					t.Errorf("%s = %q, want empty", key, value)
				}
			}
			// Codex must carry the Responses wire_api marker; nobody else does.
			if got := env[CodexWireAPIVar]; tc.wireAPI {
				if got != CodexWireAPIResponses {
					t.Errorf("%s = %q, want %q", CodexWireAPIVar, got, CodexWireAPIResponses)
				}
			} else if got != "" {
				t.Errorf("unexpected %s = %q for %s", CodexWireAPIVar, got, tc.harness)
			}
		})
	}
}

func TestClaudeCodeEmptiesAPIKeyAndSetsAuthToken(t *testing.T) {
	env := HarnessClaudeCode.BaseURLEnv().Env("https://r.example", "tok")
	if env["ANTHROPIC_AUTH_TOKEN"] != "tok" {
		t.Fatalf("ANTHROPIC_AUTH_TOKEN = %q", env["ANTHROPIC_AUTH_TOKEN"])
	}
	if v, ok := env["ANTHROPIC_API_KEY"]; !ok || v != "" {
		t.Fatalf("ANTHROPIC_API_KEY = %q (present=%v), want empty and present", v, ok)
	}
}

func TestProbeSurfacesVersionAndAvailability(t *testing.T) {
	// A runner that returns canned version output makes the harness available.
	availableRun := func(version string) commandRunner {
		return func(ctx context.Context, name string, args ...string) ([]byte, error) {
			return []byte(name + " " + version), nil
		}
	}
	// A runner that fails (binary missing) makes the harness unavailable, with a
	// nil error: absence is not a fault.
	missingRun := func(ctx context.Context, name string, args ...string) ([]byte, error) {
		return nil, errors.New("exec: not found")
	}

	for _, tc := range []struct {
		harness Harness
		pin     string
	}{
		{HarnessClaudeCode, ClaudeCodeVersion},
		{HarnessCodex, CodexVersion},
		{HarnessOpencode, OpencodeVersion},
		{HarnessGrok, GrokVersion},
	} {
		t.Run(string(tc.harness), func(t *testing.T) {
			adapter, err := AdapterFor(tc.harness, WithCommandRunner(availableRun(tc.pin)))
			if err != nil {
				t.Fatal(err)
			}
			available, version, err := adapter.Probe(context.Background())
			if err != nil {
				t.Fatalf("Probe err = %v", err)
			}
			if !available {
				t.Fatalf("Probe available = false, want true")
			}
			if version != tc.pin {
				t.Fatalf("Probe version = %q, want pinned %q", version, tc.pin)
			}

			missing, _ := AdapterFor(tc.harness, WithCommandRunner(missingRun))
			available, version, err = missing.Probe(context.Background())
			if err != nil {
				t.Fatalf("Probe(missing) err = %v, want nil", err)
			}
			if available || version != "" {
				t.Fatalf("Probe(missing) = (%v, %q), want (false, \"\")", available, version)
			}
		})
	}
}

func TestStubAdaptersReturnNotImplemented(t *testing.T) {
	ctx := context.Background()
	for _, h := range []Harness{HarnessClaudeCode, HarnessCodex, HarnessGrok} {
		adapter, err := AdapterFor(h)
		if err != nil {
			t.Fatal(err)
		}
		if err := adapter.Install(ctx); !errors.Is(err, ErrNotImplemented) {
			t.Errorf("%s Install err = %v, want ErrNotImplemented", h, err)
		}
		if _, _, err := adapter.RunTask(ctx, Task{ID: "t1"}); !errors.Is(err, ErrNotImplemented) {
			t.Errorf("%s RunTask err = %v, want ErrNotImplemented", h, err)
		}
	}
}

func TestOpencodeInstallPinsVersionAndRunTaskStubbed(t *testing.T) {
	ctx := context.Background()
	var gotArgs []string
	run := func(ctx context.Context, name string, args ...string) ([]byte, error) {
		gotArgs = append([]string{name}, args...)
		return nil, nil
	}
	adapter := NewOpencodeAdapter(WithCommandRunner(run))
	if err := adapter.Install(ctx); err != nil {
		t.Fatalf("opencode Install err = %v", err)
	}
	wantSpec := "opencode-ai@" + OpencodeVersion
	if len(gotArgs) < 4 || gotArgs[0] != "npm" || gotArgs[len(gotArgs)-1] != wantSpec {
		t.Fatalf("install command = %v, want npm install -g %s", gotArgs, wantSpec)
	}
	// RunTask remains stubbed even for the most-fleshed adapter.
	if _, _, err := adapter.RunTask(ctx, Task{ID: "t1"}); !errors.Is(err, ErrNotImplemented) {
		t.Fatalf("opencode RunTask err = %v, want ErrNotImplemented", err)
	}
}

func TestAdapterForUnknown(t *testing.T) {
	if _, err := AdapterFor("gemini"); err == nil {
		t.Fatal("AdapterFor(unknown) err = nil, want error")
	}
}
