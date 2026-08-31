package main

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestConfigurationIsDefaultOffAndPinsTheUnixSocket(t *testing.T) {
	disabled := func(string) string { return "" }
	if _, err := configurationFromEnvironment(defaultSocketPath, disabled); err == nil {
		t.Fatal("disabled scorer configuration was accepted")
	}
	if _, err := configurationFromEnvironment("/tmp/scorer.sock", disabled); err == nil {
		t.Fatal("alternate socket path was accepted")
	}
	enabled := func(key string) string {
		switch key {
		case enableEnvironment:
			return "true"
		case sourceGatewayEnvironment:
			return "192.0.2.44"
		default:
			return ""
		}
	}
	config, err := configurationFromEnvironment(defaultSocketPath, enabled)
	if err != nil || !config.enabled || config.socketPath != defaultSocketPath || config.sourceGateway != "192.0.2.44" {
		t.Fatalf("config=%#v err=%v", config, err)
	}
}

func TestRuntimeImageDigestIsAnExactImmutableAuthority(t *testing.T) {
	if !sha256ImageDigest.MatchString("sha256:" + strings.Repeat("a", 64)) {
		t.Fatal("full lowercase digest was rejected")
	}
	for _, value := range []string{"", "latest", "sha256:" + strings.Repeat("A", 64), "sha256:" + strings.Repeat("a", 63)} {
		if sha256ImageDigest.MatchString(value) {
			t.Fatalf("mutable or malformed digest was accepted: %q", value)
		}
	}
}

func TestScorerSourceGatewayIsPinnedForCandidateRouting(t *testing.T) {
	for _, value := range []string{"", "127.0.0.1", "0.0.0.0", "::1", "169.254.1.2"} {
		_, err := sourceGatewayFromEnvironment(func(key string) string {
			if key == sourceGatewayEnvironment {
				return value
			}
			return ""
		})
		if err == nil {
			t.Fatalf("unsafe source gateway %q was accepted", value)
		}
	}

	gateway, err := sourceGatewayFromEnvironment(func(key string) string {
		if key == sourceGatewayEnvironment {
			return "192.0.2.44"
		}
		return ""
	})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := sourceListenerAddress(gateway), "192.0.2.44:11438"; got != want {
		t.Fatalf("source listener address=%q want=%q", got, want)
	}
	if got, want := sourcePublicBaseURL(), "http://host.docker.internal:11438"; got != want {
		t.Fatalf("source public URL=%q want=%q", got, want)
	}
}

func TestScorerLoadsTheCanonicalLockedPolicy(t *testing.T) {
	path, err := filepath.Abs(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_inference_policy_locked_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := loadPolicy(path); err != nil {
		t.Fatalf("loadPolicy(%q): %v", path, err)
	}
}

func TestControlMuxOnlyExposesConstantHealth(t *testing.T) {
	mux := controlMux(nil)
	health := httptest.NewRecorder()
	mux.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusNoContent || health.Body.Len() != 0 || health.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("health status=%d body=%q headers=%v", health.Code, health.Body.String(), health.Header())
	}
	unknown := httptest.NewRecorder()
	mux.ServeHTTP(unknown, httptest.NewRequest(http.MethodPost, "/v1/coding/supervisor/start", nil))
	if unknown.Code != http.StatusNotFound {
		t.Fatalf("unexpected control route status=%d", unknown.Code)
	}
}
