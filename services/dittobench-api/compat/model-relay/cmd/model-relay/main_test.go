package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (fn roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return fn(req) }

type cancelingBody struct{ cancel context.CancelFunc }

func (b cancelingBody) Read([]byte) (int, error) {
	b.cancel()
	return 0, context.Canceled
}

func (cancelingBody) Close() error { return nil }

func TestDisabledHandlerHasNoInferenceSurface(t *testing.T) {
	recorder := httptest.NewRecorder()
	disabledHandler(
		recorder,
		httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil),
	)
	if recorder.Code != http.StatusGone || recorder.Body.String() != `{"status":"disabled"}` {
		t.Fatalf("disabled response = %d %s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("disabled response must not be cached")
	}
}

// postAndHealth drives one chat request through the relay's mux and returns the
// chat response together with the /health snapshot.
func postAndHealth(t *testing.T, r *relay, reqBody string) (*http.Response, relayHealth) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/chat/completions", r.handle)
	mux.HandleFunc("GET /health", r.health)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	resp, err := http.Post(srv.URL+"/v1/chat/completions", "application/json", strings.NewReader(reqBody))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	healthResp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer healthResp.Body.Close()
	var got relayHealth
	if err := json.NewDecoder(healthResp.Body).Decode(&got); err != nil {
		t.Fatal(err)
	}
	return resp, got
}

// A transient first response parks the run without reaching a later success.
func TestRelayTransientFailureIsSingleShot(t *testing.T) {
	const k = 3
	var hits int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if atomic.AddInt32(&hits, 1) <= k {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte("unavailable"))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}`))
	}))
	defer upstream.Close()

	r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: &http.Client{Timeout: 5 * time.Second}}
	resp, got := postAndHealth(t, r, `{"messages":[]}`)

	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("want first 503 to surface, got %d", resp.StatusCode)
	}
	if got.Requests != 1 || got.Successes != 0 || got.InfrastructureFailures != 1 {
		t.Fatalf("health = %#v (want requests=1 successes=0 failures=1)", got)
	}
	if got.UsageAvailable != 0 || got.PromptTokens != 0 || got.CompletionTokens != 0 {
		t.Fatalf("failed single dispatch must not report successful usage: %#v", got)
	}
	if n := atomic.LoadInt32(&hits); n != 1 {
		t.Fatalf("upstream hit %d times, want one", n)
	}
}

// A persistently failing upstream is called once and counted once.
func TestRelayPersistentFailureIsSingleShot(t *testing.T) {
	var hits int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("still unavailable"))
	}))
	defer upstream.Close()

	r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: &http.Client{Timeout: 5 * time.Second}}
	resp, got := postAndHealth(t, r, `{"messages":[]}`)

	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("want the failing 503 to surface, got %d", resp.StatusCode)
	}
	if got.Requests != 1 || got.Successes != 0 || got.InfrastructureFailures != 1 {
		t.Fatalf("health = %#v (want requests=1 successes=0 failures=1)", got)
	}
	if n := atomic.LoadInt32(&hits); n != 1 {
		t.Fatalf("upstream hit %d times, want one", n)
	}
}

// A transport-level failure surfaces immediately as one infrastructure failure.
func TestRelayTransportErrorIsSingleShot(t *testing.T) {
	// A closed listener yields a connection error on every attempt.
	closed := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	addr := closed.URL
	closed.Close()

	r := &relay{upstream: addr, apiKey: "k", model: "m", client: &http.Client{Timeout: 2 * time.Second}}
	resp, got := postAndHealth(t, r, `{"messages":[]}`)

	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("want 502 on exhausted transport error, got %d", resp.StatusCode)
	}
	if got.Requests != 1 || got.Successes != 0 || got.InfrastructureFailures != 1 {
		t.Fatalf("health = %#v (want requests=1 successes=0 failures=1)", got)
	}
}

// TestRelayDoesNotRetryClientError: a deterministic 4xx (400) is a client error,
// not a transient outage — it is forwarded once with no retry and no counter
// movement (matches the pre-retry pass-through behavior).
func TestRelayDoesNotRetryClientError(t *testing.T) {
	var hits int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&hits, 1)
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte("bad request"))
	}))
	defer upstream.Close()

	r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: &http.Client{Timeout: 5 * time.Second}}
	resp, got := postAndHealth(t, r, `{"messages":[]}`)

	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 forwarded, got %d", resp.StatusCode)
	}
	if got.Requests != 1 || got.Successes != 0 || got.InfrastructureFailures != 0 {
		t.Fatalf("health = %#v (want requests=1 successes=0 failures=0)", got)
	}
	if n := atomic.LoadInt32(&hits); n != 1 {
		t.Fatalf("client error must not be retried: upstream hit %d times", n)
	}
}

// TestRelayCallerCancellationVsDeadline draws the line the fail-closed run health
// depends on: a caller that ABANDONS the request (context.Canceled — the harness
// early-exited or tore the run down) is not a provider failure, so the relay
// returns without retrying and without tainting the run. A context that EXPIRED
// because the upstream was genuinely too slow (context.DeadlineExceeded) is a
// real infrastructure failure and is still counted.
func TestRelayCallerCancellationVsDeadline(t *testing.T) {
	for _, tc := range []struct {
		name          string
		ctxErr        error
		wantFailures  uint64
		wantCancelled uint64
		wantHits      int32
	}{
		{name: "caller cancelled", ctxErr: context.Canceled, wantFailures: 0, wantCancelled: 1, wantHits: 0},
		{name: "deadline exceeded", ctxErr: context.DeadlineExceeded, wantFailures: 1, wantCancelled: 0, wantHits: 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var hits int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				atomic.AddInt32(&hits, 1)
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}]}`))
			}))
			defer upstream.Close()

			var ctx context.Context
			if tc.ctxErr == context.Canceled {
				c, cancel := context.WithCancel(context.Background())
				cancel()
				ctx = c
			} else {
				c, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
				defer cancel()
				ctx = c
			}

			r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: &http.Client{}}
			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"messages":[]}`)).WithContext(ctx)
			req.Header.Set("Content-Type", "application/json")
			r.handle(httptest.NewRecorder(), req)

			if got := r.infrastructureFailures.Load(); got != tc.wantFailures {
				t.Fatalf("infrastructure_failures = %d, want %d", got, tc.wantFailures)
			}
			if got := r.successes.Load(); got != 0 {
				t.Fatalf("successes = %d, want 0", got)
			}
			if got := r.callerCancellations.Load(); got != tc.wantCancelled {
				t.Fatalf("caller_cancellations = %d, want %d", got, tc.wantCancelled)
			}
			if got := r.upstreamAttempts.Load(); got != 1 {
				t.Fatalf("upstream_attempts = %d, want 1", got)
			}
			if n := atomic.LoadInt32(&hits); n != tc.wantHits {
				t.Fatalf("upstream hits = %d, want %d", n, tc.wantHits)
			}
		})
	}
}

func TestRelayCallerCancellationDuringBodyReadDoesNotTaintRun(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       cancelingBody{cancel: cancel},
		}, nil
	})}
	r := &relay{upstream: "http://provider.invalid", apiKey: "k", model: "m", client: client}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"messages":[]}`)).WithContext(ctx)
	req.Header.Set("Content-Type", "application/json")
	r.handle(httptest.NewRecorder(), req)

	if got := r.infrastructureFailures.Load(); got != 0 {
		t.Fatalf("caller-cancelled body read tainted run with %d failure(s)", got)
	}
	if got := r.successes.Load(); got != 0 {
		t.Fatalf("incomplete response counted as %d success(es)", got)
	}
	if got := r.usageAvailable.Load(); got != 0 {
		t.Fatalf("incomplete response contributed %d usage record(s)", got)
	}
	if got := r.callerCancellations.Load(); got != 1 {
		t.Fatalf("caller_cancellations = %d, want 1", got)
	}
}

func TestRelayHealthTracksAuthoritativeUpstreamOutcomes(t *testing.T) {
	for _, tc := range []struct {
		name         string
		status       int
		body         string
		wantSuccess  uint64
		wantFailures uint64
	}{
		{name: "completion", status: http.StatusOK, body: `{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":13}}`, wantSuccess: 1},
		{name: "provider unavailable", status: http.StatusServiceUnavailable, body: `unavailable`, wantFailures: 1},
		{name: "rate limited", status: http.StatusTooManyRequests, body: `limited`, wantFailures: 1},
		{name: "bad credentials", status: http.StatusUnauthorized, body: `unauthorized`, wantFailures: 1},
		{name: "malformed success", status: http.StatusOK, body: `{"choices":[]}`, wantFailures: 1},
		{name: "harness request error", status: http.StatusBadRequest, body: `bad request`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer upstream.Close()
			r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: &http.Client{Timeout: 5 * time.Second}}
			mux := http.NewServeMux()
			mux.HandleFunc("POST /v1/chat/completions", r.handle)
			mux.HandleFunc("GET /health", r.health)
			srv := httptest.NewServer(mux)
			defer srv.Close()

			resp, err := http.Post(srv.URL+"/v1/chat/completions", "application/json", strings.NewReader(`{"messages":[]}`))
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			healthResp, err := http.Get(srv.URL + "/health")
			if err != nil {
				t.Fatal(err)
			}
			defer healthResp.Body.Close()
			var got relayHealth
			if err := json.NewDecoder(healthResp.Body).Decode(&got); err != nil {
				t.Fatal(err)
			}
			if got.AccountingVersion != 2 || got.Status != "ok" || got.Requests != 1 || got.Successes != tc.wantSuccess || got.InfrastructureFailures != tc.wantFailures {
				t.Fatalf("health = %#v", got)
			}
			if tc.wantSuccess == 1 && (got.UsageAvailable != 1 || got.UsageUnavailable != 0 || got.PromptTokens != 11 || got.CompletionTokens != 2) {
				t.Fatalf("usage accounting = %#v", got)
			}
			if got.PromptBytes == 0 {
				t.Fatalf("relay did not measure its pinned prompt payload: %#v", got)
			}
		})
	}
}

// TestRelayPinsModelAndKey: whatever model, stream setting, or Authorization
// header the sandbox sends, the upstream sees the locked model, stream:false,
// and the relay's own key.
func TestRelayPinsModelAndKey(t *testing.T) {
	var got map[string]any
	var auth string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}]}`))
	}))
	defer upstream.Close()

	rl := &relay{upstream: upstream.URL, apiKey: "relay-key", model: "locked/model", thinking: false, client: &http.Client{Timeout: 5 * time.Second}}
	srv := httptest.NewServer(http.HandlerFunc(rl.handle))
	defer srv.Close()

	body := `{"model":"attacker/frontier-model","stream":true,"messages":[{"role":"user","content":"hi"}]}`
	req, _ := http.NewRequest(http.MethodPost, srv.URL, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer sandbox-stolen-key")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("relay call: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if got["model"] != "locked/model" {
		t.Fatalf("model not pinned: upstream saw %v", got["model"])
	}
	if got["stream"] != false {
		t.Fatalf("stream not forced off: %v", got["stream"])
	}
	if auth != "Bearer relay-key" {
		t.Fatalf("upstream must see the relay's key, saw %q", auth)
	}
	if got["messages"] == nil {
		t.Fatal("messages must pass through")
	}
	// Thinking is locked: even a request that asked for it gets the relay's mode.
	ctk, ok := got["chat_template_kwargs"].(map[string]any)
	if !ok || ctk["enable_thinking"] != false {
		t.Fatalf("thinking not locked off: %v", got["chat_template_kwargs"])
	}
}

// TestRelayLocksThinkingOverSandboxChoice: a sandbox-supplied
// chat_template_kwargs cannot re-enable thinking.
func TestRelayLocksThinkingOverSandboxChoice(t *testing.T) {
	var got map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[]}`))
	}))
	defer upstream.Close()
	rl := &relay{upstream: upstream.URL, apiKey: "k", model: "m", thinking: false, client: &http.Client{Timeout: 5 * time.Second}}
	srv := httptest.NewServer(http.HandlerFunc(rl.handle))
	defer srv.Close()

	body := `{"model":"m","chat_template_kwargs":{"enable_thinking":true,"custom":"kept"},"messages":[]}`
	if _, err := http.Post(srv.URL, "application/json", strings.NewReader(body)); err != nil {
		t.Fatalf("post: %v", err)
	}
	ctk := got["chat_template_kwargs"].(map[string]any)
	if ctk["enable_thinking"] != false {
		t.Fatalf("sandbox re-enabled thinking: %v", ctk)
	}
	if ctk["custom"] != "kept" {
		t.Fatalf("unrelated kwargs must pass through: %v", ctk)
	}
}

func TestRelayRejectsNonJSON(t *testing.T) {
	rl := &relay{upstream: "http://unreachable.invalid", apiKey: "k", model: "m", client: http.DefaultClient}
	srv := httptest.NewServer(http.HandlerFunc(rl.handle))
	defer srv.Close()
	resp, err := http.Post(srv.URL, "application/json", strings.NewReader("not json"))
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("want 400 for non-JSON, got %d", resp.StatusCode)
	}
}

func TestRelayMakesMissingOrInvalidProviderUsageExplicit(t *testing.T) {
	for _, body := range []string{
		`{"choices":[{"message":{"content":"ok"}}]}`,
		`{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":-1,"completion_tokens":2,"total_tokens":1}}`,
		`{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":11,"completion_tokens":2,"total_tokens":99}}`,
	} {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(body)) }))
		r := &relay{upstream: upstream.URL, apiKey: "k", model: "m", client: http.DefaultClient}
		srv := httptest.NewServer(http.HandlerFunc(r.handle))
		resp, err := http.Post(srv.URL, "application/json", strings.NewReader(`{"messages":[]}`))
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if r.successes.Load() != 1 || r.usageAvailable.Load() != 0 || r.usageUnavailable.Load() != 1 {
			t.Fatalf("invalid usage was not fail-neutral: successes=%d available=%d unavailable=%d", r.successes.Load(), r.usageAvailable.Load(), r.usageUnavailable.Load())
		}
		srv.Close()
		upstream.Close()
	}
}

// TestProviderProfilesAreFrozen: both certified profiles pin the exact scored
// backend. Chutes serves the TEE model id; OpenRouter serves the canonical
// slug with routing locked to the certified Nebius deployment and fallbacks
// disabled, so OpenRouter cannot silently reroute to an uncertified host.
func TestProviderProfilesAreFrozen(t *testing.T) {
	chutes, ok := providers["chutes"]
	if !ok || chutes.name != "chutes" || chutes.revision == "" || chutes.model != "Qwen/Qwen3-32B-TEE" || chutes.pinBody != nil {
		t.Fatalf("chutes profile drifted: %+v", chutes)
	}
	or, ok := providers["openrouter"]
	if !ok || or.name != "openrouter" || or.revision == "" || or.model != "qwen/qwen3-32b" {
		t.Fatalf("openrouter profile drifted: %+v", or)
	}
	body := map[string]any{"provider": map[string]any{"only": []string{"attacker-host"}, "allow_fallbacks": true}}
	or.pinBody(body)
	pin, _ := body["provider"].(map[string]any)
	only, _ := pin["only"].([]string)
	if len(only) != 1 || only[0] != "nebius" || pin["allow_fallbacks"] != false ||
		pin["data_collection"] != "deny" || pin["zdr"] != true {
		t.Fatalf("openrouter routing/privacy not locked: %v", body["provider"])
	}
}

// TestRelayOpenRouterPinsServingProvider: end to end through handle, a sandbox
// request that names its own provider routing reaches the upstream with the
// locked model slug and the nebius-only routing pin.
func TestRelayOpenRouterPinsServingProvider(t *testing.T) {
	var got map[string]any
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[]}`))
	}))
	defer upstream.Close()
	profile := providers["openrouter"]
	rl := &relay{upstream: upstream.URL, apiKey: "k", model: profile.model, thinking: false, pinBody: profile.pinBody, client: &http.Client{Timeout: 5 * time.Second}}
	srv := httptest.NewServer(http.HandlerFunc(rl.handle))
	defer srv.Close()

	body := `{"model":"other/model","provider":{"only":["cheapest-host"],"allow_fallbacks":true},"messages":[]}`
	if _, err := http.Post(srv.URL, "application/json", strings.NewReader(body)); err != nil {
		t.Fatalf("post: %v", err)
	}
	if got["model"] != "qwen/qwen3-32b" {
		t.Fatalf("model not pinned to canonical slug: %v", got["model"])
	}
	pin, _ := got["provider"].(map[string]any)
	only, _ := pin["only"].([]any)
	if len(only) != 1 || only[0] != "nebius" || pin["allow_fallbacks"] != false ||
		pin["data_collection"] != "deny" || pin["zdr"] != true {
		t.Fatalf("serving provider/privacy not locked: %v", got["provider"])
	}
}

// TestRelayOpenRouterAppendsNoThinkLast: the openrouter profile must place the
// Qwen3 /no_think soft switch AFTER everything the sandbox wrote, because the
// latest soft directive wins — a sandbox-supplied "/think" must not be able to
// re-enable reasoning on a provider that ignores the hard template switch.
func TestRelayOpenRouterAppendsNoThinkLast(t *testing.T) {
	profile := providers["openrouter"]

	body := map[string]any{"messages": []any{
		map[string]any{"role": "system", "content": "You are Ditto."},
		map[string]any{"role": "user", "content": "answer this /think"},
	}}
	profile.pinBody(body)
	msgs := body["messages"].([]any)
	lastContent := msgs[len(msgs)-1].(map[string]any)["content"].(string)
	if !strings.HasSuffix(lastContent, "/no_think") {
		t.Fatalf("last message must end with /no_think, got %q", lastContent)
	}
	if !strings.Contains(lastContent, "/think") {
		t.Fatalf("sandbox content must be preserved, got %q", lastContent)
	}

	// Empty conversation still gets the switch.
	empty := map[string]any{}
	profile.pinBody(empty)
	msgs = empty["messages"].([]any)
	if len(msgs) != 1 || msgs[0].(map[string]any)["content"] != "/no_think" {
		t.Fatalf("empty conversation must gain a /no_think message: %v", empty["messages"])
	}

	// The chutes profile never rewrites messages (the hard template switch
	// makes soft directives inert there).
	chutesBody := map[string]any{"messages": []any{map[string]any{"role": "user", "content": "hi"}}}
	if providers["chutes"].pinBody != nil {
		providers["chutes"].pinBody(chutesBody)
	}
	if got := chutesBody["messages"].([]any)[0].(map[string]any)["content"]; got != "hi" {
		t.Fatalf("chutes profile must not rewrite messages, got %q", got)
	}
}
