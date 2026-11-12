package server_test

import (
	"compress/gzip"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/server"

	"github.com/ditto-assistant/model-relay/internal/metrics"
	"github.com/prometheus/client_golang/prometheus"
)

func testConfig() *config.Config {
	cfg, err := config.Load(config.MapLookup(map[string]string{
		"POSTGRES_USER":                "x",
		"POSTGRES_PASSWORD":            "x",
		"POSTGRES_DB":                  "x",
		"PYLON_OPEN_ACCESS_TOKEN":      "x",
		"DITTO_UPLOAD_PAYMENT_ADDRESS": "5NotARea1SS58AddressTestFixtureDoNotSendTaoHere",
	}))
	if err != nil {
		panic(err)
	}
	return cfg
}

func newTestServer(opts ...server.Option) http.Handler {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	// pool and prober are nil: these tests exercise only routes that touch
	// neither (/health is covered by pg-backed integration coverage).
	s := server.New(testConfig(), logger, nil, nil, "deadbeef", opts...)
	return s.Handler()
}

func TestMetricsEndpointShape(t *testing.T) {
	// Unlike the Python client, client_golang only exposes a labeled family
	// once it has at least one child; touch the one counter the relay
	// request paths increment so the family (and its exact name and labels)
	// is asserted here.
	metrics.AdmissionAtCapacity.WithLabelValues(metrics.LaneChat, metrics.ScopePerTicket).Add(0)

	h := newTestServer()
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status: %d", rec.Code)
	}
	ct := rec.Header().Get("Content-Type")
	if !strings.HasPrefix(ct, "text/plain") || !strings.Contains(ct, "version=0.0.4") {
		t.Fatalf("prometheus exposition content type expected, got %q", ct)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "ditto_inference_admission_at_capacity_total") {
		t.Fatalf("admission counter family must be declared in exposition")
	}
	if rec.Header().Get("X-Request-ID") == "" {
		t.Fatalf("middleware must echo X-Request-ID on /metrics too")
	}
}

func TestMetricsExposeOnlyLoadedDaniLabsBindingMetadata(t *testing.T) {
	cfg := testConfig()
	cfg.Inference.DaniLabsAPIKey = "secret-that-must-not-be-exposed"
	cfg.Inference.DaniLabsTrialScope = "canary"
	cfg.Inference.DaniLabsTrialUntil = "2026-11-11T16:00:00Z"
	cfg.DittoRouter.Enabled = true
	cfg.DittoRouter.APIKey = "router-secret-that-must-not-be-exposed"
	cfg.DittoRouter.Lanes = []string{config.DittoRouterLaneCompetition}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	h := server.New(cfg, logger, nil, nil, "deadbeef").Handler()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	req.RemoteAddr = "127.0.0.1:12345"
	h.ServeHTTP(rec, req)
	body := rec.Body.String()
	for _, want := range []string{
		"ditto_danilabs_binding_key_present 1\n",
		"ditto_danilabs_binding_scope 1\n",
		"ditto_danilabs_binding_router_competition 1\n",
		"ditto_danilabs_binding_deadline_seconds 1.7944128e+09\n",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing metric %q", want)
		}
	}
	if strings.Contains(body, cfg.Inference.DaniLabsAPIKey) || strings.Contains(body, cfg.DittoRouter.APIKey) {
		t.Fatal("metrics exposed credential")
	}
	cfg.DittoRouter.APIKey = ""
	withoutRouterKey := httptest.NewRecorder()
	server.New(cfg, logger, nil, nil, "deadbeef").Handler().ServeHTTP(withoutRouterKey, req)
	if !strings.Contains(withoutRouterKey.Body.String(), "ditto_danilabs_binding_router_competition 0\n") {
		t.Fatal("router without a key must not report an active competition lane")
	}
	remote := httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodGet, "/metrics", nil)
	req.RemoteAddr = "192.0.2.1:12345"
	req.Header.Set("X-Forwarded-For", "127.0.0.1")
	h.ServeHTTP(remote, req)
	if strings.Contains(remote.Body.String(), "ditto_danilabs_binding_") {
		t.Fatal("remote caller received private binding metadata")
	}
}

func TestMetricsGzipIsEncodedOnce(t *testing.T) {
	// Ensure the compressed Prometheus response exceeds the middleware's
	// threshold, as it does on a running relay with Go process metrics.
	padding := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: "relay_test_gzip_padding"}, []string{"value"})
	prometheus.MustRegister(padding)
	t.Cleanup(func() { prometheus.Unregister(padding) })
	for i := 0; i < 100; i++ {
		sum := sha256.Sum256([]byte(fmt.Sprint(i)))
		padding.WithLabelValues(fmt.Sprintf("%x", sum)).Set(1)
	}
	for _, addr := range []string{"127.0.0.1:12345", "192.0.2.1:12345"} {
		t.Run(addr, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
			req.RemoteAddr = addr
			req.Header.Set("Accept-Encoding", "gzip")
			rec := httptest.NewRecorder()
			newTestServer().ServeHTTP(rec, req)
			if rec.Header().Get("Content-Encoding") != "gzip" {
				t.Fatalf("expected gzip encoding, got %q", rec.Header().Get("Content-Encoding"))
			}
			reader, err := gzip.NewReader(rec.Body)
			if err != nil {
				t.Fatal(err)
			}
			body, err := io.ReadAll(reader)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(body), "relay_test_gzip_padding") {
				t.Fatal("one decompression did not yield Prometheus metrics")
			}
			if addr == "127.0.0.1:12345" && !strings.Contains(string(body), "ditto_danilabs_binding_scope") {
				t.Fatal("loopback binding metrics missing")
			}
		})
	}
}

func TestInferenceRoutesAbsentUntilRegistered(t *testing.T) {
	h := newTestServer()
	for _, path := range []string{
		"/api/v1/inference/exchange",
		"/api/v1/inference/chat/completions",
		"/api/v1/inference/embeddings",
		"/api/v1/inference/coding/chat/completions",
	} {
		req := httptest.NewRequest(http.MethodPost, path, nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusNotFound {
			t.Errorf("%s: want 404 before handlers are registered, got %d", path, rec.Code)
		}
	}
}

// The Python relay routes every unmatched path and method through the JSON
// error envelope (error_code 3002) and redirects trailing slashes with 307
// (FastAPI redirect_slashes). Clients parse the numeric error_code as the
// authoritative discriminator, so plain-text ServeMux 404/405 bodies would be
// a wire change.
func TestUnmatchedRoutesUseTheErrorEnvelope(t *testing.T) {
	h := newTestServer(server.WithInferenceHandlers(&server.InferenceHandlers{
		Exchange: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusTeapot)
		}),
	}))

	expectEnvelope := func(t *testing.T, rec *httptest.ResponseRecorder, status int, message string) {
		t.Helper()
		if rec.Code != status {
			t.Fatalf("status: want %d, got %d (%s)", status, rec.Code, rec.Body.String())
		}
		if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
			t.Fatalf("Content-Type: want application/json, got %q", ct)
		}
		var envelope struct {
			ErrorCode int    `json:"error_code"`
			Message   string `json:"message"`
			RequestID string `json:"request_id"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
			t.Fatalf("envelope decode (%q): %v", rec.Body.String(), err)
		}
		if envelope.ErrorCode != 3002 || envelope.Message != message || envelope.RequestID == "" {
			t.Fatalf("envelope: %+v (want code 3002, message %q, non-empty request_id)", envelope, message)
		}
	}

	t.Run("unknown path is a 404 envelope", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/nope", nil))
		expectEnvelope(t, rec, http.StatusNotFound, "Not Found")
	})

	t.Run("wrong method is a 405 envelope with Allow", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/inference/exchange", nil))
		expectEnvelope(t, rec, http.StatusMethodNotAllowed, "Method Not Allowed")
		if allow := rec.Header().Get("Allow"); allow != http.MethodPost {
			t.Fatalf("Allow: want POST, got %q", allow)
		}
	})

	t.Run("trailing slash redirects 307 like redirect_slashes", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/inference/exchange/", nil))
		if rec.Code != http.StatusTemporaryRedirect {
			t.Fatalf("status: want 307, got %d (%s)", rec.Code, rec.Body.String())
		}
		if loc := rec.Header().Get("Location"); loc != "/api/v1/inference/exchange" {
			t.Fatalf("Location: %q", loc)
		}
	})

	t.Run("unregistered inference path stays a 404 envelope", func(t *testing.T) {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/inference/embeddings", nil))
		expectEnvelope(t, rec, http.StatusNotFound, "Not Found")
	})
}

func TestInferenceRoutesMountAtExactPaths(t *testing.T) {
	mounted := map[string]int{}
	mark := func(name string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			mounted[name]++
			w.WriteHeader(http.StatusTeapot)
		})
	}
	h := newTestServer(server.WithInferenceHandlers(&server.InferenceHandlers{
		Exchange:                    mark("exchange"),
		ChatCompletions:             mark("chat"),
		Embeddings:                  mark("embeddings"),
		ConfirmationChatCompletions: mark("confirmation-chat"),
		ConfirmationEmbeddings:      mark("confirmation-embeddings"),
		CodingChatCompletions:       mark("coding-chat"),
	}))

	for path, name := range map[string]string{
		"/api/v1/inference/exchange":                      "exchange",
		"/api/v1/inference/chat/completions":              "chat",
		"/api/v1/inference/embeddings":                    "embeddings",
		"/api/v1/inference/confirmation/chat/completions": "confirmation-chat",
		"/api/v1/inference/confirmation/embeddings":       "confirmation-embeddings",
		"/api/v1/inference/coding/chat/completions":       "coding-chat",
	} {
		req := httptest.NewRequest(http.MethodPost, path, nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusTeapot || mounted[name] != 1 {
			t.Errorf("%s: handler %q not mounted at exact path (status %d)", path, name, rec.Code)
		}

		// GET must not match: the routes are POST-only.
		req = httptest.NewRequest(http.MethodGet, path, nil)
		rec = httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code == http.StatusTeapot {
			t.Errorf("%s: GET must not reach the POST handler", path)
		}
	}
}

func TestUploadAdmissionRoutesMountAtExactPaths(t *testing.T) {
	mounted := map[string]int{}
	mark := func(name string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			mounted[name]++
			w.WriteHeader(http.StatusTeapot)
		})
	}
	h := newTestServer(server.WithUploadHandlers(&server.UploadHandlers{
		EvalPricing: mark("pricing"),
		Check:       mark("check"),
	}))
	for _, test := range []struct {
		method string
		path   string
		name   string
	}{
		{http.MethodGet, "/api/v1/upload/eval-pricing", "pricing"},
		{http.MethodPost, "/api/v1/upload/check", "check"},
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(test.method, test.path, nil))
		if rec.Code != http.StatusTeapot || mounted[test.name] != 1 {
			t.Fatalf("%s %s not mounted (status=%d count=%d)", test.method, test.path, rec.Code, mounted[test.name])
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/upload/agent", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("multipart upload must remain absent from Go, got %d", rec.Code)
	}
}
