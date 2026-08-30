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
