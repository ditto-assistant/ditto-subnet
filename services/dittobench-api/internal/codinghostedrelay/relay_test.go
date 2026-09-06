package codinghostedrelay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

func source(b binding) codingsource.HostedBinding {
	return codingsource.HostedBinding{HarnessInstanceID: b.HarnessInstanceID, AgentArtifactSHA256: b.ArtifactSHA256, EvaluationID: b.EvaluationID, AttemptID: b.AttemptID, WorkerID: b.WorkerID, AssignmentSHA256: b.AssignmentSHA256, ProfileCapabilityID: b.ProfileCapabilityID, Deadline: time.Unix(b.ExpiresAtUnix, 0)}
}

func sourceRouter(t *testing.T, b binding) (*codingsource.Router, *codingsource.Lease) {
	t.Helper()
	registry := codingsource.NewRegistry(nil)
	lease, err := registry.RegisterHosted(source(b), "172.30.0.7")
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	_, port, _ := net.SplitHostPort(listener.Addr().String())
	router, err := codingsource.NewRouter(codingsource.RouterConfig{Listener: listener, PublicBaseURL: "http://host.docker.internal:" + port, Registry: registry})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = lease.Close()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		_ = router.Close(ctx)
	})
	return router, lease
}

func request(t *testing.T, router *codingsource.Router, target, remote string, body []byte) *httptest.ResponseRecorder {
	t.Helper()
	parsed, err := url.Parse(target)
	if err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(http.MethodPost, parsed.RequestURI(), bytes.NewReader(body))
	r.Host = parsed.Host
	r.RemoteAddr = remote
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-Forwarded-For", "172.30.0.7")
	r.Header.Set("Authorization", "Bearer miner-must-not-forward")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, r)
	return w
}

func TestPrivateBridgeIntegration(t *testing.T) {
	path := os.Getenv("DITTO_HOSTED_RELAY_TEST_SOCKET")
	if path == "" {
		t.Skip("real Python/PostgreSQL bridge is driven by the Platform integration test")
	}
	var b binding
	if json.Unmarshal([]byte(os.Getenv("DITTO_HOSTED_RELAY_TEST_BINDING")), &b) != nil {
		t.Fatal("invalid test binding")
	}
	token, err := hex.DecodeString(os.Getenv("DITTO_HOSTED_RELAY_TEST_TOKEN"))
	if err != nil {
		t.Fatal("invalid test token")
	}
	body, err := base64.StdEncoding.DecodeString(os.Getenv("DITTO_HOSTED_RELAY_TEST_BODY"))
	if err != nil {
		t.Fatal("invalid test body")
	}
	router, _ := sourceRouter(t, b)
	relay, err := Publish(t.Context(), Config{Router: router, Source: source(b), GrantID: b.GrantID, PolicySHA256: b.PolicySHA256, SocketPath: path, Token: token})
	if err != nil {
		t.Fatal(err)
	}
	target := relay.URL() + "/chat/completions"
	for _, remote := range []string{"172.30.0.8:1000", "127.0.0.1:1000"} {
		if w := request(t, router, target, remote, body); w.Code != 404 {
			t.Fatal("source spoof accepted")
		}
	}
	w := request(t, router, target, "172.30.0.7:1000", body)
	if w.Code != 200 {
		t.Fatalf("native source request failed: %d", w.Code)
	}
	if bytes.Contains(w.Body.Bytes(), []byte("settlement")) || bytes.Contains(w.Body.Bytes(), []byte("provider")) || !bytes.Contains(w.Body.Bytes(), []byte("private answer")) {
		t.Fatal("wrong miner projection")
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if relay.URL() != "" {
		t.Fatal("revoked URL exposed")
	}
	if w := request(t, router, target, "172.30.0.7:1000", body); w.Code != 404 {
		t.Fatal("revoked route accepted")
	}
}

type fakeBridge struct {
	path  string
	token []byte
	b     binding
	calls atomic.Int32
	mode  string
}

func bridgeFixture(t *testing.T, mode string) *fakeBridge {
	t.Helper()
	directory, err := os.MkdirTemp("", "hosted-rpc-")
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "bridge.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	if err = os.Chmod(path, 0600); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close(); _ = os.Remove(path); _ = os.Remove(directory) })
	b := binding{"dittobench-coding-hosted-relay-binding-v2", uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString(), strings.Repeat("a", 64), strings.Repeat("b", 64), strings.Repeat("c", 64), "container", "profile", time.Now().Add(time.Minute).Unix()}
	f := &fakeBridge{path: path, token: bytes.Repeat([]byte{7}, 32), b: b, mode: mode}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go f.serve(conn)
		}
	}()
	return f
}
func (f *fakeBridge) serve(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	auth := make([]byte, len(magic)+32)
	if _, err := io.ReadFull(conn, auth); err != nil || !bytes.Equal(auth, append([]byte(magic), f.token...)) {
		return
	}
	var h [4]byte
	if _, err := io.ReadFull(conn, h[:]); err != nil {
		return
	}
	n := binary.BigEndian.Uint32(h[:])
	if n > 6<<20 {
		return
	}
	raw := make([]byte, n)
	if _, err := io.ReadFull(conn, raw); err != nil {
		return
	}
	var command map[string]any
	if json.Unmarshal(raw, &command) != nil {
		return
	}
	reply := map[string]any{"schema": "dittobench-coding-hosted-relay-result-v2", "binding_sha256": command["binding_sha256"], "request_id": command["request_id"], "operation": command["operation"]}
	if f.mode == "open_disconnect" && command["operation"] == "open" {
		return
	}
	switch command["operation"] {
	case "complete":
		f.calls.Add(1)
		if f.mode == "disconnect" {
			return
		}
		if f.mode == "block" {
			var p [1]byte
			_, _ = conn.Read(p[:])
			return
		}
		normalized := map[string]any{"schema": "dittobench-coding-hosted-inference-response-v2", "id": "gen-test", "model": "openai/gpt-5.6-luna", "provider": "Azure", "choices": []any{map[string]any{"message": map[string]any{"content": "answer", "tool_calls": []any{}}}}, "usage": map[string]any{"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost_usd_micros": 1}}
		body, _ := canonical(normalized)
		sum := sha256.Sum256(body)
		reply["response_base64"] = base64.StdEncoding.EncodeToString(body)
		receipt := map[string]any{"schema": "dittobench-coding-hosted-inference-settlement-v2", "evaluation_id": f.b.EvaluationID, "attempt_id": f.b.AttemptID, "grant_id": f.b.GrantID, "request_id": command["request_id"], "policy_sha256": f.b.PolicySHA256, "response_sha256": hex.EncodeToString(sum[:]), "prompt_tokens": 20, "completion_tokens": 10, "cost_usd_micros": 1}
		receipt["sequence"] = 1
		receipt["locked_request_sha256"] = strings.Repeat("d", 64)
		receipt["provider_receipt_sha256"] = strings.Repeat("e", 64)
		receipt["model"] = "openai/gpt-5.6-luna"
		receipt["provider"] = "Azure"
		receipt["provider_route"] = "azure/eu"
		receipt["provider_route_profile"] = "luna-azure-eu-zdr-v1"
		receipt["fallback_used"] = false
		if f.mode == "foreign" {
			receipt["grant_id"] = uuid.NewString()
		}
		if f.mode == "wrong_digest" {
			receipt["response_sha256"] = strings.Repeat("f", 64)
		}
		if f.mode == "fallback" {
			receipt["fallback_used"] = true
		}
		if f.mode == "missing_usage" {
			delete(receipt, "cost_usd_micros")
		}
		reply["settlement"] = receipt
	case "revoke":
		reply["ledger_drained"] = f.mode != "block"
	}
	encoded, _ := canonical(reply)
	if f.mode == "bad_open" {
		encoded = []byte("{}")
	}
	_, _ = conn.Write(append(binary.BigEndian.AppendUint32(nil, uint32(len(encoded))), encoded...))
}

func TestNativeRouteProjectionAndFailureClosure(t *testing.T) {
	for _, mode := range []string{"success", "foreign", "wrong_digest", "disconnect", "fallback", "missing_usage"} {
		t.Run(mode, func(t *testing.T) {
			f := bridgeFixture(t, mode)
			router, _ := sourceRouter(t, f.b)
			relay, err := Publish(t.Context(), Config{Router: router, Source: source(f.b), GrantID: f.b.GrantID, PolicySHA256: f.b.PolicySHA256, SocketPath: f.path, Token: f.token})
			if err != nil {
				t.Fatal(err)
			}
			target := relay.URL() + "/chat/completions"
			if w := request(t, router, target, "172.30.0.8:1", []byte("{}")); w.Code != 404 {
				t.Fatal("cross-container accepted")
			}
			w := request(t, router, target, "172.30.0.7:1", []byte("{}"))
			want := 200
			if mode != "success" {
				want = 502
			}
			if w.Code != want {
				t.Fatalf("status %d want %d", w.Code, want)
			}
			if f.calls.Load() != 1 {
				t.Fatal("unexpected dispatch count")
			}
			if err = relay.Revoke(t.Context()); err != nil {
				t.Fatal(err)
			}
			if w = request(t, router, target, "172.30.0.7:1", []byte("{}")); w.Code != 404 {
				t.Fatal("revoked route accepted")
			}
		})
	}
}

func TestShorterGrantExpiryPreservesAssignmentSourceBinding(t *testing.T) {
	f := bridgeFixture(t, "success")
	assignment := f.b
	assignment.ExpiresAtUnix += 60
	router, _ := sourceRouter(t, assignment)
	config := Config{Router: router, Source: source(assignment), GrantID: f.b.GrantID, PolicySHA256: f.b.PolicySHA256, SocketPath: f.path, Token: f.token, ExpiresAt: time.Unix(f.b.ExpiresAtUnix, 0)}
	relay, err := Publish(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	if !relay.deadline.Equal(config.ExpiresAt) {
		t.Fatal("grant deadline extended")
	}
	if reply := request(t, router, relay.URL()+"/chat/completions", "172.30.0.7:1", []byte("{}")); reply.Code != 200 {
		t.Fatal("assignment source binding lost", reply.Code)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	for _, expiry := range []time.Time{config.Source.Deadline.Add(time.Second), time.Now().Add(-time.Second), config.ExpiresAt.Add(time.Nanosecond)} {
		bad := config
		bad.ExpiresAt = expiry
		if handle, err := Publish(t.Context(), bad); handle != nil || err == nil {
			t.Fatal("invalid grant expiry accepted")
		}
	}
}

func TestRevocationCancelsActiveTransportWithoutInventingDrain(t *testing.T) {
	f := bridgeFixture(t, "block")
	router, _ := sourceRouter(t, f.b)
	relay, err := Publish(t.Context(), Config{Router: router, Source: source(f.b), GrantID: f.b.GrantID, PolicySHA256: f.b.PolicySHA256, SocketPath: f.path, Token: f.token})
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	target := relay.URL() + "/chat/completions"
	go func() { defer close(done); request(t, router, target, "172.30.0.7:1", []byte("{}")) }()
	deadline := time.Now().Add(time.Second)
	for f.calls.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	if err = relay.Revoke(ctx); err != ErrUnsettled {
		t.Fatalf("revoke=%v", err)
	}
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("request not drained")
	}
	if relay.URL() != "" {
		t.Fatal("failed drain resurrected URL")
	}
}

func TestPrivateSocketAndHandshakeFailClosed(t *testing.T) {
	for _, mode := range []string{"permissions", "token", "bad_open"} {
		t.Run(mode, func(t *testing.T) {
			f := bridgeFixture(t, mode)
			router, _ := sourceRouter(t, f.b)
			if mode == "permissions" {
				_ = os.Chmod(f.path, 0666)
			}
			token := append([]byte(nil), f.token...)
			if mode == "token" {
				token[0] ^= 1
			}
			if _, err := Publish(t.Context(), Config{Router: router, Source: source(f.b), GrantID: f.b.GrantID, PolicySHA256: f.b.PolicySHA256, SocketPath: f.path, Token: token}); err == nil {
				t.Fatal("invalid bridge accepted")
			}
		})
	}
}

func TestAmbiguousOpenRetainsCleanupHandle(t *testing.T) {
	f := bridgeFixture(t, "open_disconnect")
	router, _ := sourceRouter(t, f.b)
	relay, err := Publish(t.Context(), Config{Router: router, Source: source(f.b), GrantID: f.b.GrantID, PolicySHA256: f.b.PolicySHA256, SocketPath: f.path, Token: f.token})
	if err == nil || relay == nil || relay.URL() != "" {
		t.Fatal("ambiguous open lost cleanup handle")
	}
	if err = relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if f.calls.Load() != 0 {
		t.Fatal("open dispatched a provider request")
	}
}
