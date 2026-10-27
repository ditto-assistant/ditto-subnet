package inference

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

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

func TestRequiredDittoAccountRefusesUnlinkedBeforeReservation(t *testing.T) {
	for _, state := range []string{"missing", "revoked", "other-miner"} {
		t.Run(state, func(t *testing.T) {
			var calls atomic.Int32
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.WriteHeader(http.StatusInternalServerError)
			}))
			defer upstream.Close()
			f := newPGFixture(t, chatTestConfig(t, upstream.URL))
			f.seedRoute(t)
			_, router := dittoRouterTestConfig(upstream.URL)
			router.RequireCompetitionAccount = true
			f.deps.Cfg.DittoRouter = router
			if state == "revoked" {
				testutil.SeedSQL(t, f.pool, `INSERT INTO miner_ditto_links
					(miner_hotkey, ditto_user_id, linked_via, revoked_at)
					VALUES ('miner-hotkey', 'ditto-user-9', 'cli', now())`)
			}
			if state == "other-miner" {
				testutil.SeedSQL(t, f.pool, `INSERT INTO miner_ditto_links
					(miner_hotkey, ditto_user_id, linked_via)
					VALUES ('different-miner', 'ditto-user-9', 'cli')`)
			}
			body := []byte(chatBody)
			headers := f.signedProxyHeaders(1, uuid.New(), body)
			headers["X-Ditto-On-Behalf-Of"] = "forged-funded-account"
			headers["X-Ditto-Evaluation-Stage"] = "forged-stage"
			headers["X-Ditto-Evaluation-Grant"] = "forged-grant"
			headers["X-Ditto-Require-User-Billing"] = "false"
			w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
			if w.Code != http.StatusPaymentRequired {
				t.Fatalf("unlinked account must refuse: %d %s", w.Code, w.Body.String())
			}
			var requests, active, admitted int
			if err := f.pool.QueryRow(t.Context(), `SELECT request_count, active_requests,
				(SELECT count(*) FROM inference_requests WHERE grant_id = $1)
				FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&admitted, &active, &requests); err != nil {
				t.Fatal(err)
			}
			if calls.Load() != 0 || requests != 0 || admitted != 0 || active != 0 {
				t.Fatalf("refusal must reserve and spend nothing: calls=%d rows=%d admitted=%d active=%d", calls.Load(), requests, admitted, active)
			}
		})
	}
}

func TestRequiredDittoAccountRoutesTrustedIdentityAndNeverFallsBack(t *testing.T) {
	for _, routerStatus := range []int{http.StatusOK, http.StatusPaymentRequired} {
		t.Run(http.StatusText(routerStatus), func(t *testing.T) {
			var routerCalls, sponsorCalls atomic.Int32
			seenHeaders := make(chan http.Header, 1)
			sponsor := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				sponsorCalls.Add(1)
				w.WriteHeader(http.StatusInternalServerError)
			}))
			defer sponsor.Close()
			routerServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				routerCalls.Add(1)
				seenHeaders <- r.Header.Clone()
				if r.Header.Get("X-Ditto-On-Behalf-Of") != "ditto-user-9" || r.Header.Get("Authorization") != "Bearer dk_test" {
					t.Errorf("unexpected trusted identity headers: %+v", r.Header)
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(routerStatus)
				if routerStatus == http.StatusPaymentRequired {
					_, _ = w.Write([]byte(`{"error":{"type":"app_spend_not_authorized"}}`))
				} else {
					_, _ = w.Write([]byte(`{"id":"chatcmpl-1","model":"openai/gpt-oss-20b","choices":[{"message":{"role":"assistant","content":"hi"}}],"usage":{"prompt_tokens":12,"completion_tokens":3,"cost":0.0004}}`))
				}
			}))
			defer routerServer.Close()
			f := newPGFixture(t, chatTestConfig(t, sponsor.URL))
			f.seedRoute(t)
			_, router := dittoRouterTestConfig(routerServer.URL)
			router.RequireCompetitionAccount = true
			f.deps.Cfg.DittoRouter = router
			testutil.SeedSQL(t, f.pool, `INSERT INTO miner_ditto_links
				(miner_hotkey, ditto_user_id, linked_via)
				VALUES ('miner-hotkey', 'ditto-user-9', 'cli')`)
			body := []byte(chatBody)
			nonce := uuid.New()
			headers := f.signedProxyHeaders(1, nonce, body)
			headers["X-Ditto-On-Behalf-Of"] = "forged-funded-account"
			headers["X-Ditto-Evaluation-Stage"] = "forged-stage"
			headers["X-Ditto-Evaluation-Grant"] = "forged-grant"
			w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
			if w.Code != routerStatus {
				t.Fatalf("status: want %d, got %d %s", routerStatus, w.Code, w.Body.String())
			}
			if routerCalls.Load() != 1 || sponsorCalls.Load() != 0 {
				t.Fatalf("must call only Router once: router=%d sponsor=%d", routerCalls.Load(), sponsorCalls.Load())
			}
			gotHeaders := <-seenHeaders
			if gotHeaders.Get("X-Ditto-Require-User-Billing") != "true" {
				t.Fatalf("required billing mode must ignore inbound policy overrides: %+v", gotHeaders)
			}
			if gotHeaders.Get("X-Ditto-Evaluation-Grant") != f.grantID.String() || gotHeaders.Get("X-Ditto-Evaluation-Request") != nonce.String() || gotHeaders.Get("X-Ditto-Evaluation-Agent") != f.agentID.String() || gotHeaders.Get("X-Ditto-Evaluation-Stage") != "competition-chat" {
				t.Fatalf("attribution must come from the verified grant and proof: %+v", gotHeaders)
			}
			var requestState string
			var cost int64
			var active int
			if err := f.pool.QueryRow(t.Context(), `SELECT r.status, r.cost_microusd,
				g.active_requests FROM inference_requests r
				JOIN inference_grants g ON g.grant_id = r.grant_id
				WHERE r.grant_id = $1 AND r.nonce = $2`, f.grantID, nonce).Scan(&requestState, &cost, &active); err != nil {
				t.Fatal(err)
			}
			if active != 0 {
				t.Fatalf("reservation must settle: active=%d", active)
			}
			if routerStatus == http.StatusOK && (requestState != "completed" || cost != 400) {
				t.Fatalf("successful Router receipt must settle cost: %s cost=%d", requestState, cost)
			}
			if routerStatus == http.StatusPaymentRequired && (requestState != "failed" || cost != 0) {
				t.Fatalf("refused Router charge must settle zero: %s cost=%d", requestState, cost)
			}
		})
	}
}
