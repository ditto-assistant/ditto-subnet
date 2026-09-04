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
