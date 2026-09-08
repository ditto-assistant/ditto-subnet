package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
)

// TestRouterHarnessSandboxEnvInvertsTheModelLock asserts the router-track env is
// the inverse of harnessSandboxEnvForProvider: no model lock, no lockedEnvKeys
// filter, and per-harness base-URL/token vars pointing at the miner's router.
func TestRouterHarnessSandboxEnvInvertsTheModelLock(t *testing.T) {
	const (
		routerURL = "https://miner-router.example/api"
		placehold = "router-ticket"
	)
	// A caller-supplied var that lockedEnvKeys WOULD strip on the locked path, plus
	// a benign one. Both must survive here.
	reqEnv := map[string]string{
		"OPENROUTER_API_KEY": "miner-owned-key",
		"BENIGN":             "kept",
	}
	env := routerHarnessSandboxEnv(reqEnv, routerURL, placehold)

	// 1. The model lock is NOT applied: neither locked var is injected.
	if _, ok := env["DITTOBENCH_MODEL"]; ok {
		t.Error("router env must not inject DITTOBENCH_MODEL")
	}
	if _, ok := env["DITTOBENCH_INFERENCE_BASE_URL"]; ok {
		t.Error("router env must not inject DITTOBENCH_INFERENCE_BASE_URL")
	}

	// 2. lockedEnvKeys is NOT applied: a caller-supplied locked key survives.
	if !lockedEnvKeys["OPENROUTER_API_KEY"] {
		t.Fatal("precondition: OPENROUTER_API_KEY should be a locked key on the locked path")
	}
	if env["OPENROUTER_API_KEY"] != "miner-owned-key" {
		t.Errorf("caller-supplied locked var was filtered: %q", env["OPENROUTER_API_KEY"])
	}
	if env["BENIGN"] != "kept" {
		t.Errorf("benign caller var not preserved: %q", env["BENIGN"])
	}

	// 3. Claude Code: ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN set, API key empty.
	if env["ANTHROPIC_BASE_URL"] != routerURL {
		t.Errorf("ANTHROPIC_BASE_URL = %q, want router %q", env["ANTHROPIC_BASE_URL"], routerURL)
	}
	if env["ANTHROPIC_AUTH_TOKEN"] != placehold {
		t.Errorf("ANTHROPIC_AUTH_TOKEN = %q, want %q", env["ANTHROPIC_AUTH_TOKEN"], placehold)
	}
	if v, ok := env["ANTHROPIC_API_KEY"]; !ok || v != "" {
		t.Errorf("ANTHROPIC_API_KEY = %q (present=%v), want present and empty", v, ok)
	}

	// 4. Codex: OPENAI_BASE_URL at the router with the Responses wire_api marker.
	if env["OPENAI_BASE_URL"] != routerURL {
		t.Errorf("OPENAI_BASE_URL = %q, want router %q", env["OPENAI_BASE_URL"], routerURL)
	}
	if env[routerharness.CodexWireAPIVar] != routerharness.CodexWireAPIResponses {
		t.Errorf("%s = %q, want %q", routerharness.CodexWireAPIVar, env[routerharness.CodexWireAPIVar], routerharness.CodexWireAPIResponses)
	}
	// opencode + Grok share OPENAI_BASE_URL and the placeholder OPENAI_API_KEY.
	if env["OPENAI_API_KEY"] != placehold {
		t.Errorf("OPENAI_API_KEY = %q, want placeholder %q", env["OPENAI_API_KEY"], placehold)
	}
}
