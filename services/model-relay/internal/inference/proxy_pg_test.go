// Real-Postgres end-to-end tests for the inference plane: the full chat and
// embedding proxy flows against a fake upstream, the exchange rotation, and
// the admission transaction semantics (declines roll back; settlement
// commits). Tests skip (never fail) when the monorepo test Postgres at
// localhost:15433 is unavailable.
package inference

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const (
	pgTestHotkey = "5FTestHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
	pgTestModel  = "openai/gpt-oss-20b"
	testProfile  = "openrouter-route-6a097486af3c178d-v1"
	testBearer   = "test-bearer-token-with-plenty-of-entropy-43"
)

type pgFixture struct {
	deps       *Deps
	pool       *pgxpool.Pool
	agentID    uuid.UUID
	grantID    uuid.UUID
	deadline   time.Time
	brokerPub  ed25519.PublicKey
	brokerPriv ed25519.PrivateKey
}

// newPGFixture provisions a fresh database with one agent + issued ticket +
// active exchanged grant, and returns Deps wired to it.
func newPGFixture(t *testing.T, cfg *config.Config) *pgFixture {
	return newPGFixtureWithHotkey(t, cfg, pgTestHotkey)
}

func newPGFixtureWithHotkey(t *testing.T, cfg *config.Config, hotkey string) *pgFixture {
	t.Helper()
	pool := testutil.NewTestPGPool(t)
	queries := postgres.New(pool)

	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatalf("ed25519: %v", err)
	}
	f := &pgFixture{
		pool:       pool,
		agentID:    uuid.New(),
		grantID:    uuid.New(),
		deadline:   time.Now().UTC().Add(30 * time.Minute).Truncate(time.Microsecond),
		brokerPub:  pub,
		brokerPriv: priv,
	}
	brokerKey := trimBase64Padding(base64.URLEncoding.EncodeToString(pub))

	testutil.SeedSQL(t, pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256)
		 VALUES ($1, 'miner-hotkey', 'test-agent', repeat('a', 64))`, f.agentID)
	testutil.SeedSQL(t, pool,
		`INSERT INTO validator_tickets (agent_id, validator_hotkey, slot_id, status, deadline, bench_version, attempt_count)
		 VALUES ($1, $2, 'slot-0', 'issued', $3, 9, 1)`,
		f.agentID, hotkey, f.deadline)
	testutil.SeedSQL(t, pool,
		`INSERT INTO inference_grants (
		    grant_id, agent_id, bench_version, validator_hotkey, slot_id,
		    ticket_deadline, status, bearer_digest, broker_public_key,
		    generation, allowed_models, route_provider, route_profile,
		    request_budget, token_budget, expires_at, usage_accounting_version)
		 VALUES ($1, $2, 9, $3, 'slot-0', $4, 'active', $5, $6, 1,
		         '["openai/gpt-oss-20b"]'::jsonb, 'openrouter', $7,
		         8192, 25000000, $4, 2)`,
		f.grantID, f.agentID, hotkey, f.deadline,
		bearerDigest(testBearer), brokerKey, testProfile)

	logger := slog.New(slog.NewTextHandler(nullWriter{}, nil))
	f.deps = &Deps{
		Cfg:      cfg,
		Logger:   logger,
		Pool:     pool,
		Queries:  queries,
		Permits:  &stubPermits{permitted: true},
		Upstream: &http.Client{},
		Settings: NewSettingsResolver(queries, logger),
		Sleep:    func(context.Context, time.Duration) {},
	}
	return f
}

func (f *pgFixture) seedRoute(t *testing.T) {
	t.Helper()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_routing_policies (
		    model, revision, enabled, speed_weight, cost_weight, exploration_weight,
		    exploration_ticket_budget, min_tool_accuracy, min_composite,
		    min_calibration_samples, max_error_rate, max_timeout_rate,
		    cooldown_seconds, ewma_alpha)
		 VALUES ($1, 0, true, 1, 1, 0.5, 4, 0.5, 0.5, 60, 0.5, 0.5, 120, 0.3)`,
		pgTestModel)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_provider_routes (
		    model, provider, profile_revision, status, calibration_status, discovered_at)
		 VALUES ($1, 'openrouter', $2, 'healthy', 'shadow', now())`,
		pgTestModel, testProfile)
}

// signedProxyHeaders builds the six auth headers with a valid Ed25519 proof
// over body.
func (f *pgFixture) signedProxyHeaders(generation int64, nonce uuid.UUID, body []byte) map[string]string {
	requestedAt := time.Now().UTC().Truncate(time.Microsecond)
	message := proxyMessage(f.grantID, generation, nonce, requestedAt, body)
	proof := trimBase64Padding(base64.URLEncoding.EncodeToString(ed25519.Sign(f.brokerPriv, message)))
	return map[string]string{
		"X-Ditto-Grant":        f.grantID.String(),
		"X-Ditto-Generation":   "1",
		"X-Ditto-Nonce":        nonce.String(),
		"X-Ditto-Requested-At": isoformatMicro(requestedAt),
		"X-Ditto-Proof":        proof,
		"Authorization":        "Bearer " + testBearer,
	}
}

func chatTestConfig(t *testing.T, upstreamURL string) *config.Config {
	cfg := testConfig(t, map[string]string{
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	// Production config only accepts the reviewed provider credential boundary.
	// Tests replace the destination after validation so an in-process upstream
	// can exercise the real proxy without adding a boot-time bypass.
	cfg.Inference.UpstreamURL = upstreamURL
	return cfg
}

const chatBody = `{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"hello"}],"max_tokens":64}`

// fakeChatUpstream returns a valid OpenRouter completion and records the
// last upstream request for assertions.
func fakeChatUpstream(t *testing.T, captured *map[string]any) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer test-openrouter-key" {
			t.Errorf("upstream Authorization: %q", got)
		}
		if r.Header.Get("X-OpenRouter-Metadata") != "enabled" {
			t.Errorf("metadata header missing")
		}
		if r.Header.Get("X-Ditto-Proof") != "" || r.Header.Get("X-Ditto-Grant") != "" {
			t.Errorf("inbound client headers must never be forwarded upstream")
		}
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if captured != nil {
			*captured = payload
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"id":"gen-123","object":"chat.completion","created":1755000000,
			"model":"openai/gpt-oss-20b","provider":"deepinfra",
			"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
				"message":{"role":"assistant","content":"hi there","refusal":null}}],
			"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0.0021,"total_tokens":17}
		}`))
	}))
}

func TestChatFullFlow(t *testing.T) {
	var captured map[string]any
	upstream := fakeChatUpstream(t, &captured)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	f.seedRoute(t)

	nonce := uuid.New()
	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	if cc := w.Header().Get("Cache-Control"); cc != "no-store" {
		t.Fatalf("Cache-Control: %q", cc)
	}
	// The sanitized body is rebuilt from scratch: exact wire shape.
	want := `{"id":"gen-123","object":"chat.completion","created":1755000000,"model":"openai/gpt-oss-20b",` +
		`"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"hi there"},"logprobs":null}],` +
		`"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}}`
	if w.Body.String() != want {
		t.Fatalf("sanitized body:\n got %s\nwant %s", w.Body.String(), want)
	}
	// The upstream payload was locked.
	if captured["model"] != pgTestModel || captured["stream"] != false {
		t.Fatalf("upstream payload not locked: %v", captured)
	}
	if captured["n"] != float64(1) || captured["max_tokens"] != float64(64) {
		t.Fatalf("upstream n/max_tokens: %v %v", captured["n"], captured["max_tokens"])
	}
	if _, present := captured["provider"]; !present {
		t.Fatalf("provider preferences missing upstream")
	}
	reasoning, _ := captured["reasoning"].(map[string]any)
	if reasoning["effort"] != "medium" || reasoning["exclude"] != true {
		t.Fatalf("reasoning not pinned upstream: %v", captured["reasoning"])
	}

	ctx := t.Context()
	var status string
	var prompt, completion, cost int64
	var provider string
	if err := f.pool.QueryRow(ctx,
		`SELECT status, prompt_tokens, completion_tokens, cost_microusd, upstream_provider
		 FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &prompt, &completion, &cost, &provider); err != nil {
		t.Fatalf("read request row: %v", err)
	}
	if status != "completed" || prompt != 12 || completion != 5 || cost != 2100 || provider != "deepinfra" {
		t.Fatalf("request settle: %s %d/%d cost=%d provider=%s", status, prompt, completion, cost, provider)
	}
	var requestCount, active int
	var grantPrompt, grantCompletion, grantCost int64
	if err := f.pool.QueryRow(ctx,
		`SELECT request_count, active_requests, prompt_tokens, completion_tokens, cost_microusd
		 FROM inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&requestCount, &active, &grantPrompt, &grantCompletion, &grantCost); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if requestCount != 1 || active != 0 || grantPrompt != 12 || grantCompletion != 5 || grantCost != 2100 {
		t.Fatalf("grant accounting: count=%d active=%d %d/%d cost=%d", requestCount, active, grantPrompt, grantCompletion, grantCost)
	}
	var sampleCount int64
	var routeStatus string
	if err := f.pool.QueryRow(ctx,
		`SELECT sample_count, status FROM inference_provider_routes WHERE profile_revision = $1`,
		testProfile).Scan(&sampleCount, &routeStatus); err != nil {
		t.Fatalf("read route: %v", err)
	}
	if sampleCount != 1 || routeStatus != "healthy" {
		t.Fatalf("route observation: samples=%d status=%s", sampleCount, routeStatus)
	}
}

func TestChatDeclineRollsBackAdmissionWrites(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	// Spend the request budget: the admission gate marks the grant
	// exhausted, but the endpoint's decline rolls that write back.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET request_count = request_budget WHERE grant_id = $1`, f.grantID)

	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineBudgetExhausted, "inference grant has spent its request budget")

	var status string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&status); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if status != "active" {
		t.Fatalf("decline must roll back the exhausted write, grant status = %q", status)
	}
	var rows int
	if err := f.pool.QueryRow(t.Context(),
		`SELECT count(*) FROM inference_requests WHERE grant_id = $1`, f.grantID).Scan(&rows); err != nil {
		t.Fatalf("count requests: %v", err)
	}
	if rows != 0 {
		t.Fatalf("no request row may survive a decline, got %d", rows)
	}
}

func TestChatNonceReplayDecline(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	nonce := uuid.New()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at, completed_at)
		 VALUES ($1, $2, 1, 'completed', 'chat', $3, 100, 100, 90, 10, 1, now(), now())`,
		f.grantID, nonce, pgTestModel)

	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineNonceReplayed, "inference request nonce was already used")
}

func TestChatAtCapacityIsRetryable(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	// Fill the configured per-ticket lane.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET active_requests = $2 WHERE grant_id = $1`,
		f.grantID, f.deps.Cfg.Inference.TicketConcurrency)

	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 503, relayhttp.CodeDeclineAtCapacity, "inference lane is at capacity")
	if got := w.Header().Get("Retry-After"); got != "1" {
		t.Fatalf("Retry-After: want 1, got %q", got)
	}
}

func TestChatAdmissionUsesRefreshedBackroomConcurrency(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_concurrency_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 VALUES (0, '*', $1::jsonb, repeat('a', 64), $2, 'test')`,
		`{
			"chat_per_ticket_concurrency": 1,
			"chat_per_validator_concurrency": 1,
			"chat_global_concurrency": 1
		}`,
		"exercise the live chat concurrency policy")
	if err := f.deps.Settings.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh settings: %v", err)
	}
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET active_requests = 1 WHERE grant_id = $1`, f.grantID)

	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 503, relayhttp.CodeDeclineAtCapacity, "inference lane is at capacity")
}

// seedSiblingGrant creates a second agent + issued ticket + active grant for
// the same validator, returning the sibling grant id.
func (f *pgFixture) seedSiblingGrant(t *testing.T, hotkey string) uuid.UUID {
	t.Helper()
	siblingAgent := uuid.New()
	siblingGrant := uuid.New()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO agents (agent_id, miner_hotkey, name, sha256)
		 VALUES ($1, 'miner-hotkey-2', 'test-agent-2', repeat('b', 64))`, siblingAgent)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO validator_tickets (agent_id, validator_hotkey, slot_id, status, deadline, bench_version, attempt_count)
		 VALUES ($1, $2, 'slot-1', 'issued', $3, 9, 1)`,
		siblingAgent, hotkey, f.deadline)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_grants (
		    grant_id, agent_id, bench_version, validator_hotkey, slot_id,
		    ticket_deadline, status, bearer_digest, broker_public_key,
		    generation, allowed_models, route_provider, route_profile,
		    request_budget, token_budget, expires_at, usage_accounting_version)
		 VALUES ($1, $2, 9, $3, 'slot-1', $4, 'active', 'sibling-digest', 'sibling-key', 1,
		         '["openai/gpt-oss-20b"]'::jsonb, 'openrouter', $5,
		         8192, 25000000, $4, 2)`,
		siblingGrant, siblingAgent, hotkey, f.deadline, testProfile)
	return siblingGrant
}

// PR #735: the cross-grant validator/global rails count fresh started request
// rows, never the denormalized grant counters. A ghost counter left behind on
// a sibling grant must not starve this lease — and real fresh rows must.
func TestChatCrossGrantRailCountsFreshRowsNotGhostCounters(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	f.seedRoute(t)
	sibling := f.seedSiblingGrant(t, pgTestHotkey)

	// Ghost counters at (and beyond) the validator ceiling, with zero rows:
	// under the old counter-summing rails this admission would be starved
	// forever; fresh-row counting must admit it.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET active_requests = 50 WHERE grant_id = $1`, sibling)
	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	if w.Code != 200 {
		t.Fatalf("ghost counter must not gate admission: got %d: %s", w.Code, w.Body.String())
	}

	// Real fresh started rows on the sibling DO gate: fill the validator
	// configured validator concurrency ceiling with fresh rows.
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 SELECT $1, gen_random_uuid(), 1, 'started', 'chat', $2, 100, 100, 0, 0, 0, now()
		 FROM generate_series(1, $3)`,
		sibling, pgTestModel, f.deps.Cfg.Inference.ValidatorConcurrency)
	w = serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 503, relayhttp.CodeDeclineAtCapacity, "inference lane is at capacity")

	// The same rows crossed the recovery window: no longer provider work, the
	// rail releases without anyone revisiting the sibling grant.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_requests SET started_at = now() - interval '11 minutes' WHERE grant_id = $1`, sibling)
	w = serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	if w.Code != 200 {
		t.Fatalf("stale rows must release the rail: got %d: %s", w.Code, w.Body.String())
	}
}

func TestChatWrongBearerLearnsNothing(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	body := []byte(chatBody)
	headers := f.signedProxyHeaders(1, uuid.New(), body)
	headers["Authorization"] = "Bearer wrong-bearer-value-with-plenty-of-entropy!!"
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineUnattributed,
		"inference grant unavailable, and the reason is deliberately not disclosed to an unauthenticated caller")
}

func TestChatBadProofIs401(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))

	body := []byte(chatBody)
	headers := f.signedProxyHeaders(1, uuid.New(), body)
	// Proof signed over DIFFERENT body bytes.
	other := f.signedProxyHeaders(1, uuid.New(), []byte(`{"messages":[]}`))
	headers["X-Ditto-Proof"] = other["X-Ditto-Proof"]
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), headers))
	expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid inference proof")
}

// A client abort mid-provider-call must not cancel the upstream request.
// Python parity: uvicorn never cancels the endpoint task on a disconnect, so
// the provider call completes, REAL usage settles (never the full
// reservation), and the shared route records a SUCCESS observation — a
// cancellation-induced FAILED observation would cool the aggregate route down
// and stop grant minting fleet-wide.
func TestChatClientAbortStillSettlesRealUsage(t *testing.T) {
	clientCtx, abortClient := context.WithCancel(context.Background())
	defer abortClient()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// The broker vanishes while the provider call is in flight.
		abortClient()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"id":"gen-abort","object":"chat.completion","created":1755000000,
			"model":"openai/gpt-oss-20b","provider":"deepinfra",
			"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
				"message":{"role":"assistant","content":"hi there"}}],
			"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0.0021}
		}`))
	}))
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	f.seedRoute(t)

	nonce := uuid.New()
	body := []byte(chatBody)
	r := proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body))
	r = r.WithContext(clientCtx)
	w := serve(f.deps, r)
	if w.Code != 200 {
		t.Fatalf("handler must complete despite the aborted client: %d %s", w.Code, w.Body.String())
	}

	ctx := t.Context()
	var status string
	var prompt, completion int64
	if err := f.pool.QueryRow(ctx,
		`SELECT status, prompt_tokens, completion_tokens
		 FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &prompt, &completion); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "completed" || prompt != 12 || completion != 5 {
		t.Fatalf("abort must settle real usage, not the reservation: %s %d/%d", status, prompt, completion)
	}
	var routeStatus string
	var cooldownSet bool
	if err := f.pool.QueryRow(ctx,
		`SELECT status, cooldown_until IS NOT NULL FROM inference_provider_routes WHERE profile_revision = $1`,
		testProfile).Scan(&routeStatus, &cooldownSet); err != nil {
		t.Fatalf("read route: %v", err)
	}
	if routeStatus != "healthy" || cooldownSet {
		t.Fatalf("abort must not poison the shared route: %s cooldown=%v", routeStatus, cooldownSet)
	}
}

func TestChatUpstreamFailureChargesReservation(t *testing.T) {
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"boom"}`))
	}))
	defer failing.Close()
	f := newPGFixture(t, chatTestConfig(t, failing.URL))
	f.seedRoute(t)

	nonce := uuid.New()
	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body), f.signedProxyHeaders(1, nonce, body)))
	expectEnvelope(t, w, 502, relayhttp.CodeHTTPException, "inference provider unavailable")

	ctx := t.Context()
	var status, terminal string
	var prompt, reserved int64
	var attempts, phase int
	if err := f.pool.QueryRow(ctx,
		`SELECT status, terminal_error_code, prompt_tokens, reserved_tokens, upstream_attempts, fallback_phase
		 FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &terminal, &prompt, &reserved, &attempts, &phase); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "failed" || terminal != "upstream_http_500" {
		t.Fatalf("failure settle: status=%s terminal=%s", status, terminal)
	}
	if prompt != reserved {
		t.Fatalf("missing usage must charge the reservation: prompt=%d reserved=%d", prompt, reserved)
	}
	// 3 bounded attempts per phase, 2 phases in aggregate mode.
	if attempts != 6 || phase != 1 {
		t.Fatalf("attempts/phase: %d/%d", attempts, phase)
	}
	var active int
	var grantPrompt int64
	if err := f.pool.QueryRow(ctx,
		`SELECT active_requests, prompt_tokens FROM inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&active, &grantPrompt); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if active != 0 || grantPrompt != reserved {
		t.Fatalf("grant accounting after failure: active=%d prompt=%d", active, grantPrompt)
	}
	// A 500 is route-observable: the route cools down.
	var routeStatus string
	var cooldownSet bool
	if err := f.pool.QueryRow(ctx,
		`SELECT status, cooldown_until IS NOT NULL FROM inference_provider_routes WHERE profile_revision = $1`,
		testProfile).Scan(&routeStatus, &cooldownSet); err != nil {
		t.Fatalf("read route: %v", err)
	}
	if routeStatus != "degraded" || !cooldownSet {
		t.Fatalf("route must degrade on failure: %s cooldown=%v", routeStatus, cooldownSet)
	}
}

func TestExchangeFullFlowAndNonceReplay(t *testing.T) {
	var captured map[string]any
	upstream := fakeChatUpstream(t, &captured)
	defer upstream.Close()

	secret, pub, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatalf("keypair: %v", err)
	}
	validatorHotkey := ss58Encode(pub.Encode())

	cfg := chatTestConfig(t, upstream.URL)
	f := newPGFixtureWithHotkey(t, cfg, validatorHotkey)
	// The grant starts un-exchanged: pending, no bearer, generation 0.
	testutil.SeedSQL(t, f.pool, `UPDATE inference_grants SET status = 'pending', bearer_digest = NULL, broker_public_key = NULL, generation = 0`)

	brokerPub, brokerPriv, _ := ed25519.GenerateKey(nil)
	brokerKey := base64.URLEncoding.EncodeToString(brokerPub) // 43 chars + '='
	nonce := uuid.New()
	requestedAt := time.Now().UTC().Truncate(time.Microsecond)
	message := exchangeMessage(validatorHotkey, f.grantID, brokerKey, nonce, requestedAt)
	sig, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), message))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	encoded := sig.Encode()

	bodyMap := map[string]any{
		"validator_hotkey":  validatorHotkey,
		"grant_id":          f.grantID.String(),
		"broker_public_key": brokerKey,
		"nonce":             nonce.String(),
		"requested_at":      isoformatMicro(requestedAt),
		"signature":         hex.EncodeToString(encoded[:]),
	}
	bodyBytes, _ := json.Marshal(bodyMap)
	w := postExchange(f.deps, string(bodyBytes), validatorHotkey)
	if w.Code != 200 {
		t.Fatalf("exchange: want 200, got %d: %s", w.Code, w.Body.String())
	}
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("exchange must be no-store")
	}
	var resp struct {
		GrantID    string `json:"grant_id"`
		Bearer     string `json:"bearer"`
		ProxyURL   string `json:"proxy_url"`
		Generation int    `json:"generation"`
		Provider   string `json:"provider"`
		Model      string `json:"model"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.GrantID != f.grantID.String() || len(resp.Bearer) != 43 || resp.Generation != 1 {
		t.Fatalf("exchange response: %+v", resp)
	}
	if resp.ProxyURL != "http://localhost:8000/api/v1/inference/chat/completions" {
		t.Fatalf("proxy_url: %q", resp.ProxyURL)
	}
	if resp.Provider != "openrouter" || resp.Model != pgTestModel {
		t.Fatalf("bench>=7 fields: %+v", resp)
	}
	var nonceRows int
	if err := f.pool.QueryRow(t.Context(),
		`SELECT count(*) FROM validator_request_nonces WHERE nonce = $1`, nonce).Scan(&nonceRows); err != nil {
		t.Fatalf("count nonces: %v", err)
	}
	if nonceRows != 1 {
		t.Fatalf("nonce must be consumed")
	}

	// Replaying the exact same exchange: nonce already used.
	w = postExchange(f.deps, string(bodyBytes), validatorHotkey)
	expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "inference exchange nonce was already used")

	// The minted bearer + broker key work for a chat call.
	chatBodyBytes := []byte(chatBody)
	chatNonce := uuid.New()
	chatRequestedAt := time.Now().UTC().Truncate(time.Microsecond)
	proof := ed25519.Sign(brokerPriv, proxyMessage(f.grantID, 1, chatNonce, chatRequestedAt, chatBodyBytes))
	headers := map[string]string{
		"X-Ditto-Grant":        f.grantID.String(),
		"X-Ditto-Generation":   "1",
		"X-Ditto-Nonce":        chatNonce.String(),
		"X-Ditto-Requested-At": isoformatMicro(chatRequestedAt),
		"X-Ditto-Proof":        trimBase64Padding(base64.URLEncoding.EncodeToString(proof)),
		"Authorization":        "Bearer " + resp.Bearer,
	}
	w = serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", chatBody, headers))
	if w.Code != 200 {
		t.Fatalf("chat with minted bearer: want 200, got %d: %s", w.Code, w.Body.String())
	}
}

func TestExchangeRefusesWhileRecentRequestsInFlight(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()

	secret, pub, _ := schnorrkel.GenerateKeypair()
	validatorHotkey := ss58Encode(pub.Encode())
	f := newPGFixtureWithHotkey(t, chatTestConfig(t, upstream.URL), validatorHotkey)
	// A fresh started request blocks rotation until the recovery window.
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 VALUES ($1, $2, 1, 'started', 'chat', $3, 100, 100, 0, 0, 0, now())`,
		f.grantID, uuid.New(), pgTestModel)

	brokerPub, _, _ := ed25519.GenerateKey(nil)
	brokerKey := base64.URLEncoding.EncodeToString(brokerPub)
	nonce := uuid.New()
	requestedAt := time.Now().UTC().Truncate(time.Microsecond)
	message := exchangeMessage(validatorHotkey, f.grantID, brokerKey, nonce, requestedAt)
	sig, _ := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), message))
	encoded := sig.Encode()
	bodyBytes, _ := json.Marshal(map[string]any{
		"validator_hotkey":  validatorHotkey,
		"grant_id":          f.grantID.String(),
		"broker_public_key": brokerKey,
		"nonce":             nonce.String(),
		"requested_at":      isoformatMicro(requestedAt),
		"signature":         hex.EncodeToString(encoded[:]),
	})
	w := postExchange(f.deps, string(bodyBytes), validatorHotkey)
	expectEnvelope(t, w, 409, relayhttp.CodeHTTPException, "inference grant is not live")

	// The refusal still consumed the nonce (commits), and the grant was NOT
	// revoked (the in-flight gate makes no writes).
	var nonceRows int
	_ = f.pool.QueryRow(t.Context(),
		`SELECT count(*) FROM validator_request_nonces WHERE nonce = $1`, nonce).Scan(&nonceRows)
	if nonceRows != 1 {
		t.Fatalf("nonce must commit even on a refused rotation")
	}
	var status string
	_ = f.pool.QueryRow(t.Context(),
		`SELECT status FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&status)
	if status != "active" {
		t.Fatalf("in-flight refusal must not revoke, got %q", status)
	}
}

func embeddingBody() string {
	return `{"model":"perplexity/pplx-embed-v1-0.6b","input":["hello world"],"dimensions":768,"encoding_format":"float"}`
}

func TestEmbeddingsFullFlowDirect(t *testing.T) {
	// Direct Perplexity path: base64 signed-int8 vectors.
	vector := make([]byte, 768)
	for i := range vector {
		vector[i] = byte(i % 256)
	}
	pplx := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer test-pplx-key" {
			t.Errorf("pplx Authorization: %q", got)
		}
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if payload["model"] != "pplx-embed-v1-0.6b" || payload["encoding_format"] != "base64_int8" {
			t.Errorf("direct payload: %v", payload)
		}
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":5}}`))
	}))
	defer pplx.Close()

	cfg := testConfig(t, map[string]string{
		"PERPLEXITY_API_KEY":              "test-pplx-key",
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.EmbeddingFallbackURL = pplx.URL
	f := newPGFixture(t, cfg)

	nonce := uuid.New()
	body := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body), f.signedProxyHeaders(1, nonce, body)))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Object string `json:"object"`
		Model  string `json:"model"`
		Data   []struct {
			Object    string    `json:"object"`
			Index     int       `json:"index"`
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
		Usage struct {
			PromptTokens int64 `json:"prompt_tokens"`
			TotalTokens  int64 `json:"total_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Object != "list" || resp.Model != "perplexity/pplx-embed-v1-0.6b" ||
		len(resp.Data) != 1 || len(resp.Data[0].Embedding) != 768 ||
		resp.Usage.PromptTokens != 5 || resp.Usage.TotalTokens != 5 {
		t.Fatalf("embedding response shape: %s", w.Body.String())
	}
	// int8 conversion: byte 200 -> (200-256)/128.
	if resp.Data[0].Embedding[200] != float64(200-256)/128 {
		t.Fatalf("int8 conversion: %v", resp.Data[0].Embedding[200])
	}

	var status string
	var prompt, cost int64
	var provider string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, prompt_tokens, cost_microusd, upstream_provider FROM inference_requests
		 WHERE grant_id = $1 AND nonce = $2`, f.grantID, nonce).Scan(&status, &prompt, &cost, &provider); err != nil {
		t.Fatalf("read request: %v", err)
	}
	// Catalog price: 5 tokens * 0.004 = 0.02, banker's-rounded to 0.
	if status != "completed" || prompt != 5 || cost != 0 || provider != "Perplexity" {
		t.Fatalf("embedding settle: %s %d %d %s", status, prompt, cost, provider)
	}
	var embCount int
	var embTokens int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT embedding_request_count, embedding_tokens FROM inference_grants WHERE grant_id = $1`,
		f.grantID).Scan(&embCount, &embTokens); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if embCount != 1 || embTokens != 5 {
		t.Fatalf("grant embedding accounting: %d %d", embCount, embTokens)
	}
}

func TestEmbeddingsBackpressure(t *testing.T) {
	busy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Retry-After", "2")
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":"rate limited"}`))
	}))
	defer busy.Close()

	cfg := testConfig(t, map[string]string{
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.EmbeddingUpstreamURL = busy.URL
	f := newPGFixture(t, cfg)

	nonce := uuid.New()
	body := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body), f.signedProxyHeaders(1, nonce, body)))
	expectEnvelope(t, w, 503, relayhttp.CodeHTTPException, "embedding provider is temporarily at capacity")
	if got := w.Header().Get("Retry-After"); got != "2" {
		t.Fatalf("Retry-After: want 2, got %q", got)
	}
	var status, terminal string
	var prompt, reserved int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, terminal_error_code, prompt_tokens, reserved_tokens FROM inference_requests
		 WHERE grant_id = $1 AND nonce = $2`, f.grantID, nonce).Scan(&status, &terminal, &prompt, &reserved); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "failed" || terminal != "embedding_provider_backpressure_429" || prompt != reserved {
		t.Fatalf("backpressure settle: %s %s %d/%d", status, terminal, prompt, reserved)
	}
}

func TestEmbeddingsFullLaneIsBackpressureNotALostLease(t *testing.T) {
	upstream := fakeChatUpstream(t, nil)
	defer upstream.Close()
	f := newPGFixture(t, chatTestConfig(t, upstream.URL))
	// Fill the embedding lane (shipped default 12).
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET embedding_active_requests = 12 WHERE grant_id = $1`, f.grantID)

	body := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 503, relayhttp.CodeDeclineAtCapacity, "embedding lane is at capacity")
	if got := w.Header().Get("Retry-After"); got != "1" {
		t.Fatalf("Retry-After: want 1, got %q", got)
	}
}

func TestEmbeddingSettingsBoardRaisesTicketLimit(t *testing.T) {
	// The live board can raise the embedding per-ticket ceiling.
	vector := make([]byte, 768)
	pplx := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":10}}`))
	}))
	defer pplx.Close()
	cfg := testConfig(t, map[string]string{
		"PERPLEXITY_API_KEY":              "test-pplx-key",
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.EmbeddingFallbackURL = pplx.URL
	f := newPGFixture(t, cfg)
	checksum := strings.Repeat("ab", 32)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_concurrency_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 VALUES (0, '*', '{"chat_request_budget":8192,"chat_token_budget":25000000,
		     "embedding_per_ticket_concurrency":20,"embedding_per_validator_concurrency":48,
		     "embedding_global_concurrency":96}'::jsonb, $1, 'raise the lane', 'test-operator')`,
		checksum)
	if err := f.deps.Settings.Refresh(t.Context()); err != nil {
		t.Fatalf("refresh settings: %v", err)
	}
	// 12 in flight would exceed the shipped default but not the raised board.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET embedding_active_requests = 12 WHERE grant_id = $1`, f.grantID)

	body := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body), f.signedProxyHeaders(1, uuid.New(), body)))
	if w.Code != 200 {
		t.Fatalf("raised board must admit: got %d: %s", w.Code, w.Body.String())
	}
}
