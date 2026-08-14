package relayhttp

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"testing"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestRequestIDAcceptedWhenValid(t *testing.T) {
	var seen string
	h := RequestIDMiddleware(discardLogger(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = RequestID(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	req.Header.Set("X-Request-ID", "abc.DEF_123-xyz")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if seen != "abc.DEF_123-xyz" {
		t.Fatalf("valid inbound id must be kept, got %q", seen)
	}
	if got := rec.Header().Get("X-Request-ID"); got != "abc.DEF_123-xyz" {
		t.Fatalf("id must be echoed, got %q", got)
	}
}

func TestRequestIDReplacedWhenInvalid(t *testing.T) {
	uuidHex := regexp.MustCompile(`^[0-9a-f]{32}$`)
	for _, bad := range []string{"", "has space", "über", string(make([]byte, 65))} {
		var seen string
		h := RequestIDMiddleware(discardLogger(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			seen = RequestID(r.Context())
		}))
		req := httptest.NewRequest(http.MethodGet, "/health", nil)
		if bad != "" {
			req.Header.Set("X-Request-ID", bad)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)

		if !uuidHex.MatchString(seen) {
			t.Errorf("invalid inbound %q must be replaced with uuid4 hex, got %q", bad, seen)
		}
		if rec.Header().Get("X-Request-ID") != seen {
			t.Errorf("generated id must be echoed")
		}
	}
}

func TestErrorEnvelopeShape(t *testing.T) {
	h := RequestIDMiddleware(discardLogger(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		WriteHTTPError(w, r, http.StatusConflict, "inference exchange is stale", nil)
	}))
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/exchange", nil)
	req.Header.Set("X-Request-ID", "rid-1")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status: %d", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("content type: %q", ct)
	}

	// The wire shape is exactly {"error_code": int, "message": str,
	// "request_id": str} — assert via a raw map so extra fields fail too.
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %v", err)
	}
	if len(body) != 3 {
		t.Fatalf("envelope must have exactly 3 fields, got %v", body)
	}
	if body["error_code"] != float64(3002) || body["message"] != "inference exchange is stale" || body["request_id"] != "rid-1" {
		t.Fatalf("envelope wrong: %v", body)
	}
}

func TestDeclineWireMapping(t *testing.T) {
	cases := []struct {
		decline Decline
		lane    string
		status  int
		code    int
		message string
	}{
		{DeclineUnattributed, LaneInference, 429, 4100, "inference grant unavailable, and the reason is deliberately not disclosed to an unauthenticated caller"},
		{DeclineGrantRevoked, LaneInference, 429, 4101, "inference grant was revoked"},
		{DeclineBudgetExhausted, LaneEmbedding, 429, 4102, "embedding grant has spent its request budget"},
		{DeclineAtCapacity, LaneInference, 503, 4103, "inference lane is at capacity"},
		{DeclineAtCapacity, LaneEmbedding, 503, 4103, "embedding lane is at capacity"},
		{DeclineTokenBudgetExhausted, LaneInference, 429, 4104, "inference grant has spent its token budget"},
		{DeclineLeaseExpired, LaneInference, 429, 4105, "inference grant has expired"},
		{DeclineNonceReplayed, LaneEmbedding, 429, 4106, "embedding request nonce was already used"},
		{DeclineModelNotPermitted, LaneInference, 429, 4107, "inference grant does not permit this model"},
		{DeclineGrantNotExchanged, LaneInference, 429, 4108, "inference grant has not been exchanged for a bearer"},
		{DeclineReservationTooLarge, LaneInference, 429, 4109, "inference request exceeds the grant's entire token budget"},
	}

	for _, tc := range cases {
		h := RequestIDMiddleware(discardLogger(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			WriteDecline(w, r, tc.decline, tc.lane)
		}))
		req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/chat/completions", nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)

		if rec.Code != tc.status {
			t.Errorf("code %d: status got %d want %d", tc.code, rec.Code, tc.status)
		}
		var body ErrorEnvelope
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("code %d: body: %v", tc.code, err)
		}
		if body.ErrorCode != tc.code {
			t.Errorf("error_code got %d want %d", body.ErrorCode, tc.code)
		}
		if body.Message != tc.message {
			t.Errorf("code %d: message got %q want %q", tc.code, body.Message, tc.message)
		}

		retryAfter := rec.Header().Get("Retry-After")
		if tc.decline == DeclineAtCapacity {
			if retryAfter != "1" {
				t.Errorf("AT_CAPACITY must carry Retry-After: 1, got %q", retryAfter)
			}
		} else if retryAfter != "" {
			t.Errorf("code %d: terminal decline must not carry Retry-After", tc.code)
		}

		if tc.decline.Retryable() != (tc.decline == DeclineAtCapacity) {
			t.Errorf("only AT_CAPACITY is retryable")
		}
	}
}

func TestRecoverMiddlewareWritesInternalEnvelope(t *testing.T) {
	h := RequestIDMiddleware(discardLogger(), RecoverMiddleware(discardLogger(),
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			panic("boom")
		})))
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status: %d", rec.Code)
	}
	var body ErrorEnvelope
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body: %v", err)
	}
	if body.ErrorCode != CodeInternalError || body.Message != "internal server error" {
		t.Fatalf("catch-all envelope wrong: %+v", body)
	}
}

func TestValidationAndAuthEnvelopes(t *testing.T) {
	h := RequestIDMiddleware(discardLogger(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/validation":
			WriteValidationError(w, r)
		case "/auth":
			WriteValidatorAuthError(w, r)
		}
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/validation", nil))
	var body ErrorEnvelope
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if rec.Code != 422 || body.ErrorCode != 3001 || body.Message != "request validation failed" {
		t.Fatalf("validation envelope wrong: %d %+v", rec.Code, body)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/auth", nil))
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if rec.Code != 401 || body.ErrorCode != 4000 || body.Message != "validator authentication failed" {
		t.Fatalf("auth envelope wrong: %d %+v", rec.Code, body)
	}
}
