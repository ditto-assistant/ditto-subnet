// Real-Postgres tests for the dark decision lane (POST
// /api/v1/inference/decisions) against a stub OpenRouter System One server.
// Never the real API. Skips when the monorepo test Postgres is unavailable.
package inference

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"

	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const (
	decisionPin = "typesafe/jev-1.14"
	// A harness may ask anything the provider supports: every question type,
	// structured state, and its own model string (overridden by the pin).
	decisionBody = `{"model":"~typesafe/jev-latest","state":{"ticket":"checkout is blank"},"questions":{` +
		`"team":{"type":"choice","instructions":"Which team?","criteria":{"payments":"billing","frontend":null}},` +
		`"urgency":{"type":"score","instructions":"How urgent?","criteria":["later","this week","now"]},` +
		`"is_bug":{"type":"noul","instructions":"Is this a defect?"},` +
		`"where":{"type":"box","instructions":"Where is the error?"}}}`
)

// decisionUpstream is a stub System One server. served is the model it
// claims to have run; status != 200 answers that status instead.
func decisionUpstream(t *testing.T, served string, status int, captured *map[string]any, calls *atomic.Int32) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if got := r.Header.Get("Authorization"); got != "Bearer test-openrouter-key" {
			t.Errorf("upstream must use the existing OpenRouter key, got %q", got)
		}
		if r.Header.Get("X-Ditto-Proof") != "" || r.Header.Get("X-Ditto-Grant") != "" {
			t.Errorf("inbound client headers must never be forwarded upstream")
		}
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if captured != nil {
			*captured = payload
		}
		if status != http.StatusOK {
			w.Header().Set("Retry-After", "2")
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":{"code":503,"message":"Service temporarily unavailable"}}`))
			return
		}
		_, _ = w.Write([]byte(`{"id":"gen-dec-1","model":"` + served + `","provider":"TypeSafe",` +
			`"answers":{"team":{"type":"choice","choice":"payments","confidence":0.75,` +
			`"probabilities":{"payments":0.84,"frontend":0.16}},"is_bug":{"type":"noul","noul":0.96}},` +
			`"usage":{"cost":0.00002,"input_tokens":476,"output_tokens":70}}`))
	}))
	t.Cleanup(server.Close)
	return server
}

// decisionFixture is a live v9 grant plus, when lane is true, the operator
// board turned on and the grant stamped as the mint would stamp it.
func decisionFixture(t *testing.T, upstreamURL string, lane bool) *pgFixture {
	t.Helper()
	cfg := testConfig(t, map[string]string{"DITTO_INFERENCE_TIMEOUT_SECONDS": "5"})
	cfg.Inference.DecisionUpstreamURL = upstreamURL
	f := newPGFixture(t, cfg)
	if lane {
		testutil.SeedSQL(t, f.pool,
			`INSERT INTO inference_concurrency_settings_revisions
			    (parent_revision, scope, settings, checksum, reason, actor)
			 VALUES (0, '*', '{"decision_lane":{"enabled":true,"model":"typesafe/jev-1.14"}}'::jsonb,
			         $1, 'open the dark decision lane', 'test-operator')`,
			strings.Repeat("cd", 32))
		testutil.SeedSQL(t, f.pool,
			`UPDATE inference_grants SET decision_model = $2, decision_input_microusd_per_mtok = 42000,
			     decision_request_budget = 100, decision_token_budget = 1000000
			 WHERE grant_id = $1`, f.grantID, decisionPin)
	}
	if err := f.deps.Settings.Refresh(t.Context()); err != nil {
		t.Fatalf("refresh settings: %v", err)
	}
	return f
}

func postDecision(f *pgFixture, nonce uuid.UUID) *httptest.ResponseRecorder {
	body := []byte(decisionBody)
	return serve(f.deps, proxyRequest("/api/v1/inference/decisions", decisionBody, f.signedProxyHeaders(1, nonce, body)))
}

func TestDecisionLaneOffIsAnUnmountedPath(t *testing.T) {
	var calls atomic.Int32
	upstream := decisionUpstream(t, decisionPin, http.StatusOK, nil, &calls)
	f := decisionFixture(t, upstream.URL, false)

	w := postDecision(f, uuid.New())
	expectEnvelope(t, w, http.StatusNotFound, relayhttp.CodeHTTPException, "Not Found")
	var rows int
	if err := f.pool.QueryRow(t.Context(), `SELECT count(*) FROM inference_requests`).Scan(&rows); err != nil {
		t.Fatalf("count requests: %v", err)
	}
	if calls.Load() != 0 || rows != 0 {
		t.Fatalf("a dark lane must touch neither the provider nor the ledger: calls=%d rows=%d", calls.Load(), rows)
	}

	// Board on, but this grant was minted with the lane off: no pin, 4107.
	f2 := decisionFixture(t, upstream.URL, true)
	testutil.SeedSQL(t, f2.pool,
		`UPDATE inference_grants SET decision_model = NULL, decision_request_budget = 0,
		     decision_token_budget = 0 WHERE grant_id = $1`, f2.grantID)
	w = postDecision(f2, uuid.New())
	expectEnvelope(t, w, http.StatusTooManyRequests, relayhttp.CodeDeclineModelNotPermitted, "decision grant does not permit this model")
}

func TestDecisionFullFlowForcesThePinAndBooksLedgerCost(t *testing.T) {
	var captured map[string]any
	var calls atomic.Int32
	upstream := decisionUpstream(t, decisionPin+"-20261020", http.StatusOK, &captured, &calls)
	f := decisionFixture(t, upstream.URL, true)

	nonce := uuid.New()
	w := postDecision(f, nonce)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	if captured["model"] != decisionPin {
		t.Fatalf("upstream model must be forced to the pin, got %v", captured["model"])
	}
	questions, _ := captured["questions"].(map[string]any)
	if len(questions) != 4 || questions["where"] == nil || captured["state"] == nil {
		t.Fatalf("questions and state must pass through untouched: %v", captured)
	}
	var resp struct {
		Model   string                    `json:"model"`
		Answers map[string]map[string]any `json:"answers"`
		Usage   map[string]int64          `json:"usage"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Model != decisionPin+"-20261020" || resp.Answers["team"]["choice"] != "payments" ||
		resp.Usage["input_tokens"] != 476 || strings.Contains(w.Body.String(), "gen-dec-1") {
		t.Fatalf("public decision response: %s", w.Body.String())
	}

	var kind, status string
	var prompt, cost int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT request_kind, status, prompt_tokens, cost_microusd FROM inference_requests
		 WHERE grant_id = $1 AND nonce = $2`, f.grantID, nonce).Scan(&kind, &status, &prompt, &cost); err != nil {
		t.Fatalf("read request: %v", err)
	}
	// 476 input tokens x $0.042/M = 19.992 micro-USD -> 20. Output is free.
	if kind != "decision" || status != "completed" || prompt != 476 || cost != 20 {
		t.Fatalf("decision settle: %s %s %d %d", kind, status, prompt, cost)
	}
	var count, active int32
	var tokens, decisionCost, chatPrompt int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT decision_request_count, decision_active_requests, decision_tokens, decision_cost_microusd, prompt_tokens
		 FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&count, &active, &tokens, &decisionCost, &chatPrompt); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if count != 1 || active != 0 || tokens != 476 || decisionCost != 20 || chatPrompt != 0 {
		t.Fatalf("grant decision accounting: %d %d %d %d chat=%d", count, active, tokens, decisionCost, chatPrompt)
	}
}

func TestDecisionInfraFailuresAreUnchargedAndNoFault(t *testing.T) {
	for _, tc := range []struct {
		name    string
		served  string
		status  int
		want    int
		code    string
		message string
	}{
		{"served model mismatch", "typesafe/jev-1.13", http.StatusOK, 502, "decision_model_mismatch", "decision provider served an unpinned model"},
		{"provider outage", decisionPin, http.StatusServiceUnavailable, 503, "decision_provider_backpressure_503", "decision provider is temporarily at capacity"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var calls atomic.Int32
			upstream := decisionUpstream(t, tc.served, tc.status, nil, &calls)
			f := decisionFixture(t, upstream.URL, true)
			nonce := uuid.New()
			w := postDecision(f, nonce)
			// The same 502/503 shapes as the chat and embedding lanes, which
			// the scorer maps to model_relay_unavailable. No new code.
			expectEnvelope(t, w, tc.want, relayhttp.CodeHTTPException, tc.message)
			var status, code string
			var prompt, cost int64
			if err := f.pool.QueryRow(t.Context(),
				`SELECT status, terminal_error_code, prompt_tokens, cost_microusd FROM inference_requests
				 WHERE grant_id = $1 AND nonce = $2`, f.grantID, nonce).Scan(&status, &code, &prompt, &cost); err != nil {
				t.Fatalf("read request: %v", err)
			}
			if status != "failed" || code != tc.code || prompt != 0 || cost != 0 {
				t.Fatalf("infra failure must settle uncharged: %s %s %d %d", status, code, prompt, cost)
			}
		})
	}
}

func TestDecisionTokenWallIsItsOwnAndSparesChat(t *testing.T) {
	var calls atomic.Int32
	upstream := decisionUpstream(t, decisionPin, http.StatusOK, nil, &calls)
	f := decisionFixture(t, upstream.URL, true)
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET decision_tokens = decision_token_budget WHERE grant_id = $1`, f.grantID)

	w := postDecision(f, uuid.New())
	expectEnvelope(t, w, http.StatusTooManyRequests, relayhttp.CodeDeclineTokenBudget, "decision grant has spent its token budget")
	var grantStatus string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&grantStatus); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if calls.Load() != 0 || grantStatus != "active" {
		t.Fatalf("a spent decision wall must not reach the provider or exhaust the grant: calls=%d status=%s",
			calls.Load(), grantStatus)
	}
}
