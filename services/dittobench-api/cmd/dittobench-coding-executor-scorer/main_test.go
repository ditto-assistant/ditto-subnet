package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestConfigurationIsDefaultOffAndPinsTheUnixSocket(t *testing.T) {
	if _, err := configurationFromEnvironment(defaultSocketPath, func(string) string { return "" }); err == nil {
		t.Fatal("disabled scorer configuration was accepted")
	}
	if _, err := configurationFromEnvironment("/tmp/scorer.sock", func(string) string { return "true" }); err == nil {
		t.Fatal("alternate socket path was accepted")
	}
	config, err := configurationFromEnvironment(defaultSocketPath, func(string) string { return "true" })
	if err != nil || !config.enabled || config.socketPath != defaultSocketPath {
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
