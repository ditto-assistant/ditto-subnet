package inference

import (
	"testing"

	"github.com/ditto-assistant/model-relay/internal/postgres"
)

// These tests are the relay half of the sandbox egress proof
// (services/dittobench-api/docs/sandbox-egress.md), an activation prerequisite
// for the bench v13 block-bound confirmation seeds. The claim under test: the
// only egress a harness has is this relay, and on it neither the model nor the
// provider/route is miner-controllable, so "which upstream did I talk to" is
// not a covert channel and no request field can steer inference off the pinned
// OpenRouter upstream.

func TestLockedGrantModelIgnoresTheRequestedModelFromV7(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, nil))
	for _, version := range []int32{7, 9, 12, 13, 14} {
		grant := &postgres.InferenceGrant{
			BenchVersion:  version,
			AllowedModels: []byte(`["openai/gpt-oss-20b"]`),
		}
		for _, requested := range []string{"", "openai/gpt-oss-20b", "evil/other-model", "qwen/qwen3-32b"} {
			model, herr := deps.lockedGrantModel(grant, requested)
			if herr != nil {
				t.Fatalf("v%d requested %q: %v", version, requested, herr)
			}
			if model != "openai/gpt-oss-20b" {
				t.Fatalf("v%d requested %q served %q, want the ticket-locked model", version, requested, model)
			}
		}
		// A grant with no locked model cannot fall back to the request.
		bare := &postgres.InferenceGrant{BenchVersion: version, AllowedModels: []byte(`[]`)}
		if _, herr := deps.lockedGrantModel(bare, "evil/other-model"); herr == nil || herr.status != 409 {
			t.Fatalf("v%d grant without a locked model must be 409, got %v", version, herr)
		}
	}
}

func TestLegacyGrantModelMustBeOnTheFixedAllowlist(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, map[string]string{
		"DITTO_INFERENCE_ALLOWED_MODELS": "qwen/qwen3-32b,openai/gpt-oss-20b",
	}))
	legacy := &postgres.InferenceGrant{BenchVersion: 6, AllowedModels: []byte(`["anything"]`)}
	if model, herr := deps.lockedGrantModel(legacy, "qwen/qwen3-32b"); herr != nil || model != "qwen/qwen3-32b" {
		t.Fatalf("allowlisted legacy model refused: %q %v", model, herr)
	}
	if _, herr := deps.lockedGrantModel(legacy, "evil/other-model"); herr == nil || herr.status != 403 {
		t.Fatalf("legacy model off the fixed allowlist must be 403, got %v", herr)
	}
}

func TestLockedUpstreamPayloadDropsMinerRoutingControls(t *testing.T) {
	payload := parsePayload(t, `{
		"model":"evil/other-model",
		"provider":{"only":["evil-provider"],"order":["evil-provider"],"allow_fallbacks":false},
		"route":"fallback",
		"preset":"@preset/evil",
		"messages":[{"role":"user","content":"hi"}]
	}`)
	upstream, herr := lockedUpstreamPayload(payload, v7Model, 40, 13)
	if herr != nil {
		t.Fatalf("locked payload: %v", herr)
	}
	if upstream["model"] != v7Model {
		t.Fatalf("model must be the ticket-locked one, got %v", upstream["model"])
	}
	for _, gone := range []string{"provider", "route", "preset"} {
		if _, present := upstream[gone]; present {
			t.Fatalf("miner routing control %q must never reach the upstream", gone)
		}
	}
}

func TestRoutingFieldsThatCouldSteerTheUpstreamAreRefusedOrDropped(t *testing.T) {
	// Refused with a legible error: a request carrying them is rejected.
	for _, field := range []string{"models", "transforms", "plugins", "web_search_options"} {
		if _, refused := refusedRequestFields[field]; !refused {
			t.Fatalf("%q must be a refused request field", field)
		}
	}
	// Dropped silently: forwarded requests never carry them upstream.
	for _, field := range []string{"provider", "route", "preset"} {
		if _, dropped := droppedRequestFields[field]; !dropped {
			t.Fatalf("%q must be a dropped request field", field)
		}
		if _, forwarded := forwardedRequestFields[field]; forwarded {
			t.Fatalf("%q must not be forwarded", field)
		}
	}
	// And nothing shaped like an endpoint override is forwardable at all.
	for _, field := range []string{"base_url", "api_base", "endpoint", "upstream", "url"} {
		if isAllowedRequestField(field) {
			t.Fatalf("%q must not be an allowed request field", field)
		}
	}
}
