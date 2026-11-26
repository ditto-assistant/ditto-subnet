package inference

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/testutil"
	"github.com/google/uuid"
)

type daniTestTransport func(*http.Request) (*http.Response, error)

func (f daniTestTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestDaniLabsBoardRoutesAndSettlesCanary(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	cfg := chatTestConfig(t, upstream.URL)
	cfg.Inference.DaniLabsAPIKey = "dani-test-key"
	cfg.Inference.DaniLabsTrialUntil = time.Now().Add(time.Hour).Format(time.RFC3339)
	cfg.Inference.DaniLabsTrialScope = "canary"
	f := newPGFixture(t, cfg)
	f.seedRoute(t)
	var daniCalls int
	f.deps.Upstream.Transport = daniTestTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Host != "api.danilabs.ai" {
			return http.DefaultTransport.RoundTrip(r)
		}
		daniCalls++
		if r.Header.Get("Authorization") != "Bearer dani-test-key" {
			t.Error("wrong DaniLabs credential")
		}
		body := `{"id":"dani-1","object":"chat.completion","created":1,"model":"openai/gpt-oss-20b","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"hello"}}],"usage":{"prompt_tokens":12,"completion_tokens":3}}`
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body)), Request: r}, nil
	})
	testutil.SeedSQL(t, f.pool, `INSERT INTO inference_concurrency_settings_revisions
		(parent_revision, scope, settings, checksum, reason, actor)
		VALUES (0, '*', '{"danilabs_percent":100}'::jsonb, repeat('a', 64), 'test canary', 'test')`)
	if err := f.deps.Settings.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	send := func() uuid.UUID {
		t.Helper()
		nonce := uuid.New()
		body := []byte(chatBody)
		w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
		if w.Code != 200 {
			t.Fatalf("status %d: %s", w.Code, w.Body.String())
		}
		return nonce
	}
	send()
	if daniCalls != 0 {
		t.Fatal("non-canary was routed to DaniLabs")
	}
	testutil.SeedSQL(t, f.pool, `UPDATE validator_tickets SET purpose = 'benchmark_canary' WHERE agent_id = $1`, f.agentID)
	nonce := send()
	if daniCalls != 1 {
		t.Fatalf("DaniLabs calls=%d", daniCalls)
	}
	var provider string
	var prompt, completion, cost int64
	if err := f.pool.QueryRow(context.Background(), `SELECT upstream_provider, prompt_tokens, completion_tokens, cost_microusd FROM inference_requests WHERE grant_id=$1 AND nonce=$2`, f.grantID, nonce).Scan(&provider, &prompt, &completion, &cost); err != nil {
		t.Fatal(err)
	}
	if provider != daniLabsRoute || prompt != 12 || completion != 3 || cost != 0 {
		t.Fatalf("provider=%q prompt=%d completion=%d cost=%d", provider, prompt, completion, cost)
	}
}
