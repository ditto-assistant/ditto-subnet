package codingrelay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type panicReadCloser struct{}

func (panicReadCloser) Read([]byte) (int, error) { panic("request body was read") }

func (panicReadCloser) Close() error { return nil }

func relayRequest(method, target string, body []byte) *http.Request {
	request := httptest.NewRequest(method, target, bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	return request
}

func decodeRelayError(t *testing.T, recorder *httptest.ResponseRecorder) map[string]string {
	t.Helper()
	var envelope struct {
		Error map[string]string `json:"error"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	return envelope.Error
}

func TestRelayHandlerReturnsOnlyMinerSafeResponse(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	request := relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0])
	request.Header.Set("Authorization", "Bearer miner-placeholder-secret")
	request.Header.Set("HTTP-Referer", "https://miner.invalid/private")
	request.Header.Set("X-OpenRouter-Title", "miner-controlled")
	recorder := httptest.NewRecorder()
	relay.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Cache-Control") != "no-store" ||
		recorder.Header().Get("X-Content-Type-Options") != "nosniff" ||
		recorder.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("headers=%v", recorder.Header())
	}
	parseMinerResponse(t, fixture.policy, recorder.Body.Bytes())
	for _, forbidden := range []string{
		"Azure", "cost_usd_micros", "miner-placeholder-secret", "miner-controlled", "miner.invalid",
	} {
		if strings.Contains(recorder.Body.String(), forbidden) {
			t.Fatalf("response leaked %q: %s", forbidden, recorder.Body.String())
		}
	}
}

func TestRelayHandlerRejectsUnsupportedHTTPShapesBeforeAdmission(t *testing.T) {
	fixture := newRelayFixture(t)
	var calls int
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		calls++
		return UpstreamResult{}, errors.New("must not run")
	})
	tests := map[string]struct {
		request *http.Request
		status  int
	}{
		"path":      {relayRequest(http.MethodPost, "https://relay.invalid/v1/chat/completions", fixture.requests[0]), http.StatusNotFound},
		"query":     {relayRequest(http.MethodPost, "https://relay.invalid/chat/completions?route=other", fixture.requests[0]), http.StatusNotFound},
		"method":    {relayRequest(http.MethodGet, "https://relay.invalid/chat/completions", nil), http.StatusMethodNotAllowed},
		"malformed": {relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", []byte(`{}`)), http.StatusBadRequest},
	}
	wrongMedia := relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0])
	wrongMedia.Header.Set("Content-Type", "text/plain")
	tests["media type"] = struct {
		request *http.Request
		status  int
	}{wrongMedia, http.StatusUnsupportedMediaType}
	encoded := relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0])
	encoded.Header.Set("Content-Encoding", "gzip")
	tests["content encoding"] = struct {
		request *http.Request
		status  int
	}{encoded, http.StatusUnsupportedMediaType}
	oversized := relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", []byte(`{}`))
	oversized.ContentLength = int64(fixture.policy.MaxRequestBytes) + 1
	tests["content length"] = struct {
		request *http.Request
		status  int
	}{oversized, http.StatusRequestEntityTooLarge}

	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			relay, err := New(t.Context(), fixture.config(upstream))
			if err != nil {
				t.Fatal(err)
			}
			recorder := httptest.NewRecorder()
			relay.Handler().ServeHTTP(recorder, test.request)
			if recorder.Code != test.status {
				t.Fatalf("status=%d want=%d body=%s", recorder.Code, test.status, recorder.Body.String())
			}
			failure := decodeRelayError(t, recorder)
			if failure["type"] != "coding_relay_error" || failure["code"] == "" || failure["message"] == "" {
				t.Fatalf("error=%v", failure)
			}
		})
	}
	if calls != 0 || fixture.journal.begins != 0 {
		t.Fatalf("invalid requests called upstream=%d or journal=%d", calls, fixture.journal.begins)
	}
}

func TestRelayHandlerMapsCapabilityBudgetAndProviderFailures(t *testing.T) {
	t.Run("revoked", func(t *testing.T) {
		fixture := newRelayFixture(t)
		relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})))
		if err != nil {
			t.Fatal(err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		recorder := httptest.NewRecorder()
		relay.Handler().ServeHTTP(recorder, relayRequest(
			http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0],
		))
		if recorder.Code != http.StatusGone || decodeRelayError(t, recorder)["code"] != "capability_revoked" {
			t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
		}
	})

	t.Run("budget", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.binding.RequestBudget = 1
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.completeResult(t, request, 0), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		first := httptest.NewRecorder()
		relay.Handler().ServeHTTP(first, relayRequest(
			http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0],
		))
		if first.Code != http.StatusOK {
			t.Fatalf("first status=%d body=%s", first.Code, first.Body.String())
		}
		second := httptest.NewRecorder()
		relay.Handler().ServeHTTP(second, relayRequest(
			http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[1],
		))
		if second.Code != http.StatusTooManyRequests || decodeRelayError(t, second)["code"] != "budget_exhausted" {
			t.Fatalf("second status=%d body=%s", second.Code, second.Body.String())
		}
	})

	t.Run("provider failure", func(t *testing.T) {
		fixture := newRelayFixture(t)
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.failureResult(request), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		recorder := httptest.NewRecorder()
		relay.Handler().ServeHTTP(recorder, relayRequest(
			http.MethodPost, "https://relay.invalid/chat/completions", fixture.requests[0],
		))
		failure := decodeRelayError(t, recorder)
		if recorder.Code != http.StatusBadGateway || failure["code"] != "provider_failure" ||
			strings.Contains(recorder.Body.String(), "provider_timeout") || strings.Contains(recorder.Body.String(), "Azure") {
			t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
		}
	})
}

func TestNilRelayHandlerFailsClosed(t *testing.T) {
	var relay *Relay
	recorder := httptest.NewRecorder()
	relay.Handler().ServeHTTP(recorder, relayRequest(
		http.MethodPost, "https://relay.invalid/chat/completions", []byte(`{}`),
	))
	if recorder.Code != http.StatusBadGateway || decodeRelayError(t, recorder)["code"] != "relay_unavailable" {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestRelayHandlerRejectsExcessParserConcurrencyBeforeReadingBody(t *testing.T) {
	fixture := newRelayFixture(t)
	relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New("unused")
	})))
	if err != nil {
		t.Fatal(err)
	}
	if !relay.acquireRequest() || !relay.acquireRequest() {
		t.Fatal("failed to reserve bounded parser slots")
	}
	defer relay.releaseRequest()
	defer relay.releaseRequest()
	request := relayRequest(http.MethodPost, "https://relay.invalid/chat/completions", nil)
	request.Body = panicReadCloser{}
	request.ContentLength = -1
	recorder := httptest.NewRecorder()
	relay.Handler().ServeHTTP(recorder, request)
	if recorder.Code != http.StatusConflict || decodeRelayError(t, recorder)["code"] != "concurrent_request" {
		t.Fatalf("status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}
