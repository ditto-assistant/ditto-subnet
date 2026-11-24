package inference

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

func TestDittoRouterOnBehalfOfFollowsTheActiveLinkOnly(t *testing.T) {
	f := newPGFixture(t, chatTestConfig(t, "https://openrouter.ai/api/v1/chat/completions"))
	f.deps.Cfg.DittoRouter = config.DittoRouterConfig{Enabled: true, APIKey: "dk", Lanes: []string{config.DittoRouterLaneCompetition}}
	ctx := context.Background()
	agent := pgtype.UUID{Bytes: f.agentID, Valid: true}

	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent); ok || user != "" {
		t.Fatalf("no link yet: %q %v", user, ok)
	}
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO miner_ditto_links (miner_hotkey, ditto_user_id, ditto_email, linked_via)
		 VALUES ('miner-hotkey', 'ditto-user-9', 'm@example.com', 'cli')`)
	user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent)
	if !ok || user != "ditto-user-9" {
		t.Fatalf("active link: %q %v", user, ok)
	}
	// Another miner's agent never inherits the link.
	other := uuid.New()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256) VALUES ($1, 'other-hotkey', 'o', repeat('b', 64))`, other)
	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, pgtype.UUID{Bytes: other, Valid: true}); ok || user != "" {
		t.Fatalf("other miner must not be attributed: %q %v", user, ok)
	}
	testutil.SeedSQL(t, f.pool, `UPDATE miner_ditto_links SET revoked_at = now() WHERE miner_hotkey = 'miner-hotkey'`)
	if user, ok := f.deps.dittoRouterOnBehalfOf(ctx, agent); ok || user != "" {
		t.Fatalf("revoked link must not route: %q %v", user, ok)
	}
}

// dittoRouterChatFixture enables the competition Router lane, links the
// fixture agent's miner to a consenting Ditto account and points the Router at
// the given upstream. The OpenRouter upstream fails the test if it is called:
// a linked agent's call must never reach it.
func dittoRouterChatFixture(t *testing.T, router http.HandlerFunc) *pgFixture {
	t.Helper()
	openrouter := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Errorf("a linked agent's call reached OpenRouter")
		w.WriteHeader(http.StatusInternalServerError)
	}))
	t.Cleanup(openrouter.Close)
	upstream := httptest.NewServer(router)
	t.Cleanup(upstream.Close)
	f := newPGFixture(t, chatTestConfig(t, openrouter.URL))
	f.deps.Cfg.DittoRouter = config.DittoRouterConfig{Enabled: true, URL: upstream.URL, APIKey: "dk",
		OnBehalfOfHeader: "X-Ditto-On-Behalf-Of", Lanes: []string{config.DittoRouterLaneCompetition}}
	f.seedRoute(t)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO miner_ditto_links (miner_hotkey, ditto_user_id, ditto_email, linked_via)
		 VALUES ('miner-hotkey', 'ditto-user-9', 'm@example.com', 'cli')`)
	return f
}

func (f *pgFixture) seedOpenOpenRouterCircuit(t *testing.T) {
	t.Helper()
	now := time.Now().UTC()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO provider_outage_circuits
		 (provider, state, epoch, opened_at, retry_at, last_failure_at,
		  failure_count, last_status, last_error_code)
		 VALUES ('openrouter', 'open', $1, $2, $3, $2, 1, 429, 'chat_backpressure')`,
		uuid.New(), now, now.Add(2*time.Minute))
}

// openRouterRouteState reads the grant's shared OpenRouter route.
func (f *pgFixture) openRouterRouteState(t *testing.T) (samples int64, status string, cooling bool) {
	t.Helper()
	if err := f.pool.QueryRow(t.Context(),
		`SELECT sample_count, status, cooldown_until IS NOT NULL FROM inference_provider_routes
		 WHERE provider = 'openrouter' AND profile_revision = $1`, testProfile).
		Scan(&samples, &status, &cooling); err != nil {
		t.Fatalf("read route: %v", err)
	}
	return samples, status, cooling
}

func TestDittoRouterFailuresLeaveTheSharedOpenRouterRouteAlone(t *testing.T) {
	for name, testCase := range map[string]struct {
		status   int
		terminal string
	}{
		"one user's 402 consent refusal": {http.StatusPaymentRequired, "ditto_router_payment_required"},
		"a Router 500":                   {http.StatusInternalServerError, "upstream_http_500"},
	} {
		t.Run(name, func(t *testing.T) {
			f := dittoRouterChatFixture(t, func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(testCase.status)
				_, _ = w.Write([]byte(`{"error":{"type":"app_spend_not_authorized","message":"no"}}`))
			})
			nonce := uuid.New()
			body := []byte(chatBody)
			w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
			if w.Code != http.StatusBadGateway {
				t.Fatalf("status=%d: %s", w.Code, w.Body.String())
			}
			var terminal string
			if err := f.pool.QueryRow(t.Context(),
				`SELECT terminal_error_code FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
				f.grantID, nonce).Scan(&terminal); err != nil {
				t.Fatalf("read request: %v", err)
			}
			if terminal != testCase.terminal {
				t.Fatalf("terminal=%s want %s", terminal, testCase.terminal)
			}
			// No OpenRouter call happened, so the shared route (and with it new
			// grant minting in aggregate_throughput) must not cool down.
			if samples, status, cooling := f.openRouterRouteState(t); samples != 0 || status != "healthy" || cooling {
				t.Fatalf("Router failure observed the OpenRouter route: samples=%d status=%s cooling=%v", samples, status, cooling)
			}
			var circuits int
			if err := f.pool.QueryRow(t.Context(),
				`SELECT count(*) FROM provider_outage_circuits WHERE provider = 'openrouter'`).Scan(&circuits); err != nil {
				t.Fatalf("read circuit: %v", err)
			}
			if circuits != 0 {
				t.Fatalf("Router failure touched the openrouter circuit: count=%d", circuits)
			}
		})
	}
}

func TestDittoRouterSuccessKeepsAnOpenRouterOutageOpenAndIsSanitized(t *testing.T) {
	f := dittoRouterChatFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(dittoRouterCompletion(pgTestModel)))
	})
	f.seedOpenOpenRouterCircuit(t)
	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d: %s", w.Code, w.Body.String())
	}
	want := `{"id":"chatcmpl-1","object":"chat.completion","created":1755000000,"model":"` + pgTestModel + `",` +
		`"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"hi"},"logprobs":null}],` +
		`"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}`
	if w.Body.String() != want {
		t.Fatalf("Router body must be rebuilt, not passed through:\n got %s\nwant %s", w.Body.String(), want)
	}
	var state string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT state FROM provider_outage_circuits WHERE provider = 'openrouter'`).Scan(&state); err != nil {
		t.Fatalf("read circuit: %v", err)
	}
	if state != "open" {
		t.Fatalf("a Router success healed an OpenRouter outage: state=%s", state)
	}
	if samples, status, _ := f.openRouterRouteState(t); samples != 0 || status != "healthy" {
		t.Fatalf("Router success observed the OpenRouter route: samples=%d status=%s", samples, status)
	}
}

func TestDittoRouterReplyFromAnotherModelIsRefused(t *testing.T) {
	f := dittoRouterChatFixture(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(dittoRouterCompletion("some/other-model")))
	})
	nonce := uuid.New()
	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
	if w.Code != http.StatusBadGateway {
		t.Fatalf("status=%d: %s", w.Code, w.Body.String())
	}
	var terminal string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT terminal_error_code FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&terminal); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if terminal != "provider_identity_mismatch" {
		t.Fatalf("terminal=%s", terminal)
	}
}
