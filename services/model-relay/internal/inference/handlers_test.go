package inference

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

type stubPermits struct {
	permitted bool
	err       error
}

func (s *stubPermits) ValidatorPermit(ctx context.Context, hotkey string) (bool, error) {
	return s.permitted, s.err
}

func testConfig(t *testing.T, extra map[string]string) *config.Config {
	t.Helper()
	env := map[string]string{
		"POSTGRES_USER":                 "u",
		"POSTGRES_PASSWORD":             "p",
		"POSTGRES_DB":                   "d",
		"PYLON_OPEN_ACCESS_TOKEN":       "tok",
		"DITTO_UPLOAD_PAYMENT_ADDRESS":  "5NotARea1SS58AddressTestFixtureDoNotSendTaoHere",
		"DITTO_INFERENCE_PROXY_ENABLED": "1",
		"OPENROUTER_API_KEY":            "test-openrouter-key",
	}
	for k, v := range extra {
		env[k] = v
	}
	cfg, err := config.Load(config.MapLookup(env))
	if err != nil {
		t.Fatalf("config: %v", err)
	}
	return cfg
}

func newGateDeps(t *testing.T, cfg *config.Config) *Deps {
	t.Helper()
	return &Deps{
		Cfg:     cfg,
		Logger:  slog.New(slog.NewTextHandler(testWriter{t}, nil)),
		Permits: &stubPermits{permitted: true},
		Sleep:   func(context.Context, time.Duration) {},
	}
}

type testWriter struct{ t *testing.T }

func (w testWriter) Write(p []byte) (int, error) {
	w.t.Log(strings.TrimSuffix(string(p), "\n"))
	return len(p), nil
}

// serve routes a request through the real middleware stack so the envelope
// carries a request id, like production.
func serve(deps *Deps, r *http.Request) *httptest.ResponseRecorder {
	handlers := NewHandlers(deps)
	mux := http.NewServeMux()
	mux.Handle("POST /api/v1/inference/exchange", handlers.Exchange)
	mux.Handle("POST /api/v1/inference/chat/completions", handlers.ChatCompletions)
	mux.Handle("POST /api/v1/inference/embeddings", handlers.Embeddings)
	mux.Handle("POST /api/v1/inference/confirmation/chat/completions", handlers.ConfirmationChatCompletions)
	mux.Handle("POST /api/v1/inference/confirmation/embeddings", handlers.ConfirmationEmbeddings)
	logger := slog.New(slog.NewTextHandler(nullWriter{}, nil))
	h := relayhttp.RequestIDMiddleware(logger, relayhttp.RecoverMiddleware(logger, mux))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

type nullWriter struct{}

func (nullWriter) Write(p []byte) (int, error) { return len(p), nil }

func decodeEnvelope(t *testing.T, w *httptest.ResponseRecorder) relayhttp.ErrorEnvelope {
	t.Helper()
	var env relayhttp.ErrorEnvelope
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("envelope decode (%q): %v", w.Body.String(), err)
	}
	return env
}

func expectEnvelope(t *testing.T, w *httptest.ResponseRecorder, status, code int, message string) {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status: want %d, got %d (%s)", status, w.Code, w.Body.String())
	}
	env := decodeEnvelope(t, w)
	if env.ErrorCode != code {
		t.Fatalf("error_code: want %d, got %d (%s)", code, env.ErrorCode, w.Body.String())
	}
	if message != "" && env.Message != message {
		t.Fatalf("message: want %q, got %q", message, env.Message)
	}
}

const validHotkey = "5FTestHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

func exchangeBody(overrides map[string]any) string {
	body := map[string]any{
		"validator_hotkey":  validHotkey,
		"grant_id":          uuid.New().String(),
		"broker_public_key": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"nonce":             uuid.New().String(),
		"requested_at":      time.Now().UTC().Format("2006-01-02T15:04:05.000000") + "+00:00",
		"signature":         strings.Repeat("ab", 64),
	}
	for k, v := range overrides {
		if v == nil {
			delete(body, k)
		} else {
			body[k] = v
		}
	}
	encoded, _ := json.Marshal(body)
	return string(encoded)
}

func postExchange(deps *Deps, body string, hotkeyHeader string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodPost, "/api/v1/inference/exchange", strings.NewReader(body))
	if hotkeyHeader != "" {
		r.Header.Set("X-Validator-Hotkey", hotkeyHeader)
	}
	return serve(deps, r)
}

func TestExchangeValidation(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, nil))

	// Malformed / incomplete bodies -> 422 3001 (Pydantic parity), and this
	// precedes even the proxy-disabled 404.
	for name, body := range map[string]string{
		"not json":         "{",
		"unknown field":    exchangeBody(map[string]any{"extra": 1}),
		"missing field":    exchangeBody(map[string]any{"signature": nil}),
		"bad hotkey":       exchangeBody(map[string]any{"validator_hotkey": "0Oli"}),
		"bad broker key":   exchangeBody(map[string]any{"broker_public_key": "short"}),
		"bad signature":    exchangeBody(map[string]any{"signature": "zzzz"}),
		"bad uuid":         exchangeBody(map[string]any{"grant_id": "nope"}),
		"naive timestamp":  exchangeBody(map[string]any{"requested_at": "2026-08-13T12:00:00"}),
		"non-string field": exchangeBody(map[string]any{"nonce": 7}),
	} {
		t.Run(name, func(t *testing.T) {
			w := postExchange(deps, body, validHotkey)
			expectEnvelope(t, w, 422, relayhttp.CodeRequestValidation, "request validation failed")
		})
	}

	t.Run("disabled proxy", func(t *testing.T) {
		disabled := newGateDeps(t, testConfig(t, map[string]string{"DITTO_INFERENCE_PROXY_ENABLED": "0", "OPENROUTER_API_KEY": ""}))
		w := postExchange(disabled, exchangeBody(nil), validHotkey)
		expectEnvelope(t, w, 404, relayhttp.CodeHTTPException, "inference proxy is disabled")
	})

	t.Run("hotkey header mismatch", func(t *testing.T) {
		w := postExchange(deps, exchangeBody(nil), "5FQtherHotkeyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
		expectEnvelope(t, w, 401, relayhttp.CodeValidatorAuth, "validator authentication failed")
	})

	t.Run("missing hotkey header", func(t *testing.T) {
		w := postExchange(deps, exchangeBody(nil), "")
		expectEnvelope(t, w, 401, relayhttp.CodeValidatorAuth, "validator authentication failed")
	})

	t.Run("stale request", func(t *testing.T) {
		stale := time.Now().UTC().Add(-3*time.Minute).Format("2006-01-02T15:04:05.000000") + "+00:00"
		w := postExchange(deps, exchangeBody(map[string]any{"requested_at": stale}), validHotkey)
		expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "inference exchange is stale")
	})

	t.Run("future skew also stale", func(t *testing.T) {
		future := time.Now().UTC().Add(3*time.Minute).Format("2006-01-02T15:04:05.000000") + "+00:00"
		w := postExchange(deps, exchangeBody(map[string]any{"requested_at": future}), validHotkey)
		expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "inference exchange is stale")
	})

	t.Run("bad signature", func(t *testing.T) {
		// Well-formed hex that does not verify.
		w := postExchange(deps, exchangeBody(nil), validHotkey)
		expectEnvelope(t, w, 401, relayhttp.CodeValidatorAuth, "validator authentication failed")
	})
}

func proxyRequest(path string, body string, headers map[string]string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	return r
}

func validProxyHeaders() map[string]string {
	return map[string]string{
		"X-Ditto-Grant":        uuid.New().String(),
		"X-Ditto-Generation":   "1",
		"X-Ditto-Nonce":        uuid.New().String(),
		"X-Ditto-Requested-At": time.Now().UTC().Format(time.RFC3339Nano),
		"X-Ditto-Proof":        "cHJvb2Y",
		"Authorization":        "Bearer some-bearer-token-value-that-is-long-enough",
	}
}

func TestChatGates(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, nil))
	const path = "/api/v1/inference/chat/completions"

	t.Run("malformed header precedes everything", func(t *testing.T) {
		disabled := newGateDeps(t, testConfig(t, map[string]string{"DITTO_INFERENCE_PROXY_ENABLED": "0", "OPENROUTER_API_KEY": ""}))
		h := validProxyHeaders()
		h["X-Ditto-Grant"] = "not-a-uuid"
		w := serve(disabled, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 422, relayhttp.CodeRequestValidation, "request validation failed")
	})

	t.Run("malformed generation", func(t *testing.T) {
		h := validProxyHeaders()
		h["X-Ditto-Generation"] = "one"
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 422, relayhttp.CodeRequestValidation, "request validation failed")
	})

	t.Run("malformed requested-at", func(t *testing.T) {
		h := validProxyHeaders()
		h["X-Ditto-Requested-At"] = "yesterday"
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 422, relayhttp.CodeRequestValidation, "request validation failed")
	})

	t.Run("disabled proxy", func(t *testing.T) {
		disabled := newGateDeps(t, testConfig(t, map[string]string{"DITTO_INFERENCE_PROXY_ENABLED": "0", "OPENROUTER_API_KEY": ""}))
		w := serve(disabled, proxyRequest(path, "{}", validProxyHeaders()))
		expectEnvelope(t, w, 404, relayhttp.CodeHTTPException, "inference proxy is disabled")
	})

	t.Run("missing headers", func(t *testing.T) {
		h := validProxyHeaders()
		delete(h, "X-Ditto-Proof")
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "missing inference proof")
	})

	t.Run("bad authorization scheme", func(t *testing.T) {
		h := validProxyHeaders()
		h["Authorization"] = "Basic zzz"
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid inference proof")
	})

	t.Run("body too large", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, strings.Repeat("x", 262145), validProxyHeaders()))
		expectEnvelope(t, w, 413, relayhttp.CodeHTTPException, "inference request is too large")
	})

	t.Run("stale request", func(t *testing.T) {
		h := validProxyHeaders()
		h["X-Ditto-Requested-At"] = time.Now().UTC().Add(-time.Minute).Format(time.RFC3339Nano)
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "inference request is stale")
	})

	t.Run("invalid JSON", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, "{", validProxyHeaders()))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException, "invalid JSON request")
	})

	t.Run("non-object JSON", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, "[1,2]", validProxyHeaders()))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException, "inference request must be a JSON object")
	})

	t.Run("stream true is refused legibly", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, `{"stream":true,"messages":[{"role":"user","content":"hi"}]}`, validProxyHeaders()))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException,
			"unsupported inference parameter: stream (this lane answers with a single non-streaming response)")
	})

	t.Run("missing model is 403", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, `{"messages":[{"role":"user","content":"hi"}]}`, validProxyHeaders()))
		expectEnvelope(t, w, 403, relayhttp.CodeHTTPException, "model is not permitted")
	})

	t.Run("request id echoed", func(t *testing.T) {
		r := proxyRequest(path, "{", validProxyHeaders())
		r.Header.Set("X-Request-ID", "test-rid-123")
		w := serve(deps, r)
		if w.Header().Get("X-Request-ID") != "test-rid-123" {
			t.Fatalf("request id must echo, got %q", w.Header().Get("X-Request-ID"))
		}
		env := decodeEnvelope(t, w)
		if env.RequestID != "test-rid-123" {
			t.Fatalf("envelope request_id: %q", env.RequestID)
		}
	})
}

func TestEmbeddingGates(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, nil))
	const path = "/api/v1/inference/embeddings"

	t.Run("body too large", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, strings.Repeat("x", 1024*1024+1), validProxyHeaders()))
		expectEnvelope(t, w, 413, relayhttp.CodeHTTPException, "embedding request is too large")
	})

	t.Run("stale request", func(t *testing.T) {
		h := validProxyHeaders()
		h["X-Ditto-Requested-At"] = time.Now().UTC().Add(-31 * time.Second).Format(time.RFC3339Nano)
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "embedding request is stale")
	})

	t.Run("invalid embedding payloads", func(t *testing.T) {
		for name, body := range map[string]string{
			"empty object":     `{}`,
			"wrong model":      `{"model":"other","input":["x"],"dimensions":768,"encoding_format":"float"}`,
			"extra key":        `{"model":"perplexity/pplx-embed-v1-0.6b","input":["x"],"dimensions":768,"encoding_format":"float","user":"u"}`,
			"wrong dims":       `{"model":"perplexity/pplx-embed-v1-0.6b","input":["x"],"dimensions":512,"encoding_format":"float"}`,
			"wrong format":     `{"model":"perplexity/pplx-embed-v1-0.6b","input":["x"],"dimensions":768,"encoding_format":"base64"}`,
			"empty input":      `{"model":"perplexity/pplx-embed-v1-0.6b","input":[],"dimensions":768,"encoding_format":"float"}`,
			"empty string":     `{"model":"perplexity/pplx-embed-v1-0.6b","input":[""],"dimensions":768,"encoding_format":"float"}`,
			"non-string input": `{"model":"perplexity/pplx-embed-v1-0.6b","input":[1],"dimensions":768,"encoding_format":"float"}`,
		} {
			t.Run(name, func(t *testing.T) {
				w := serve(deps, proxyRequest(path, body, validProxyHeaders()))
				expectEnvelope(t, w, 400, relayhttp.CodeHTTPException, "invalid embedding request")
			})
		}
	})
}

func TestConfirmationGates(t *testing.T) {
	deps := newGateDeps(t, testConfig(t, nil))
	const path = "/api/v1/inference/confirmation/chat/completions"

	t.Run("malformed header precedes everything", func(t *testing.T) {
		disabled := newGateDeps(t, testConfig(t, map[string]string{"DITTO_INFERENCE_PROXY_ENABLED": "0", "OPENROUTER_API_KEY": ""}))
		h := validProxyHeaders()
		h["X-Ditto-Grant"] = "not-a-uuid"
		w := serve(disabled, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 422, relayhttp.CodeRequestValidation, "request validation failed")
	})

	t.Run("disabled proxy", func(t *testing.T) {
		disabled := newGateDeps(t, testConfig(t, map[string]string{"DITTO_INFERENCE_PROXY_ENABLED": "0", "OPENROUTER_API_KEY": ""}))
		w := serve(disabled, proxyRequest(path, "{}", validProxyHeaders()))
		expectEnvelope(t, w, 404, relayhttp.CodeHTTPException, "confirmation proxy is disabled")
	})

	t.Run("missing headers", func(t *testing.T) {
		h := validProxyHeaders()
		delete(h, "X-Ditto-Proof")
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "missing confirmation proof")
	})

	t.Run("bad authorization scheme is also the missing-proof 401", func(t *testing.T) {
		// Unlike the ordinary lane, _confirmation_headers folds a non-Bearer
		// Authorization into the same uniform 401 detail.
		h := validProxyHeaders()
		h["Authorization"] = "Basic zzz"
		w := serve(deps, proxyRequest(path, "{}", h))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "missing confirmation proof")
	})

	t.Run("stale request precedes the body-size gate", func(t *testing.T) {
		// _confirmation_headers runs before request.body() in Python, so an
		// oversized stale request answers 409, not 413.
		h := validProxyHeaders()
		h["X-Ditto-Requested-At"] = time.Now().UTC().Add(-time.Minute).Format(time.RFC3339Nano)
		w := serve(deps, proxyRequest(path, strings.Repeat("x", 262145), h))
		expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "confirmation request is stale")
	})

	t.Run("body too large", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, strings.Repeat("x", 262145), validProxyHeaders()))
		expectEnvelope(t, w, 413, relayhttp.CodeHTTPException, "confirmation request is too large")
	})

	t.Run("invalid JSON", func(t *testing.T) {
		w := serve(deps, proxyRequest(path, "{", validProxyHeaders()))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException, "invalid JSON request")
	})

	t.Run("embedding lane gates", func(t *testing.T) {
		const embPath = "/api/v1/inference/confirmation/embeddings"
		w := serve(deps, proxyRequest(embPath, strings.Repeat("x", 1024*1024+1), validProxyHeaders()))
		expectEnvelope(t, w, 413, relayhttp.CodeHTTPException, "embedding request is too large")
		w = serve(deps, proxyRequest(embPath, `{"model":"other","input":["x"],"dimensions":768,"encoding_format":"float"}`, validProxyHeaders()))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException, "invalid embedding request")
	})
}

func TestPythonFloatRepr(t *testing.T) {
	for value, want := range map[float64]string{
		0.0021:  "0.0021",
		0.1:     "0.1",
		2.0:     "2.0",
		0.0001:  "0.0001",
		5e-05:   "5e-05",
		1e-06:   "1e-06",
		0:       "0.0",
		12.3456: "12.3456",
	} {
		if got := pythonFloatRepr(value); got != want {
			t.Errorf("pythonFloatRepr(%v): want %q, got %q", value, want, got)
		}
	}
}

func TestParseHeaderDatetime(t *testing.T) {
	if _, aware, err := parseHeaderDatetime("2026-08-13T12:00:00.000000+00:00"); err != nil || !aware {
		t.Fatalf("offset form must parse aware: %v", err)
	}
	if _, aware, err := parseHeaderDatetime("2026-08-13T12:00:00Z"); err != nil || !aware {
		t.Fatalf("Z form must parse aware: %v", err)
	}
	if _, aware, err := parseHeaderDatetime("2026-08-13T12:00:00"); err != nil || aware {
		t.Fatalf("naive form must parse as not-aware: %v", err)
	}
	if _, _, err := parseHeaderDatetime("not-a-date"); err == nil {
		t.Fatal("garbage must fail")
	}
}
