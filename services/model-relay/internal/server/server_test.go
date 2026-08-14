package server_test

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/server"

	"github.com/ditto-assistant/model-relay/internal/metrics"
)

func testConfig() *config.Config {
	cfg, err := config.Load(config.MapLookup(map[string]string{
		"POSTGRES_USER":           "x",
		"POSTGRES_PASSWORD":       "x",
		"POSTGRES_DB":             "x",
		"PYLON_OPEN_ACCESS_TOKEN": "x",
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

func TestInferenceRoutesAbsentUntilRegistered(t *testing.T) {
	h := newTestServer()
	for _, path := range []string{
		"/api/v1/inference/exchange",
		"/api/v1/inference/chat/completions",
		"/api/v1/inference/embeddings",
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
	}))

	for path, name := range map[string]string{
		"/api/v1/inference/exchange":                      "exchange",
		"/api/v1/inference/chat/completions":              "chat",
		"/api/v1/inference/embeddings":                    "embeddings",
		"/api/v1/inference/confirmation/chat/completions": "confirmation-chat",
		"/api/v1/inference/confirmation/embeddings":       "confirmation-embeddings",
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
