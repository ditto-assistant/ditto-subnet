package main

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
	"github.com/ditto-assistant/dittobench-api/internal/codingtransport"
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
		case validatorHotkeyEnvironment:
			return "5" + strings.Repeat("A", 47)
		default:
			return ""
		}
	}
	config, err := configurationFromEnvironment(defaultSocketPath, enabled)
	if err != nil || !config.enabled || config.socketPath != defaultSocketPath ||
		config.sourceGateway != "192.0.2.44" || config.validatorHotkey == "" {
		t.Fatalf("config=%#v err=%v", config, err)
	}
	malformed := func(key string) string {
		if key == enableEnvironment {
			return "true"
		}
		if key == sourceGatewayEnvironment {
			return "192.0.2.44"
		}
		if key == validatorHotkeyEnvironment {
			return "not-an-ss58-hotkey"
		}
		return ""
	}
	if _, err := configurationFromEnvironment(defaultSocketPath, malformed); err == nil {
		t.Fatal("malformed validator authority was accepted")
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

func TestMTLSTransportIsDefaultOffAndRequiresPrivateBindAndCredentials(t *testing.T) {
	if _, err := mtlsConfigurationFromEnvironment(func(string) string { return "" }); err == nil {
		t.Fatal("disabled mTLS transport was accepted")
	}
	valid := func(key string) string {
		switch key {
		case mtlsEnableEnvironment:
			return "true"
		case mtlsBindEnvironment:
			return "10.30.0.8"
		case validatorHotkeyEnvironment:
			return "5" + strings.Repeat("A", 47)
		case mtlsSourceCIDREnvironment:
			return "10.30.0.9/32"
		case "CREDENTIALS_DIRECTORY":
			return "/run/credentials/ditto-coding-executor-mtls"
		default:
			return ""
		}
	}
	config, err := mtlsConfigurationFromEnvironment(valid)
	if err != nil || config.bindAddress != "10.30.0.8" || config.sourceCIDR != "10.30.0.9/32" ||
		config.validatorHotkey == "" {
		t.Fatalf("config=%#v err=%v", config, err)
	}
	public := func(key string) string {
		if key == mtlsBindEnvironment {
			return "203.0.113.8"
		}
		return valid(key)
	}
	if _, err := mtlsConfigurationFromEnvironment(public); err == nil {
		t.Fatal("public mTLS bind address was accepted")
	}
	broadSource := func(key string) string {
		if key == mtlsSourceCIDREnvironment {
			return "10.30.0.0/24"
		}
		return valid(key)
	}
	if _, err := mtlsConfigurationFromEnvironment(broadSource); err == nil {
		t.Fatal("broad validator source range was accepted")
	}
}

func TestArtifactCanaryConfigurationIsSeparateAndDefaultOff(t *testing.T) {
	if _, err := artifactCanaryConfigurationFromEnvironment(func(string) string { return "" }); err == nil {
		t.Fatal("disabled artifact canary was accepted")
	}
	enabled := func(key string) string {
		switch key {
		case artifactCanaryEnvironment:
			return "true"
		case "CREDENTIALS_DIRECTORY":
			return "/run/credentials/coding-artifact-canary"
		default:
			return ""
		}
	}
	config, err := artifactCanaryConfigurationFromEnvironment(enabled)
	if err != nil || config.capabilityPath != "/run/credentials/coding-artifact-canary/artifact-capability.json" {
		t.Fatalf("config=%#v err=%v", config, err)
	}
	invalid := func(key string) string {
		if key == artifactCanaryEnvironment {
			return "true"
		}
		if key == "CREDENTIALS_DIRECTORY" {
			return "/tmp/credentials"
		}
		return ""
	}
	if _, err := artifactCanaryConfigurationFromEnvironment(invalid); err == nil {
		t.Fatal("alternate artifact credential directory was accepted")
	}
	if boolCount(true, true, false) != 2 || boolCount(false, false, false) != 0 {
		t.Fatal("scorer mode count is invalid")
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

func TestControlMuxExposesConstantHealthAndAuthorityFreeReadiness(t *testing.T) {
	mux := controlMux(nil)
	health := httptest.NewRecorder()
	mux.ServeHTTP(health, httptest.NewRequest(http.MethodGet, "/health", nil))
	if health.Code != http.StatusNoContent || health.Body.Len() != 0 || health.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("health status=%d body=%q headers=%v", health.Code, health.Body.String(), health.Header())
	}
	notReady := httptest.NewRecorder()
	mux.ServeHTTP(notReady, httptest.NewRequest(http.MethodGet, codingtransport.ReadinessPath, nil))
	if notReady.Code != http.StatusServiceUnavailable {
		t.Fatalf("unconstructed readiness status=%d body=%q", notReady.Code, notReady.Body.String())
	}
	readyMux := controlMux(new(codingcontrol.Ingress))
	ready := httptest.NewRecorder()
	readyMux.ServeHTTP(ready, httptest.NewRequest(http.MethodGet, codingtransport.ReadinessPath, nil))
	expected := `{"schema":"dittobench-coding-executor-readiness-v1","coding_contract_version":1,"weight_eligible":false,"transport":"mtls","supervisor_ready":true,"publication_ready":true,"ticket_authority_used":false}` + "\n"
	if ready.Code != http.StatusOK || ready.Body.String() != expected ||
		ready.Header().Get("Cache-Control") != "no-store" ||
		ready.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("ready status=%d body=%q headers=%v", ready.Code, ready.Body.String(), ready.Header())
	}
	unknown := httptest.NewRecorder()
	mux.ServeHTTP(unknown, httptest.NewRequest(http.MethodPost, "/v1/coding/supervisor/start", nil))
	if unknown.Code != http.StatusNotFound {
		t.Fatalf("unexpected control route status=%d", unknown.Code)
	}
}
