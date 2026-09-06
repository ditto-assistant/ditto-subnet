package codinghostedworker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
	"github.com/ditto-assistant/dittobench-api/internal/codingharness"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

func TestConcurrentCleanupCancelsRunWithoutFreezeCommit(t *testing.T) {
	a, f := setup(t)
	f.blockRun = true
	done := make(chan error, 1)
	go func() { _, err := a.Run(t.Context()); done <- err }()
	<-f.runStarted
	if err := a.Cleanup(); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err == nil {
		t.Fatal("cancelled run succeeded")
	}
	if slices.Contains(f.events, "commit") {
		t.Fatal("cancelled run committed")
	}
}

type routeListener struct{ net.Listener }

func (l routeListener) Addr() net.Addr {
	a := *l.Listener.Addr().(*net.TCPAddr)
	a.IP = net.ParseIP("172.21.0.1")
	return &a
}

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type nativeRuntime struct{ f *fixture }

func (r nativeRuntime) Available(context.Context) error { return nil }
func (r nativeRuntime) Load(context.Context, codingharness.ImageSource) (string, error) {
	return "screened-test-image", nil
}
func (r nativeRuntime) Start(context.Context, string) (codingharness.Running, error) {
	return nativeRunning{}, r.f.event("container-start")
}
func (r nativeRuntime) Stop(context.Context, codingharness.Running) error {
	return r.f.event("destroy")
}
func (r nativeRuntime) Release(context.Context, string) {}

type nativeRunning struct{}

func (nativeRunning) ContainerID() string { return strings.Repeat("c", 64) }
func (nativeRunning) BaseURL() string     { return "http://127.0.0.1:8000" }
func (nativeRunning) SourceIP() string    { return "172.21.0.5" }
func (nativeRunning) ImageRef() string    { return "screened-test-image" }

type nativeStarts struct {
	f    *fixture
	used bool
}

func (s *nativeStarts) CommitStart(context.Context, codingharness.HostedBinding, string) (bool, error) {
	if s.used {
		return false, nil
	}
	s.used = true
	return true, s.f.event("commit-start")
}

func TestNativeHarnessSourceRouterAndRelayComposeWithWorker(t *testing.T) {
	old, f := setup(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	registry := codingsource.NewRegistry(nil)
	base := fmt.Sprintf("http://host.docker.internal:%d", listener.Addr().(*net.TCPAddr).Port)
	router, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: routeListener{listener}, PublicBaseURL: base, Registry: registry})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = router.Close(context.Background()) })
	transport := transportFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "127.0.0.1:8000" {
			return nil, ErrAttempt
		}
		w := httptest.NewRecorder()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/coding/health":
			h, _ := (testHarness{f}).Health(r.Context())
			_ = json.NewEncoder(w).Encode(h)
		case "/coding/seed":
			var seed codingcontract.HostedSeedRequest
			if json.NewDecoder(r.Body).Decode(&seed) != nil {
				return nil, ErrAttempt
			}
			response, _ := (testHarness{f}).Seed(r.Context(), seed)
			_ = json.NewEncoder(w).Encode(response)
		case "/coding/run":
			var run codingcontract.HostedRunRequest
			if json.NewDecoder(r.Body).Decode(&run) != nil {
				return nil, ErrAttempt
			}
			target, err := url.Parse(run.WorkspaceCapabilityURL)
			if err != nil {
				return nil, ErrAttempt
			}
			f.handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				r.URL = target
				r.Host = target.Host
				r.RemoteAddr = "172.21.0.5:9001"
				router.ServeHTTP(w, r)
			})
			response, err := (testHarness{f}).Run(r.Context(), run)
			if err != nil {
				return nil, err
			}
			response.FinalReport.Summary = "done"
			response.FinalReport.RemainingRisks = []string{}
			_ = json.NewEncoder(w).Encode(response)
		default:
			return nil, ErrAttempt
		}
		return w.Result(), nil
	})
	harnesses, err := codingharness.NewHosted(codingharness.HostedConfig{Config: codingharness.Config{Runtime: nativeRuntime{f}, Sources: registry, Transport: transport, NewInstance: func() string { return "synthetic-instance" }}, Starts: &nativeStarts{f: f}})
	if err != nil {
		t.Fatal(err)
	}
	executors, err := codingexecutor.NewPhaseFactory(codingexecutor.FactoryConfig{ImageRepository: "example.invalid/native", CandidateUID: 10001, CandidateGID: 10001, RequireRootless: true, RequireIsolatedDaemon: true})
	if err != nil {
		t.Fatal(err)
	}
	// Exercise the real native relay against a synthetic local bridge, with no
	// provider dispatch. The bridge protocol is separately tested with Python/PG.
	root, err := os.MkdirTemp("", "native-worker-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	socket := filepath.Join(root, "bridge.sock")
	backend, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(socket, 0600); err != nil {
		t.Fatal(err)
	}
	token := bytes.Repeat([]byte{1}, 32)
	var peers sync.WaitGroup
	acceptDone := make(chan struct{})
	go func() {
		defer close(acceptDone)
		for {
			conn, err := backend.Accept()
			if err != nil {
				return
			}
			peers.Add(1)
			go func() {
				defer peers.Done()
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
				prefix := make([]byte, len("DITTO-HOSTED-INFERENCE-V2\n")+32)
				if _, err := io.ReadFull(conn, prefix); err != nil || !bytes.Equal(prefix, append([]byte("DITTO-HOSTED-INFERENCE-V2\n"), token...)) {
					return
				}
				var size [4]byte
				if _, err := io.ReadFull(conn, size[:]); err != nil {
					return
				}
				n := binary.BigEndian.Uint32(size[:])
				if n > 16384 {
					return
				}
				body := make([]byte, n)
				if _, err := io.ReadFull(conn, body); err != nil {
					return
				}
				var command map[string]any
				if json.Unmarshal(body, &command) != nil {
					return
				}
				reply := map[string]any{"schema": "dittobench-coding-hosted-relay-result-v2", "binding_sha256": command["binding_sha256"], "request_id": command["request_id"], "operation": command["operation"]}
				if command["operation"] == "revoke" {
					reply["ledger_drained"] = true
				}
				encoded, _ := json.Marshal(reply)
				_, _ = conn.Write(append(binary.BigEndian.AppendUint32(nil, uint32(len(encoded))), encoded...))
			}()
		}
	}()
	t.Cleanup(func() { _ = backend.Close(); <-acceptDone; peers.Wait() })
	f.bridge = &Bridge{GrantID: "40000000-0000-4000-8000-000000000004", PolicySHA256: strings.Repeat("d", 64), SocketPath: socket, Token: token, Revoke: func(context.Context) error { return f.event("bridge-revoke") }, Close: func(context.Context) error { _ = backend.Close(); return f.event("bridge-close") }}
	w, err := New(Config{Harnesses: harnesses, Executors: executors, Router: router, Control: f})
	if err != nil {
		t.Fatal(err)
	}
	// Verified frames have their own Go/Python integration. Use synthetic inputs
	// here with the real runner, harness lifecycle, source router and relay.
	w.deps.load = func(context.Context, codingsource.HostedBinding) (input, error) { return f.inputs, f.event("load") }
	binding := old.binding
	binding.AgentID = "50000000-0000-4000-8000-000000000005"
	binding.ScreenedImageSHA256 = strings.Repeat("b", 64)
	binding.ScreenedImageID = "sha256:" + strings.Repeat("c", 64)
	binding.ScreenedImageRef = "ditto-screen/" + binding.AgentID + ":latest"
	binding.ScreenedImageSize = 1
	binding.ScreeningPolicyVersion = 11
	binding.ImageURL = "https://storage.invalid/image.tar?signature=synthetic"
	binding.ImageExpiresAt = time.Now().Add(time.Minute)
	a, err := w.Attempt(binding)
	if err != nil {
		t.Fatal(err)
	}
	authority, err := a.Run(t.Context())
	if err != nil {
		_ = a.Cleanup()
		t.Fatal("native worker integration failed", err, f.events)
	}
	if authority.FrozenPatchSHA256 != fmt.Sprintf("%x", sha256.Sum256(f.patch)) {
		t.Fatal("native freeze identity differs")
	}
	if slices.Index(f.events, "commit-start") > slices.Index(f.events, "container-start") {
		t.Fatal("candidate started before durable start")
	}
	if err := a.Cleanup(); err != nil {
		t.Fatal(err)
	}
}
