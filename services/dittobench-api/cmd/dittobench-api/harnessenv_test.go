package main

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestHarnessSandboxEnvForcesModel: the frozen model + provider are forced and
// no OpenRouter key is forwarded. HARNESS_MODEL is set to a bogus id to prove
// the frozen model wins (it is not env-tunable).
func TestHarnessSandboxEnvForcesModel(t *testing.T) {
	t.Setenv("HARNESS_MODEL", "qwen/qwen2.5-72b-instruct")
	env := harnessSandboxEnv(nil, protocol.BenchVersionV6)
	if _, ok := env["OPENROUTER_API_KEY"]; ok {
		t.Fatal("locked path must NOT forward the OpenRouter key")
	}
	if env["DITTOBENCH_MODEL"] != llm.LockedHarnessModel {
		t.Fatalf("locked model = %q, want the frozen %q", env["DITTOBENCH_MODEL"], llm.LockedHarnessModel)
	}
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider {
		t.Fatalf("locked provider = %q, want the frozen %q", env["DITTOBENCH_PROVIDER"], legacyLockedProvider)
	}
	if env["DITTOBENCH_DB"] != "/tmp/dittobench.db" {
		t.Fatalf("sandbox DB path = %q, want bounded writable tmpfs", env["DITTOBENCH_DB"])
	}
}

func TestSandboxRuntimeEnvLocksWritableDBWithoutChangingProvider(t *testing.T) {
	env := sandboxRuntimeEnv(map[string]string{
		"DITTOBENCH_DB":      "/app/read-only.db",
		"OPENROUTER_API_KEY": "practice-key",
	})
	if env["DITTOBENCH_DB"] != "/tmp/dittobench.db" {
		t.Fatalf("sandbox DB path = %q, want bounded writable tmpfs", env["DITTOBENCH_DB"])
	}
	if env["OPENROUTER_API_KEY"] != "practice-key" {
		t.Fatal("practice provider env was unexpectedly changed")
	}
}

// TestHarnessSandboxEnvRelayRouting: chat routes to the chat gateway via the
// frozen chutes provider while embeddings keep hitting the local Ollama, and no
// real key enters the sandbox.
func TestHarnessSandboxEnvRelayRouting(t *testing.T) {
	t.Setenv("HARNESS_GATEWAY_URL", "http://host.docker.internal:11435")
	t.Setenv("HARNESS_EMBED_URL", "http://host.docker.internal:11434")
	env := harnessSandboxEnv(nil, protocol.BenchVersionV6)
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider || env["DITTOBENCH_MODEL"] != llm.LockedHarnessModel {
		t.Fatalf("locked provider/model wrong: %v", env)
	}
	if env["CHUTES_BASE_URL"] != "http://host.docker.internal:11435" {
		t.Fatalf("chat must route to the relay: %q", env["CHUTES_BASE_URL"])
	}
	if env["OLLAMA_BASE_URL"] != "http://host.docker.internal:11434" {
		t.Fatalf("embeddings must keep hitting local Ollama: %q", env["OLLAMA_BASE_URL"])
	}
	if env["CHUTES_API_KEY"] != "relay" {
		t.Fatalf("sandbox must hold only the placeholder key, got %q", env["CHUTES_API_KEY"])
	}
	if _, ok := env["OPENROUTER_API_KEY"]; ok {
		t.Fatal("locked path must not forward the OpenRouter key")
	}
}

func TestTicketInferenceEnvContainsNoSessionCapability(t *testing.T) {
	t.Setenv("HARNESS_EMBED_URL", "")
	env := harnessSandboxEnv(nil, protocol.BenchVersionV7, "secret-session-route")
	if got := env["DITTOBENCH_INFERENCE_BASE_URL"]; got != "http://host.docker.internal:11436/v1/inference" {
		t.Fatalf("ticket gateway = %q", got)
	}
	// v7 deliberately selects the generic OpenAI-compatible adapter, because it
	// is the only selector every shipped harness implements. The paired key is
	// the non-secret placeholder, never a real credential.
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider {
		t.Fatalf("v7 provider = %q, want %q", env["DITTOBENCH_PROVIDER"], legacyLockedProvider)
	}
	if got := env["CHUTES_BASE_URL"]; got != "http://host.docker.internal:11436/v1/inference" {
		t.Fatalf("v7 chutes base url = %q, want the ticket broker", got)
	}
	if env["CHUTES_API_KEY"] != brokerPlaceholderKey {
		t.Fatalf("v7 chutes key = %q, want the non-secret placeholder", env["CHUTES_API_KEY"])
	}
	for key, value := range env {
		if key != "BENIGN" && strings.Contains(value, "secret-session-route") {
			t.Fatalf("session capability leaked through %s", key)
		}
	}
	if env["DITTOBENCH_MODEL"] != llm.V7HarnessModel {
		t.Fatalf("v7 model = %q, want %q", env["DITTOBENCH_MODEL"], llm.V7HarnessModel)
	}
	if got := env["OLLAMA_BASE_URL"]; got != "http://host.docker.internal:11436" {
		t.Fatalf("v7 embeddings must use the source-bound operation broker, got %q", got)
	}
}

func TestLegacyScoredSessionAlsoBrokersEmbeddingOperations(t *testing.T) {
	env := harnessSandboxEnv(nil, protocol.BenchVersionV6, "legacy-session")
	if got := env["OLLAMA_BASE_URL"]; got != "http://host.docker.internal:11436" {
		t.Fatalf("v6 scored embeddings bypassed the source-bound broker: %q", got)
	}
	if got := env["CHUTES_BASE_URL"]; got != "http://host.docker.internal:11436/v1/inference" {
		t.Fatalf("v6 scored chat did not use the source-bound broker: %q", got)
	}
}

// TestHarnessSandboxEnvLockCannotBeOverridden pins the security-critical
// invariant: a malicious req.Env cannot escape the locked model by setting the
// OpenRouter key, swapping the model id, or redirecting the gateway.
func TestHarnessSandboxEnvLockCannotBeOverridden(t *testing.T) {
	hostile := map[string]string{
		"OPENROUTER_API_KEY":            "sk-attacker",
		"DITTOBENCH_MODEL":              "openai/gpt-4o",
		"DITTOBENCH_PROVIDER":           "openrouter",
		"OLLAMA_BASE_URL":               "http://attacker.example/v1",
		"CHUTES_API_KEY":                "cpk-attacker",
		"CHUTES_BASE_URL":               "http://attacker.example/v1",
		"OPENAI_API_KEY":                "sk-attacker",
		"OPENAI_BASE_URL":               "http://attacker.example/v1",
		"DITTOBENCH_INFERENCE_BASE_URL": "http://attacker.example/v1",
		"DITTOBENCH_DB":                 "/app/attacker.db",
		"BENIGN":                        "ok", // non-locked keys still pass through
	}
	env := harnessSandboxEnv(hostile, protocol.BenchVersionV6)
	// Keys with no locked value must be dropped entirely; the OpenAI + OpenRouter
	// selectors are never set on the locked path.
	for _, k := range []string{"OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"} {
		if _, ok := env[k]; ok {
			t.Fatalf("req.Env must not be able to set %s under lock", k)
		}
	}
	// The chutes selectors are set by the lock, so the attacker's values must be
	// overwritten with the locked placeholder / gateway, never survive.
	if env["CHUTES_API_KEY"] != "relay" {
		t.Fatalf("req.Env kept a real Chutes key under lock: %q", env["CHUTES_API_KEY"])
	}
	if env["CHUTES_BASE_URL"] == "http://attacker.example/v1" {
		t.Fatal("req.Env redirected the chat gateway under lock")
	}
	if env["DITTOBENCH_MODEL"] != llm.LockedHarnessModel {
		t.Fatalf("req.Env overrode the locked model: %q", env["DITTOBENCH_MODEL"])
	}
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider {
		t.Fatalf("req.Env overrode the locked provider: %q", env["DITTOBENCH_PROVIDER"])
	}
	if env["OLLAMA_BASE_URL"] == "http://attacker.example/v1" {
		t.Fatal("req.Env redirected the gateway URL under lock")
	}
	if env["BENIGN"] != "ok" {
		t.Fatalf("non-locked caller env should still pass through, got %q", env["BENIGN"])
	}
	if env["DITTOBENCH_DB"] != "/tmp/dittobench.db" {
		t.Fatalf("req.Env overrode the locked DB path: %q", env["DITTOBENCH_DB"])
	}
}

// TestV7BYOKCompatSelectorsPointAtTheBroker pins the backwards-compatibility
// contract: a harness written against the pre-v7 "bring your own key" shape
// reads a conventional OpenAI/OpenRouter selector, so every one of those
// selectors must resolve to the same ticket-bound broker gateway that
// DITTOBENCH_INFERENCE_BASE_URL resolves to. No miner change, one route.
func TestV7BYOKCompatSelectorsPointAtTheBroker(t *testing.T) {
	env := harnessSandboxEnv(nil, protocol.BenchVersionV7, "session-route")
	gateway := env["DITTOBENCH_INFERENCE_BASE_URL"]
	if gateway == "" {
		t.Fatal("v7 must set the platform gateway")
	}
	for _, key := range byokCompatEnvKeys {
		if env[key] != gateway {
			t.Fatalf("%s = %q, want the broker gateway %q", key, env[key], gateway)
		}
	}
	for _, key := range []string{"OPENAI_API_KEY", "OPENROUTER_API_KEY"} {
		if env[key] != brokerPlaceholderKey {
			t.Fatalf("%s = %q, want the non-secret placeholder", key, env[key])
		}
	}
}

// TestV7CompatSelectorsCannotBeRedirected pins that the new aliases joined the
// lock: a hostile req.Env may not repoint them at an attacker host, which would
// otherwise be a way around the locked model.
func TestV7CompatSelectorsCannotBeRedirected(t *testing.T) {
	hostile := map[string]string{
		"OPENAI_BASE_URL":     "http://attacker.example/v1",
		"OPENAI_API_BASE":     "http://attacker.example/v1",
		"OPENROUTER_BASE_URL": "http://attacker.example/v1",
		"OPENROUTER_API_KEY":  "sk-attacker",
		"OPENAI_API_KEY":      "sk-attacker",
	}
	env := harnessSandboxEnv(hostile, protocol.BenchVersionV7, "session-route")
	gateway := env["DITTOBENCH_INFERENCE_BASE_URL"]
	for _, key := range byokCompatEnvKeys {
		if env[key] != gateway {
			t.Fatalf("req.Env redirected %s to %q", key, env[key])
		}
	}
	for _, key := range []string{"OPENAI_API_KEY", "OPENROUTER_API_KEY"} {
		if env[key] != brokerPlaceholderKey {
			t.Fatalf("req.Env kept a real key in %s: %q", key, env[key])
		}
	}
}

// TestLegacyVersionsKeepNoCompatSelectors pins that the aliases are a v7-only
// addition: historical versions replay exactly as they were scored.
func TestLegacyVersionsKeepNoCompatSelectors(t *testing.T) {
	env := harnessSandboxEnv(nil, protocol.BenchVersionV6, "legacy-session")
	for _, key := range append(append([]string{}, byokCompatEnvKeys...), "OPENAI_API_KEY", "OPENROUTER_API_KEY") {
		if _, ok := env[key]; ok {
			t.Fatalf("v6 env gained %s; historical replay must not change", key)
		}
	}
}

// TestV7SelectsTheUniversalOpenAICompatibleAdapter pins the selector that
// actually reaches the deployed fleet. Every harness submitted against this
// benchmark implements a `chutes` arm reading CHUTES_BASE_URL verbatim; only
// post-cutover rebases implement `platform`. Selecting `platform` sent every
// older image down its compiled-in OpenRouter default.
func TestV7SelectsTheUniversalOpenAICompatibleAdapter(t *testing.T) {
	env := harnessSandboxEnv(nil, protocol.BenchVersionV7, "session-route")
	const gateway = "http://host.docker.internal:11436/v1/inference"
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider {
		t.Fatalf("provider = %q, want %q", env["DITTOBENCH_PROVIDER"], legacyLockedProvider)
	}
	if env["CHUTES_BASE_URL"] != gateway {
		t.Fatalf("CHUTES_BASE_URL = %q, want %q", env["CHUTES_BASE_URL"], gateway)
	}
	if env["DITTOBENCH_INFERENCE_BASE_URL"] != gateway {
		t.Fatalf("the documented v7 selector must stay set: %q", env["DITTOBENCH_INFERENCE_BASE_URL"])
	}
	if env["CHUTES_API_KEY"] != brokerPlaceholderKey || env["OPENAI_API_KEY"] != brokerPlaceholderKey {
		t.Fatal("compat key selectors must carry the non-secret placeholder")
	}
	if env["DITTOBENCH_MODEL"] != llm.V7HarnessModel {
		t.Fatalf("model = %q, want the v7 locked model", env["DITTOBENCH_MODEL"])
	}
}

// TestV7EmbeddingSelectorConcatenatesOntoTheBrokerRoute pins the embeddings
// half. The harness embedder appends "/api/embed" to OLLAMA_BASE_URL verbatim
// (it does NOT append /v1, unlike the ollama *chat* arm), so the injected value
// must be the broker root for string concatenation to land on the real route.
func TestV7EmbeddingSelectorConcatenatesOntoTheBrokerRoute(t *testing.T) {
	env := harnessSandboxEnv(nil, protocol.BenchVersionV7, "session-route")
	base := env["OLLAMA_BASE_URL"]
	if base != "http://host.docker.internal:11436" {
		t.Fatalf("OLLAMA_BASE_URL = %q, want the broker root", base)
	}
	if got := strings.TrimSuffix(base, "/") + embeddingAPIPath; got != "http://host.docker.internal:11436/api/embed" {
		t.Fatalf("embedder would POST to %q, which is not the broker embed route", got)
	}
}

// TestV7LockCannotBeOverridden verifies the lock rather than trusting the
// declaration: a hostile req.Env must not be able to select `openrouter` and
// opt back out of ticket metering, nor repoint chat or embeddings elsewhere.
func TestV7LockCannotBeOverridden(t *testing.T) {
	hostile := map[string]string{
		"DITTOBENCH_PROVIDER":           "openrouter",
		"DITTOBENCH_MODEL":              "qwen/qwen3-32b",
		"CHUTES_BASE_URL":               "http://attacker.example/v1",
		"CHUTES_API_KEY":                "cpk-attacker",
		"OLLAMA_BASE_URL":               "http://attacker.example",
		"OPENROUTER_API_KEY":            "sk-attacker",
		"OPENAI_API_KEY":                "sk-attacker",
		"OPENAI_BASE_URL":               "http://attacker.example/v1",
		"OPENAI_API_BASE":               "http://attacker.example/v1",
		"OPENROUTER_BASE_URL":           "http://attacker.example/v1",
		"DITTOBENCH_INFERENCE_BASE_URL": "http://attacker.example/v1",
		"BENIGN":                        "ok",
	}
	env := harnessSandboxEnv(hostile, protocol.BenchVersionV7, "session-route")
	if env["DITTOBENCH_PROVIDER"] != legacyLockedProvider {
		t.Fatalf("req.Env escaped the provider lock: %q", env["DITTOBENCH_PROVIDER"])
	}
	if env["DITTOBENCH_MODEL"] != llm.V7HarnessModel {
		t.Fatalf("req.Env escaped the model lock: %q", env["DITTOBENCH_MODEL"])
	}
	for key, value := range env {
		if strings.Contains(value, "attacker.example") {
			t.Fatalf("req.Env redirected %s to %q", key, value)
		}
		if strings.Contains(value, "attacker") && key != "BENIGN" {
			t.Fatalf("req.Env kept an attacker value in %s: %q", key, value)
		}
	}
	if env["BENIGN"] != "ok" {
		t.Fatal("non-locked keys must still pass through")
	}
}
