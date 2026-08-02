package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/google/uuid"
)

func TestOpenRouterShimRoutesHardcodedTLSURLThroughBoundBroker(t *testing.T) {
	originalCandidates := systemCABundleCandidates
	systemCABundleCandidates = nil
	t.Cleanup(func() { systemCABundleCandidates = originalCandidates })

	bundlePath := filepath.Join(t.TempDir(), "ca-bundle.pem")
	tlsConfig, err := prepareOpenRouterShim(bundlePath, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := os.ReadFile(bundlePath)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(bundle, []byte("PRIVATE KEY")) {
		t.Fatal("public sandbox CA bundle contains a private key")
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(bundle) {
		t.Fatal("shim CA bundle contains no certificate")
	}

	var upstreamModel string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Model string `json:"model"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode upstream request: %v", err)
		}
		upstreamModel = body.Model
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"usage":{"prompt_tokens":3,"completion_tokens":4},"choices":[{"message":{"content":"OK"}}]}`)
	}))
	defer upstream.Close()

	broker := newInferenceBroker(1)
	runID := uuid.NewString()
	sessionID, err := broker.prepareLegacy(runID, protocol.BenchVersionV6, upstream.URL, relayHealthSnapshot{
		Provider: "openrouter", ProfileRevision: llm.OpenRouterRelayProfileRevision,
		Model: llm.LockedHarnessModel,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !broker.bindSource(sessionID, runID, "127.0.0.1") {
		t.Fatal("failed to bind source for shim test")
	}

	shim := httptest.NewUnstartedServer(http.HandlerFunc(broker.handleOpenRouterShim))
	shim.TLS = tlsConfig
	shim.StartTLS()
	defer shim.Close()
	transport := &http.Transport{
		Proxy: nil,
		DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, shim.Listener.Addr().String())
		},
		TLSClientConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
			RootCAs:    roots,
			ServerName: openRouterShimServerName,
		},
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	response, err := client.Post(
		"https://openrouter.ai/api/v1/chat/completions",
		"application/json",
		bytes.NewBufferString(`{"model":"attacker/alternate-model","messages":[{"role":"user","content":"hello"}]}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("shim status=%d body=%s", response.StatusCode, body)
	}
	if upstreamModel != llm.LockedHarnessModel {
		t.Fatalf("shim forwarded model %q, want locked %q", upstreamModel, llm.LockedHarnessModel)
	}
	snapshot, err := broker.snapshot(sessionID)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Requests != 1 || snapshot.Successes != 1 || snapshot.UsageAvailable != 1 {
		t.Fatalf("shim bypassed trusted accounting: %+v", snapshot)
	}
}

func TestOpenRouterShimRejectsOtherHostsPathsAndSources(t *testing.T) {
	broker := newInferenceBroker(1)
	for _, testCase := range []struct {
		name       string
		host       string
		path       string
		remoteAddr string
		want       int
	}{
		{name: "other host", host: "example.com", path: "/api/v1/chat/completions", remoteAddr: "192.0.2.1:1", want: http.StatusNotFound},
		{name: "other path", host: "openrouter.ai", path: "/api/v1/models", remoteAddr: "192.0.2.1:1", want: http.StatusNotFound},
		{name: "unbound source", host: "openrouter.ai", path: "/api/v1/chat/completions", remoteAddr: "192.0.2.1:1", want: http.StatusUnauthorized},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "https://"+testCase.host+testCase.path, bytes.NewBufferString(`{}`))
			request.RemoteAddr = testCase.remoteAddr
			recorder := httptest.NewRecorder()
			broker.handleOpenRouterShim(recorder, request)
			if recorder.Code != testCase.want {
				t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
			}
		})
	}
}
