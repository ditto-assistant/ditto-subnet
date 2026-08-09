package main

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV8HarnessEnvironmentUsesPlatformAdapter(t *testing.T) {
	t.Setenv("HARNESS_EMBED_URL", "")
	env := harnessSandboxEnv(nil, protocol.BenchVersionV8, "session-route")
	if env["DITTOBENCH_PROVIDER"] != platformLockedProvider {
		t.Fatalf("provider = %q, want %q", env["DITTOBENCH_PROVIDER"], platformLockedProvider)
	}
	if env["DITTOBENCH_MODEL"] != llm.HarnessModelForVersion(protocol.BenchVersionV8) {
		t.Fatalf("wrong locked model: %q", env["DITTOBENCH_MODEL"])
	}
	if got := env["DITTOBENCH_INFERENCE_BASE_URL"]; got != "http://host.docker.internal:11436/v1/inference" {
		t.Fatalf("ticket gateway = %q", got)
	}
	if got := env["OLLAMA_BASE_URL"]; got != "http://host.docker.internal:11436" {
		t.Fatalf("embedding gateway = %q", got)
	}
	for _, baseURLKey := range append([]string{"CHUTES_BASE_URL"}, v8CompatBaseURLKeys...) {
		if got := env[baseURLKey]; got != "http://host.docker.internal:11436/v1/inference" {
			t.Fatalf("%s = %q, want ticket broker", baseURLKey, got)
		}
	}
	for _, key := range []string{"CHUTES_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"} {
		if got := env[key]; got != brokerPlaceholderKey {
			t.Fatalf("%s = %q, want non-secret broker placeholder", key, got)
		}
	}
	for key, value := range env {
		if strings.Contains(value, "session-route") {
			t.Fatalf("opaque session id leaked through %s", key)
		}
	}
}

func TestV8HarnessEnvironmentCannotBeOverridden(t *testing.T) {
	hostile := map[string]string{
		"DITTOBENCH_PROVIDER":           "openrouter",
		"DITTOBENCH_MODEL":              "attacker/model",
		"DITTOBENCH_INFERENCE_BASE_URL": "http://attacker.example/v1",
		"OLLAMA_BASE_URL":               "http://attacker.example",
		"CHUTES_API_KEY":                "attacker",
		"CHUTES_BASE_URL":               "http://attacker.example/v1",
		"OPENAI_API_KEY":                "attacker",
		"OPENAI_BASE_URL":               "http://attacker.example/v1",
		"OPENROUTER_API_KEY":            "attacker",
		"DITTOBENCH_DB":                 "/app/read-only.db",
		"BENIGN":                        "ok",
	}
	env := harnessSandboxEnv(hostile, protocol.BenchVersionV8, "session-route")
	if env["DITTOBENCH_PROVIDER"] != platformLockedProvider {
		t.Fatalf("provider lock escaped: %q", env["DITTOBENCH_PROVIDER"])
	}
	if env["DITTOBENCH_MODEL"] != llm.HarnessModelForVersion(protocol.BenchVersionV8) {
		t.Fatalf("model lock escaped: %q", env["DITTOBENCH_MODEL"])
	}
	if env["DITTOBENCH_DB"] != "/tmp/dittobench.db" {
		t.Fatalf("database path escaped: %q", env["DITTOBENCH_DB"])
	}
	if env["BENIGN"] != "ok" {
		t.Fatal("non-locked environment was not preserved")
	}
	for key, value := range env {
		if strings.Contains(value, "attacker") && key != "BENIGN" {
			t.Fatalf("hostile value survived in %s: %q", key, value)
		}
	}
}

func TestV8CompatibilityEnvironmentRoutesEveryAliasToBroker(t *testing.T) {
	env := harnessSandboxEnvForProvider(nil, protocol.BenchVersionV8, v8CompatLockedProvider, "session-route")
	if env["DITTOBENCH_PROVIDER"] != v8CompatLockedProvider {
		t.Fatalf("provider = %q, want %q", env["DITTOBENCH_PROVIDER"], v8CompatLockedProvider)
	}
	for _, baseURLKey := range append([]string{"CHUTES_BASE_URL"}, v8CompatBaseURLKeys...) {
		if got := env[baseURLKey]; got != "http://host.docker.internal:11436/v1/inference" {
			t.Fatalf("%s = %q, want ticket broker", baseURLKey, got)
		}
	}
	for _, key := range []string{"CHUTES_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"} {
		if got := env[key]; got != brokerPlaceholderKey {
			t.Fatalf("%s = %q, want non-secret broker placeholder", key, got)
		}
	}
}

func TestV9HarnessEnvironmentIsAnExactAllowlist(t *testing.T) {
	hostile := map[string]string{
		"BENIGN":                         "still-not-allowed",
		"DATASET_SEED":                   "778899001122334455",
		"RUN_SIZE":                       "full",
		"QUESTION_TYPE":                  "canary",
		"EXPECTED_ANSWER":                "oracle",
		"PATH":                           "/attacker/bin",
		"HOME":                           "/attacker/home",
		"AWS_SECRET_ACCESS_KEY":          "attacker",
		"GOOGLE_APPLICATION_CREDENTIALS": "/attacker/key.json",
	}
	env := harnessSandboxEnv(hostile, protocol.BenchVersionV9, "session-route")
	wantKeys := map[string]bool{
		"DITTOBENCH_PROVIDER": true, "DITTOBENCH_INFERENCE_BASE_URL": true,
		"CHUTES_BASE_URL": true, "CHUTES_API_KEY": true,
		"OPENAI_BASE_URL": true, "OPENAI_API_BASE": true,
		"OPENROUTER_BASE_URL": true, "OPENAI_API_KEY": true,
		"OPENROUTER_API_KEY": true, "DITTOBENCH_MODEL": true,
		"OLLAMA_BASE_URL": true, "DITTOBENCH_DB": true,
	}
	if len(env) != len(wantKeys) {
		t.Fatalf("v9 env has %d entries, exact allowlist has %d: %#v", len(env), len(wantKeys), env)
	}
	for key, value := range env {
		if !wantKeys[key] {
			t.Errorf("v9 env contains non-allowlisted key %q", key)
		}
		for _, forbidden := range []string{"778899001122334455", "full", "canary", "oracle", "attacker", "session-route"} {
			if strings.Contains(value, forbidden) {
				t.Errorf("v9 env %s leaks %q through value %q", key, forbidden, value)
			}
		}
	}
	if env["DITTOBENCH_MODEL"] != llm.HarnessModelForVersion(protocol.BenchVersionV9) {
		t.Fatalf("v9 locked model = %q", env["DITTOBENCH_MODEL"])
	}
}

func TestV8EnvironmentStillPreservesArbitraryCallerKeys(t *testing.T) {
	env := harnessSandboxEnv(map[string]string{"BENIGN": "legacy-value", "PATH": "/legacy/bin"}, protocol.BenchVersionV8)
	if env["BENIGN"] != "legacy-value" || env["PATH"] != "/legacy/bin" {
		t.Fatalf("v8 caller env compatibility changed: %#v", env)
	}
}

func TestSandboxRuntimeEnvOnlyLocksWritableDatabase(t *testing.T) {
	env := sandboxRuntimeEnv(map[string]string{
		"DITTOBENCH_DB":      "/app/read-only.db",
		"OPENROUTER_API_KEY": "practice-key",
	})
	if env["DITTOBENCH_DB"] != "/tmp/dittobench.db" {
		t.Fatalf("sandbox DB path = %q", env["DITTOBENCH_DB"])
	}
	if env["OPENROUTER_API_KEY"] != "practice-key" {
		t.Fatal("practice provider environment was unexpectedly changed")
	}
}
