//go:build rootless_router_integration

package codingcertservice

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcanary"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/rootlessnetns"
	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// This test needs the dedicated CI job: a real rootless Docker daemon started
// by the test user with the isolated-daemon label, util-linux nsenter, the
// prebuilt router listener helper and the validator's Python client. It uses
// only synthetic data: the committed public certification pack, a random
// bearer and a synthetic runtime image digest. It never skips silently.

const integrationImageDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

func integrationEnv(t *testing.T, name string) string {
	t.Helper()
	value := os.Getenv(name)
	if value == "" {
		t.Fatalf("%s is required", name)
	}
	return value
}

type countingHandler struct {
	next     http.Handler
	requests atomic.Int64
}

func (handler *countingHandler) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	handler.requests.Add(1)
	handler.next.ServeHTTP(response, request)
}

func TestCertificationServiceSocketAndReadinessUnderRootlessDocker(t *testing.T) {
	socket := integrationEnv(t, "DITTOBENCH_ROOTLESS_IT_DOCKER_SOCKET")
	helper := integrationEnv(t, "DITTOBENCH_ROOTLESS_IT_HELPER")
	client := integrationEnv(t, "DITTOBENCH_CERT_IT_CLIENT")
	euid, egid := os.Geteuid(), os.Getegid()
	if euid == 0 {
		t.Fatal("the integration test must run as the rootless daemon user")
	}
	ctx, cancel := context.WithTimeout(t.Context(), 4*time.Minute)
	defer cancel()

	gateway, err := sandbox.DefaultBridgeGatewayFromSocket(ctx, socket)
	if err != nil {
		t.Fatalf("default bridge gateway: %v", err)
	}
	config := Config{
		ServiceUID: euid, ControlGID: egid, RouterListen: netip.AddrPortFrom(gateway, 18090),
		DockerSocketPath: socket, RouterHelperPath: helper,
		RuntimeImageDigest: integrationImageDigest,
	}

	// Startup gate: the dedicated daemon is rootless for this user, reached at
	// its socket with the pinned modes, non-detached, with the router address
	// as its bridge gateway.
	placement := newPlacement(config, euid, nil, nil)
	if !placement.RootlessTopology(ctx) {
		t.Fatal("rootless topology was not proven on a correctly configured daemon")
	}
	wrongUser := newPlacement(config, euid, nil, nil)
	wrongUser.config.ServiceUID = euid + 1
	if wrongUser.RootlessTopology(ctx) {
		t.Fatal("rootless topology accepted another service user")
	}
	wrongGateway := newPlacement(config, euid, nil, nil)
	wrongGateway.config.RouterListen = netip.AddrPortFrom(netip.MustParseAddr("10.254.254.1"), 18090)
	if wrongGateway.RootlessTopology(ctx) {
		t.Fatal("rootless topology accepted a router address that is not the bridge gateway")
	}

	router, err := rootlessnetns.Listen(ctx, config.routerConfig())
	if err != nil {
		t.Fatalf("in-namespace router listener: %v", err)
	}
	defer router.Close()
	placement.router = router

	root, err := os.MkdirTemp("/tmp", "ccs")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(root, "control")
	if err := os.Mkdir(directory, 0o700); err != nil || os.Chmod(directory, ControlDirectoryMode) != nil {
		t.Fatalf("control directory: %v", err)
	}
	control, err := ListenControlSocket(filepath.Join(directory, ControlSocketName), euid, egid)
	if err != nil {
		t.Fatalf("control socket: %v", err)
	}
	defer control.Close()
	placement.control = control

	repo, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	pack, err := codingcanary.LoadPublicPack(repo)
	if err != nil {
		t.Fatalf("public pack: %v", err)
	}
	executors, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{
		ImageRepository: "registry.invalid/ditto/coding-runtime", CandidateUID: CandidateUID, CandidateGID: CandidateGID,
		RequireRootless: true, RequireIsolatedDaemon: true, DockerHost: "unix://" + socket,
	})
	if err != nil {
		t.Fatal(err)
	}
	var tokenBytes [24]byte
	if _, err := rand.Read(tokenBytes[:]); err != nil {
		t.Fatal(err)
	}
	token := hex.EncodeToString(tokenBytes[:])
	service, err := codingcanary.New(codingcanary.Config{
		ControlToken: token, Backend: refusingBackend{}, Pack: pack,
		Topology: placement.Check, RuntimeImageDigest: integrationImageDigest,
		Readiness: func(ctx context.Context) codingcanary.ReadinessCheck {
			// The daemon half is real: rootless with the isolated-daemon label.
			// No reviewed runtime image exists on a CI runner, so image presence
			// is synthetic here and covered by unit tests.
			daemon, _ := executors.CertificationReadiness(ctx, integrationImageDigest)
			return codingcanary.ReadinessCheck{ExecutorDaemon: daemon, RuntimeImage: daemon}
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer service.Close()
	handler := &countingHandler{next: Routes(service.Handler(), service.ReadinessHandler())}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = server.Serve(control.Listener()) }()
	defer server.Close()

	readiness := func(t *testing.T) codingcanary.ReadinessResponse {
		t.Helper()
		transport := &http.Transport{
			Proxy: nil, DisableKeepAlives: true,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", filepath.Join(directory, ControlSocketName))
			},
		}
		defer transport.CloseIdleConnections()
		request, _ := http.NewRequestWithContext(ctx, http.MethodGet, "http://coding-certification.invalid"+codingcanary.ReadinessPath, nil)
		request.Header.Set("Authorization", "Bearer "+token)
		response, err := (&http.Client{Transport: transport}).Do(request)
		if err != nil {
			t.Fatalf("readiness request: %v", err)
		}
		defer response.Body.Close()
		body, _ := io.ReadAll(io.LimitReader(response.Body, 1<<16))
		var decoded codingcanary.ReadinessResponse
		if response.StatusCode != http.StatusOK || json.Unmarshal(body, &decoded) != nil {
			t.Fatalf("readiness status=%d body=%s", response.StatusCode, body)
		}
		return decoded
	}
	runClient := func(t *testing.T, expect string) {
		t.Helper()
		command := exec.CommandContext(ctx, client,
			"--socket", filepath.Join(directory, ControlSocketName),
			"--uid", strconv.Itoa(euid), "--gid", strconv.Itoa(egid),
			"--image-digest", integrationImageDigest,
			"--manifest-sha256", pack.CanaryManifestSHA256,
			"--expect", expect,
		)
		command.Env = append(os.Environ(), "DITTOBENCH_CERT_IT_TOKEN="+token)
		output, err := command.CombinedOutput()
		if err != nil {
			t.Fatalf("validator client expecting %s: %v\n%s", expect, err, output)
		}
		if strings.Contains(string(output), token) {
			t.Fatal("validator client printed the bearer")
		}
		t.Logf("validator client (%s): %s", expect, strings.TrimSpace(string(output)))
	}

	// (a) Placed: every proof holds and the validator's own client, over the
	// verified socket, reports ready with the pinned digests.
	ready := readiness(t)
	if !ready.Ready || !ready.RootlessTopologyReady || !ready.ListenerNamespaceReady || !ready.ControlSocketReady ||
		!ready.ExecutorDaemonReady || ready.RuntimeImageDigest != integrationImageDigest ||
		ready.CanaryManifestSHA256 != pack.CanaryManifestSHA256 {
		t.Fatalf("placed readiness=%+v", ready)
	}
	runClient(t, "ready")

	// (b) A relaxed control socket: the service reports control_socket, and the
	// validator refuses before sending a single request.
	socketPath := filepath.Join(directory, ControlSocketName)
	if err := os.Chmod(socketPath, 0o666); err != nil {
		t.Fatal(err)
	}
	if got := readiness(t); got.Ready || got.Failure != "control_socket" {
		t.Fatalf("relaxed socket readiness=%+v", got)
	}
	before := handler.requests.Load()
	runClient(t, "socket_refused")
	if handler.requests.Load() != before {
		t.Fatal("the validator sent a request over an unverified socket")
	}
	if err := os.Chmod(socketPath, ControlSocketMode); err != nil {
		t.Fatal(err)
	}
	if got := readiness(t); !got.Ready {
		t.Fatalf("restored readiness=%+v", got)
	}

	// (c) A router listener outside RootlessKit's namespace (the legacy host
	// namespace listener) is refused by the kernel namespace check.
	var hostConfig net.ListenConfig
	hostConfig.SetMultipathTCP(false)
	hostListener, err := hostConfig.Listen(ctx, "tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer hostListener.Close()
	placement.router = hostListener
	if got := readiness(t); got.Ready || got.Failure != "listener_namespace" || !got.RootlessTopologyReady {
		t.Fatalf("host listener readiness=%+v", got)
	}
	runClient(t, "not_ready:listener_namespace")
	placement.router = router
	if got := readiness(t); !got.Ready {
		t.Fatalf("restored listener readiness=%+v", got)
	}

	// (d) The control socket path swapped for another socket with identical
	// owner, group and mode: the served inode no longer matches.
	if err := os.Rename(socketPath, socketPath+".served"); err != nil {
		t.Fatal(err)
	}
	impostor, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(socketPath, ControlSocketMode); err != nil {
		t.Fatal(err)
	}
	if control.Verify() == nil {
		t.Fatal("a swapped control socket verified")
	}
	_ = impostor.Close()
	if err := os.Rename(socketPath+".served", socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		t.Fatal(err)
	}
	if err := control.Verify(); err != nil {
		t.Fatalf("restored control socket: %v", err)
	}
}

type refusingBackend struct{}

func (refusingBackend) Certify(context.Context, codingcanary.Request) (codingcanary.Outcome, error) {
	return codingcanary.Outcome{}, codingcanary.ErrUnavailable
}
