package codinghost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

const testControlToken = "coding-shadow-host-control-token-0000000000000001"

func TestHostComposesPrivateHandlersAndClosesWithoutExecutingCandidate(t *testing.T) {
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	docker := sandbox.NewLocalDocker()
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.EgressNetwork = "coding-sandbox"
	docker.EgressProxy = "http://proxy.invalid:3128"
	host, err := newHost(Config{
		ControlToken: testControlToken, PrivateRoot: root, SourceListener: listener,
		SourcePublicBaseURL: "http://host.docker.internal:" + strconv.Itoa(port),
		Policy:              loadPolicy(t), RuntimeImageRepository: "registry.invalid/coding-runtime",
		Docker: docker, CandidateUID: 65532, CandidateGID: 65532,
		MaxTotalBytes: 512 << 20, MaxAttempts: 4,
	}, func(context.Context) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/coding/certifier/canary", strings.NewReader(`{}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	host.CanaryHandler().ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("default canary status=%d", response.Code)
	}
	for path, handler := range map[string]http.Handler{
		"/v1/coding/supervisor/recover":   host.SupervisorHandler(),
		"/v1/coding/publications/pending": host.PublicationHandler(),
	} {
		request := httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{}`))
		request.Header.Set("Content-Type", "application/json")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("path=%s status=%d", path, response.Code)
		}
	}
	if err := host.Close(t.Context()); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "outbox")); err != nil {
		t.Fatal(err)
	}
}

func TestHostAttachesCanaryWhenThePublicPackIsPresent(t *testing.T) {
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	docker := sandbox.NewLocalDocker()
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.EgressNetwork = "coding-sandbox"
	docker.EgressProxy = "http://proxy.invalid:3128"
	repo, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	host, err := newHost(Config{
		ControlToken: testControlToken, PrivateRoot: root, SourceListener: listener,
		SourcePublicBaseURL: "http://host.docker.internal:" + strconv.Itoa(port),
		Policy:              loadPolicy(t), RuntimeImageRepository: "registry.invalid/coding-runtime",
		RuntimeImageDigest: "sha256:" + strings.Repeat("2", 64), CanaryEnabled: true, CertificationRoot: repo,
		Docker: docker, CandidateUID: 65532, CandidateGID: 65532,
		MaxTotalBytes: 512 << 20, MaxAttempts: 4,
	}, func(context.Context) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/v1/coding/certifier/canary", strings.NewReader(`{}`))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	host.CanaryHandler().ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("enabled canary status=%d", response.Code)
	}
	if err := host.Close(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// The readiness probe runs the live daemon checks certify would run after a
// claim. A daemon that cannot prove rootless isolation is never ready.
func TestHostCanaryReadinessRefusesAnUnprovenDaemon(t *testing.T) {
	directory := t.TempDir()
	if err := os.WriteFile(filepath.Join(directory, "docker"), []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", directory)
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	docker := sandbox.NewLocalDocker()
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.EgressNetwork = "coding-sandbox"
	docker.EgressProxy = "http://proxy.invalid:3128"
	docker.DockerHost = "unix:///run/ditto-coding-executor/docker.sock"
	repo, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	host, err := newHost(Config{
		ControlToken: testControlToken, PrivateRoot: root, SourceListener: listener,
		SourcePublicBaseURL: "http://host.docker.internal:" + strconv.Itoa(port),
		Policy:              loadPolicy(t), RuntimeImageRepository: "registry.invalid/coding-runtime",
		RuntimeImageDigest: "sha256:" + strings.Repeat("2", 64), CanaryEnabled: true, CertificationRoot: repo,
		Docker: docker, CandidateUID: 65532, CandidateGID: 65532,
		MaxTotalBytes: 512 << 20, MaxAttempts: 4,
	}, func(context.Context) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = host.Close(context.Background()) })
	for authorized, want := range map[bool]int{false: http.StatusUnauthorized, true: http.StatusOK} {
		request := httptest.NewRequest(http.MethodGet, "/v1/coding/certifier/canary/readiness", nil)
		if authorized {
			request.Header.Set("Authorization", "Bearer "+testControlToken)
		}
		response := httptest.NewRecorder()
		host.CanaryReadinessHandler().ServeHTTP(response, request)
		if response.Code != want {
			t.Fatalf("authorized=%v status=%d", authorized, response.Code)
		}
		if !authorized {
			continue
		}
		var readiness map[string]any
		if err := json.Unmarshal(response.Body.Bytes(), &readiness); err != nil {
			t.Fatal(err)
		}
		if readiness["ready"] != false || readiness["pack_loaded"] != true ||
			readiness["executor_daemon_ready"] != false || readiness["failure"] != "executor_daemon" {
			t.Fatalf("readiness=%v", readiness)
		}
	}
}

func TestHostWithoutCanaryHasNoReadinessRoute(t *testing.T) {
	var host *Host
	response := httptest.NewRecorder()
	host.CanaryReadinessHandler().ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/v1/coding/certifier/canary/readiness", nil))
	if response.Code != http.StatusNotFound {
		t.Fatalf("status=%d", response.Code)
	}
}

func TestHostRefusesTheRootfulSandboxDaemonAsItsCodingEndpoint(t *testing.T) {
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	docker := sandbox.NewLocalDocker()
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.EgressNetwork = "coding-sandbox"
	docker.EgressProxy = "http://proxy.invalid:3128"
	docker.DockerHost = "tcp://127.0.0.1:2375"
	_, err = newHost(Config{
		ControlToken: testControlToken, PrivateRoot: root, SourceListener: listener,
		SourcePublicBaseURL: "http://host.docker.internal:1", Policy: loadPolicy(t),
		RuntimeImageRepository: "registry.invalid/coding-runtime", Docker: docker,
		CandidateUID: 65532, CandidateGID: 65532, MaxTotalBytes: 512 << 20, MaxAttempts: 4,
	}, func(context.Context) error { return nil })
	if err == nil {
		t.Fatal("coding host accepted the rootful sandbox daemon endpoint")
	}
}

func TestHostFailsClosedWhenCanaryIsEnabledWithoutThePublicPack(t *testing.T) {
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	docker := sandbox.NewLocalDocker()
	docker.RequireRootless = true
	docker.RequireIsolatedDaemon = true
	docker.EgressNetwork = "coding-sandbox"
	docker.EgressProxy = "http://proxy.invalid:3128"
	_, err = newHost(Config{
		ControlToken: testControlToken, PrivateRoot: root, SourceListener: listener,
		SourcePublicBaseURL: "http://host.docker.internal:1", Policy: loadPolicy(t),
		RuntimeImageRepository: "registry.invalid/coding-runtime", RuntimeImageDigest: "sha256:" + strings.Repeat("2", 64),
		CanaryEnabled: true, CertificationRoot: root, Docker: docker,
		CandidateUID: 65532, CandidateGID: 65532, MaxTotalBytes: 512 << 20, MaxAttempts: 4,
	}, func(context.Context) error { return nil })
	if err == nil {
		t.Fatal("expected canary-enabled host without a pack to fail closed")
	}
}

func TestHarnessMaxInstancesGivesCanaryItsOwnSlot(t *testing.T) {
	if harnessMaxInstances(false) != 1 || harnessMaxInstances(true) != 2 {
		t.Fatal("canary must not share the sole private harness slot")
	}
}

func TestHostConfigDiagnosticsNeverExposeControlToken(t *testing.T) {
	config := Config{ControlToken: testControlToken, PrivateRoot: "/private"}
	if strings.Contains(fmt.Sprintf("%#v", config), testControlToken) ||
		!errors.Is(json.NewEncoder(io.Discard).Encode(config), ErrClosed) {
		t.Fatal("coding host config exposed private state")
	}
}

func loadPolicy(t *testing.T) codingcontract.InferencePolicy {
	t.Helper()
	path := filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract",
		"testdata", "coding_inference_policy_locked_v1.json",
	)
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	policy, err := codingcontract.ParseInferencePolicy(body)
	if err != nil {
		t.Fatal(err)
	}
	return policy
}
