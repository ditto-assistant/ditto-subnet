package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

func TestProviderAPIKeyReadsPrivateFileWithoutEnvironment(t *testing.T) {
	t.Setenv("PROVIDER_RELAY_API_KEY", "")
	t.Setenv("OPENROUTER_API_KEY", "")
	path := filepath.Join(t.TempDir(), "provider.key")
	if err := os.WriteFile(path, []byte("host-key"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PROVIDER_RELAY_API_KEY_FILE", path)
	key, err := providerAPIKey()
	if err != nil || key != "host-key" {
		t.Fatalf("providerAPIKey() = (%q, %v)", key, err)
	}
}

func TestProviderAPIKeyRejectsBroadFilePermissions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "provider.key")
	if err := os.WriteFile(path, []byte("host-key"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PROVIDER_RELAY_API_KEY_FILE", path)
	if _, err := providerAPIKey(); err == nil {
		t.Fatal("providerAPIKey accepted a broadly readable key file")
	}
}

func TestRelayPinsModelProviderAndKey(t *testing.T) {
	var got map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer host-key" {
			t.Fatalf("upstream authorization = %q", r.Header.Get("Authorization"))
		}
		if got := r.Header.Get("HTTP-Referer"); got != providercert.AppReferer {
			t.Fatalf("HTTP-Referer = %q", got)
		}
		if got := r.Header.Get("X-OpenRouter-Title"); got != providercert.AppTitle {
			t.Fatalf("X-OpenRouter-Title = %q", got)
		}
		if got := r.Header.Get("X-Title"); got != providercert.AppTitle {
			t.Fatalf("X-Title = %q", got)
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"provider":"DeepInfra","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14,"cost":0.001}}`))
	}))
	defer upstream.Close()
	r := &relay{upstream: upstream.URL + "?openrouter.ai", apiKey: "host-key", model: providercert.DefaultModel, provider: "deepinfra", client: &http.Client{Timeout: time.Second}}
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"other","stream":true,"messages":[{"role":"user","content":"hi"}]}`))
	request.Header.Set("Authorization", "Bearer attacker-key")
	response := httptest.NewRecorder()
	r.handle(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d", response.Code)
	}
	if got["model"] != "qwen/qwen3-32b" || got["stream"] != false {
		t.Fatalf("model/stream not pinned: %#v", got)
	}
	provider := got["provider"].(map[string]any)
	if provider["allow_fallbacks"] != false {
		t.Fatalf("fallbacks not disabled: %#v", provider)
	}
	only := provider["only"].([]any)
	if len(only) != 1 || only[0] != "deepinfra" {
		t.Fatalf("provider pin = %#v", provider)
	}
	stats := r.statsSnapshot()
	if stats.Requests != 1 || stats.PromptTokens != 10 || stats.CompletionTokens != 4 || stats.CostUSD != 0.001 {
		t.Fatalf("stats = %#v", stats)
	}
	if stats.ResponseProviders["DeepInfra"] != 1 || stats.FinishReasons["tool_calls"] != 1 {
		t.Fatalf("attribution stats = %#v", stats)
	}
	r.handleStatsReset(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/stats/reset", nil))
	if reset := r.statsSnapshot(); reset.Requests != 0 || reset.CostUSD != 0 {
		t.Fatalf("reset stats = %#v", reset)
	}
}

func TestDirectRelayPinsModelWithoutOpenRouterRouting(t *testing.T) {
	var got map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("HTTP-Referer") != "" || r.Header.Get("X-Title") != "" {
			t.Fatalf("direct request received OpenRouter attribution")
		}
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
	}))
	defer upstream.Close()
	r := &relay{upstream: upstream.URL, apiKey: "host-key", model: "Qwen/Qwen3.6-27B-TEE", client: &http.Client{Timeout: time.Second}}
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"other","provider":{"only":["attacker"]}}`))
	response := httptest.NewRecorder()
	r.handle(response, request)
	if response.Code != http.StatusOK || got["model"] != "Qwen/Qwen3.6-27B-TEE" || got["provider"] != nil {
		t.Fatalf("status=%d body=%#v", response.Code, got)
	}
}

func TestGPTOSSRelayPreservesAgentReasoningEffort(t *testing.T) {
	var got map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatal(err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}`))
	}))
	defer upstream.Close()
	r := &relay{upstream: upstream.URL, apiKey: "host-key", model: "openai/gpt-oss-20b", client: &http.Client{Timeout: time.Second}}
	request := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"other","reasoning_effort":"high"}`))
	response := httptest.NewRecorder()
	r.handle(response, request)
	if response.Code != http.StatusOK || got["model"] != "openai/gpt-oss-20b" {
		t.Fatalf("status=%d body=%#v", response.Code, got)
	}
	if _, present := got["reasoning_effort"]; present {
		t.Fatalf("flat reasoning alias escaped normalization: %#v", got)
	}
	reasoning := got["reasoning"].(map[string]any)
	if reasoning["effort"] != "high" || reasoning["exclude"] != true {
		t.Fatalf("reasoning = %#v", reasoning)
	}
}
