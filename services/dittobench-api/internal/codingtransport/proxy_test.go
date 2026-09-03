package codingtransport

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
)

const testHotkey = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func envelopeHeader(t *testing.T, hotkey string) string {
	t.Helper()
	now := time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)
	value := codingcontract.ExecutorControlEnvelope{
		Schema: codingcontract.ExecutorControlSchema, CodingContractVersion: 1,
		WeightEligible: false, ValidatorHotkey: hotkey,
		AgentID: "10000000-0000-4000-8000-000000000001", AgentArtifactSHA256: strings.Repeat("1", 64),
		CodingRunID: "coding-run-001", TicketID: "20000000-0000-4000-8000-000000000002",
		Operation: "supervisor.author", Method: http.MethodPost,
		RequestBodySHA256: strings.Repeat("2", 64), Nonce: "30000000-0000-4000-8000-000000000003",
		IssuedAt: now, ExpiresAt: now.Add(time.Minute), Signature: strings.Repeat("ab", 64),
	}
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func tlsState(t *testing.T, hotkey string) *tls.ConnectionState {
	t.Helper()
	uri, err := ValidatorURI(hotkey)
	if err != nil {
		t.Fatal(err)
	}
	leaf := &x509.Certificate{URIs: []*url.URL{uri}}
	return &tls.ConnectionState{
		Version: tls.VersionTLS13, HandshakeComplete: true,
		PeerCertificates: []*x509.Certificate{leaf},
		VerifiedChains:   [][]*x509.Certificate{{leaf}},
	}
}

func TestProxyBindsClientCertificateToEnvelopeAndForwardsOnlySafeHeaders(t *testing.T) {
	body := []byte(`{"ticket_id":"20000000-0000-4000-8000-000000000002"}`)
	transport := roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.URL.Scheme != "http" || request.URL.Host != "coding-executor.internal" ||
			request.URL.Path != "/v1/coding/supervisor/author" || request.Method != http.MethodPost {
			t.Fatalf("upstream request=%s %s", request.Method, request.URL)
		}
		observed, err := io.ReadAll(request.Body)
		if err != nil || string(observed) != string(body) {
			t.Fatalf("body=%q err=%v", observed, err)
		}
		if request.Header.Get(codingcontrol.EnvelopeHeader) == "" ||
			request.Header.Get("Authorization") != "" || request.Header.Get("Cookie") != "" {
			t.Fatalf("forwarded headers=%v", request.Header)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header: http.Header{
				"Content-Type": {"application/json"},
				"Set-Cookie":   {"must-not-cross=1"},
			},
			Body: io.NopCloser(strings.NewReader(`{"ok":true}` + "\n")),
		}, nil
	})
	proxy, err := New(Config{
		ValidatorHotkey: testHotkey, UnixSocketPath: ControlSocketPath,
		RoundTripper: transport,
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(
		http.MethodPost, "/v1/coding/supervisor/author", strings.NewReader(string(body)),
	)
	request.TLS = tlsState(t, testHotkey)
	request.Header.Set(codingcontrol.EnvelopeHeader, envelopeHeader(t, testHotkey))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer external-must-not-forward")
	request.Header.Set("Cookie", "external=must-not-forward")
	response := httptest.NewRecorder()
	proxy.Handler().ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != `{"ok":true}`+"\n" ||
		response.Header().Get("Set-Cookie") != "" {
		t.Fatalf("status=%d body=%q headers=%v", response.Code, response.Body.String(), response.Header())
	}
}

func TestProxyRejectsCertificateEnvelopeAndRouteDrift(t *testing.T) {
	called := false
	proxy, err := New(Config{
		ValidatorHotkey: testHotkey, UnixSocketPath: ControlSocketPath,
		RoundTripper: roundTripFunc(func(*http.Request) (*http.Response, error) {
			called = true
			return nil, errors.New("unexpected call")
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	otherHotkey := "5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
	tests := map[string]struct {
		method string
		path   string
		tls    *tls.ConnectionState
		header string
		status int
	}{
		"no certificate":    {http.MethodPost, "/v1/coding/supervisor/author", nil, envelopeHeader(t, testHotkey), http.StatusUnauthorized},
		"wrong certificate": {http.MethodPost, "/v1/coding/supervisor/author", tlsState(t, otherHotkey), envelopeHeader(t, testHotkey), http.StatusUnauthorized},
		"wrong envelope":    {http.MethodPost, "/v1/coding/supervisor/author", tlsState(t, testHotkey), envelopeHeader(t, otherHotkey), http.StatusUnauthorized},
		"query":             {http.MethodPost, "/v1/coding/supervisor/author?debug=1", tlsState(t, testHotkey), envelopeHeader(t, testHotkey), http.StatusNotFound},
		"method":            {http.MethodGet, "/v1/coding/supervisor/author", tlsState(t, testHotkey), envelopeHeader(t, testHotkey), http.StatusNotFound},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			request := httptest.NewRequest(test.method, test.path, strings.NewReader(`{}`))
			request.TLS = test.tls
			request.Header.Set(codingcontrol.EnvelopeHeader, test.header)
			response := httptest.NewRecorder()
			proxy.Handler().ServeHTTP(response, request)
			if response.Code != test.status {
				t.Fatalf("status=%d body=%q", response.Code, response.Body.String())
			}
		})
	}
	if called {
		t.Fatal("rejected transport reached the Unix client")
	}
}

func TestTLSConfigAndDiagnosticsFailClosed(t *testing.T) {
	pool := x509.NewCertPool()
	config, err := ServerTLSConfig(tls.Certificate{Certificate: [][]byte{{1}}}, pool)
	if err != nil || config.MinVersion != tls.VersionTLS13 || config.MaxVersion != tls.VersionTLS13 ||
		config.ClientAuth != tls.RequireAndVerifyClientCert || !config.SessionTicketsDisabled ||
		len(config.NextProtos) != 1 || config.NextProtos[0] != "http/1.1" {
		t.Fatalf("TLS config=%#v err=%v", config, err)
	}
	private := Config{ValidatorHotkey: testHotkey, UnixSocketPath: ControlSocketPath}
	if strings.Contains(fmt.Sprintf("%#v", private), ControlSocketPath) {
		t.Fatal("transport diagnostics exposed private configuration")
	}
	if _, err := json.Marshal(private); !errors.Is(err, ErrPrivate) {
		t.Fatalf("marshal error=%v", err)
	}
}
