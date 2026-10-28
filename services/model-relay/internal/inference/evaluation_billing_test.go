package inference

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/testutil"
	"github.com/google/uuid"
)

func TestFullEvaluationChatDispatchesFrozenProviderOnceAndNeverRouterInference(t *testing.T) {
	for _, mode := range []string{"success", "unfunded", "uncertain"} {
		t.Run(mode, func(t *testing.T) {
			var providerCalls, routerCalls, reservations, settlements atomic.Int32
			goodProvider := fakeChatUpstream(t, nil)
			defer goodProvider.Close()
			provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				providerCalls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				if mode == "uncertain" {
					w.WriteHeader(502)
					_, _ = w.Write([]byte(`{"error":{"message":"unknown provider outcome"}}`))
					return
				}
				goodProvider.Config.Handler.ServeHTTP(w, r)
			}))
			defer provider.Close()
			billing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("X-Ditto-On-Behalf-Of") != "trusted-payer" {
					t.Error("forged payer reached accounting")
				}
				w.Header().Set("Content-Type", "application/json")
				if strings.HasSuffix(r.URL.Path, "/reservations") {
					reservations.Add(1)
					if mode == "unfunded" {
						w.WriteHeader(402)
					}
				} else if strings.HasSuffix(r.URL.Path, "/settle") {
					settlements.Add(1)
					var body map[string]any
					_ = json.NewDecoder(r.Body).Decode(&body)
					want := "settled"
					if mode == "uncertain" {
						want = "unknown"
					}
					if body["state"] != want {
						t.Errorf("provider result %s reported as %+v", mode, body)
					}
				} else {
					routerCalls.Add(1)
					t.Errorf("unexpected Router inference dispatch %s", r.URL.Path)
				}
				_, _ = w.Write([]byte(`{}`))
			}))
			defer billing.Close()
			f := newPGFixture(t, chatTestConfig(t, provider.URL))
			f.seedRoute(t)
			f.deps.Cfg.DittoRouter = config.DittoRouterConfig{Enabled: true, URL: billing.URL + "/chat/completions", Lanes: []string{config.DittoRouterLaneCompetition}, APIKey: "isolated-service", EvaluationBillingEnabled: true, EvaluationBillingURL: billing.URL, EvaluationRequestMicrousd: 1000000}
			run := uuid.New()
			testutil.SeedSQL(t, f.pool, `INSERT INTO miner_ditto_links(miner_hotkey,ditto_user_id,linked_via) VALUES('miner-hotkey','trusted-payer','cli')`)
			testutil.SeedSQL(t, f.pool, `INSERT INTO paid_evaluation_plans(run_id,agent_id,bench_version,phase,cycle,payer_uid,state,estimate_microusd,cap_microusd,expires_at,refreshed_at) VALUES($1,$2,9,'competition',1,'trusted-payer','approved',1000000,5000000,now()+interval '1 hour',now())`, run, f.agentID)
			testutil.SeedSQL(t, f.pool, `INSERT INTO paid_evaluation_grant_bindings(grant_id,run_id) VALUES($1,$2)`, f.grantID, run)
			body := []byte(chatBody)
			headers := f.signedProxyHeaders(1, uuid.New(), body)
			headers["X-Ditto-On-Behalf-Of"] = "forged-funded-user"
			rr := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
			want := 200
			if mode == "unfunded" {
				want = 402
			}
			if mode == "uncertain" {
				want = 502
			}
			if rr.Code != want {
				t.Fatalf("%s got %d want %d: %s", mode, rr.Code, want, rr.Body.String())
			}
			var active int
			if err := f.pool.QueryRow(t.Context(), `SELECT active_requests FROM inference_grants WHERE grant_id=$1`, f.grantID).Scan(&active); err != nil {
				t.Fatal(err)
			}
			p, s := int32(1), int32(1)
			if mode == "unfunded" {
				p, s = 0, 0
			}
			if providerCalls.Load() != p || settlements.Load() != s || reservations.Load() != 1 || routerCalls.Load() != 0 || active != 0 {
				t.Fatalf("provider=%d settle=%d reserve=%d router=%d active=%d", providerCalls.Load(), settlements.Load(), reservations.Load(), routerCalls.Load(), active)
			}
		})
	}
}

func TestEvaluationBillingDoesNotFollowRedirectOrDispatchInference(t *testing.T) {
	leaked := 0
	foreign := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { leaked++; w.WriteHeader(200) }))
	defer foreign.Close()
	calls := 0
	service := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("X-Ditto-On-Behalf-Of") != "trusted-user" || r.Header.Get("X-Ditto-Require-User-Billing") != "true" {
			t.Error("trusted identity missing")
		}
		http.Redirect(w, r, foreign.URL, http.StatusTemporaryRedirect)
	}))
	defer service.Close()
	cfg := config.DittoRouterConfig{EvaluationBillingURL: service.URL, APIKey: "test-service-key"}
	if e := evaluationPost(t.Context(), service.Client(), cfg, "trusted-user", "/reservations", map[string]any{"id": "server-nonce"}, nil); e == nil {
		t.Fatal("redirect accepted")
	}
	if leaked != 0 || calls != 1 {
		t.Fatalf("credential redirect calls%d leaked%d", calls, leaked)
	}
}

func TestEvaluationUnknownOutcomeRetainsHoldAndSuccessCarriesActualReceipt(t *testing.T) {
	var bodies []map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		bodies = append(bodies, body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	bill := &evaluationBill{cfg: config.DittoRouterConfig{EvaluationBillingURL: server.URL, APIKey: "test"}, client: server.Client(), uid: "payer", id: "nonce", provider: "Locked"}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if err := bill.finish(ctx, &settleOutcome{status: "failed"}, nil); err != nil {
		t.Fatal(err)
	}
	if bodies[0]["state"] != "unknown" || len(bodies[0]) != 1 {
		t.Fatalf("uncertain cost guessed %+v", bodies[0])
	}
	out := &settleOutcome{status: "completed", usageAvailable: true, promptTokens: 12, completionTokens: 3, costMicrousd: 25, upstreamProvider: textValue("ActualProvider")}
	if err := bill.finish(ctx, out, []byte(`{"id":"provider-receipt","usage":{"prompt_tokens":12,"completion_tokens":3}}`)); err != nil {
		t.Fatal(err)
	}
	if bodies[1]["state"] != "settled" || bodies[1]["actualMicrousd"] != float64(25) || bodies[1]["provider"] != "ActualProvider" {
		t.Fatalf("receipt missing %+v", bodies[1])
	}
}
