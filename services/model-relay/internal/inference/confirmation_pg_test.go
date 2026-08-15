// Real-Postgres end-to-end tests for the confirmation inference plane
// (PR #699/#712): the reader/judge chat proxy and the embedding proxy under
// purpose-bound confirmation capabilities, with the replay-collapsed
// admission and the settle accounting. Tests skip (never fail) when the
// monorepo test Postgres at localhost:15433 is unavailable.
package inference

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const (
	confirmationTestModel    = "moonshotai/kimi-k2-0905"
	confirmationTestProvider = "deepinfra"
	confirmationChatPath     = "/api/v1/inference/confirmation/chat/completions"
	confirmationEmbPath      = "/api/v1/inference/confirmation/embeddings"
)

type confirmationFixture struct {
	deps       *Deps
	pool       *pgxpool.Pool
	bundleID   uuid.UUID
	ticketID   uuid.UUID
	grantID    uuid.UUID
	deadline   time.Time
	brokerPriv ed25519.PrivateKey
}

// newConfirmationFixture provisions a fresh database with the full
// confirmation chain (settings revision -> bundle -> issued ticket -> one
// active grant of the requested lane) and Deps wired to it.
func newConfirmationFixture(t *testing.T, cfg *config.Config, lane, model, provider string) *confirmationFixture {
	t.Helper()
	pool := testutil.NewTestPGPool(t)

	pub, priv, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatalf("ed25519: %v", err)
	}
	f := &confirmationFixture{
		pool:       pool,
		bundleID:   uuid.New(),
		ticketID:   uuid.New(),
		grantID:    uuid.New(),
		deadline:   time.Now().UTC().Add(30 * time.Minute).Truncate(time.Microsecond),
		brokerPriv: priv,
	}
	brokerKey := trimBase64Padding(base64.URLEncoding.EncodeToString(pub))

	testutil.SeedSQL(t, pool,
		`INSERT INTO confirmation_bundle_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 VALUES (0, '*', '{}'::jsonb, repeat('c', 64), 'initial confirmation settings', 'test-operator')`)
	testutil.SeedSQL(t, pool,
		`INSERT INTO confirmation_bundles (
		    bundle_id, artifact_sha256, bench_version, profile_revision, profile_checksum,
		    retest_generation, generation_reason, settings_revision, settings_checksum, state)
		 SELECT $1, repeat('a', 64), 9, 'confirmation-profile-v1', repeat('b', 64),
		        0, 'initial', max(revision), repeat('c', 64), 'leased'
		 FROM confirmation_bundle_settings_revisions`, f.bundleID)
	testutil.SeedSQL(t, pool,
		`INSERT INTO confirmation_bundle_tickets (
		    ticket_id, bundle_id, validator_hotkey, slot_id, status, attempt, issued_at, deadline)
		 VALUES ($1, $2, $3, 'slot-0', 'issued', 1, $4, $5)`,
		f.ticketID, f.bundleID, pgTestHotkey, f.deadline.Add(-time.Hour), f.deadline)
	testutil.SeedSQL(t, pool,
		`INSERT INTO confirmation_inference_grants (
		    grant_id, ticket_id, bundle_id, validator_hotkey, lane, status,
		    bearer_digest, broker_public_key, generation, model, provider,
		    route_provider, receipt_provider, profile_revision,
		    request_budget, token_budget, cost_budget_microusd, expires_at)
		 VALUES ($1, $2, $3, $4, $5, 'active', $6, $7, 1, $8, $9, $10, $10,
		         'confirmation-profile-v1', 100, 1000000, 5000000, $11)`,
		f.grantID, f.ticketID, f.bundleID, pgTestHotkey, lane,
		bearerDigest(testBearer), brokerKey, model, provider, confirmationTestProvider, f.deadline)

	logger := slog.New(slog.NewTextHandler(nullWriter{}, nil))
	queries := postgres.New(pool)
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

// signedHeaders builds the six auth headers with a valid Ed25519 proof over
// body for the confirmation grant.
func (f *confirmationFixture) signedHeaders(generation int64, nonce uuid.UUID, body []byte) map[string]string {
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

// confirmationChatBody carries the mandatory frozen-route provider pin.
func confirmationChatBody() string {
	return `{"model":"` + confirmationTestModel + `",` +
		`"messages":[{"role":"user","content":"judge this"}],"max_tokens":64,` +
		`"provider":{"only":["` + confirmationTestProvider + `"],` +
		`"order":["` + confirmationTestProvider + `"],` +
		`"allow_fallbacks":false,"require_parameters":true,"data_collection":"deny"}}`
}

func TestLockedConfirmationChatPayloadPreservesLaneTokenField(t *testing.T) {
	provider := confirmationProviderPreferences(confirmationTestProvider)
	payload := map[string]any{
		"model":      confirmationTestModel,
		"messages":   []any{map[string]any{"role": "user", "content": "memory"}},
		"max_tokens": json.Number("64"),
		"provider":   provider,
	}
	for _, testCase := range []struct {
		lane    string
		present string
		absent  string
	}{
		{lane: "judge", present: "max_completion_tokens", absent: "max_tokens"},
		{lane: "reader", present: "max_tokens", absent: "max_completion_tokens"},
	} {
		t.Run(testCase.lane, func(t *testing.T) {
			grant := postgres.ConfirmationInferenceGrant{
				Lane: testCase.lane, Model: confirmationTestModel, RouteProvider: confirmationTestProvider,
			}
			upstream, maxTokens, herr := lockedConfirmationChatPayload(payload, &grant, 128)
			if herr != nil || maxTokens != 64 || upstream[testCase.present] != 64 {
				t.Fatalf("locked payload=%v max=%d err=%v", upstream, maxTokens, herr)
			}
			if _, found := upstream[testCase.absent]; found {
				t.Fatalf("%s retained forbidden %s: %v", testCase.lane, testCase.absent, upstream)
			}
			lockedProvider, _ := upstream["provider"].(map[string]any)
			if lockedProvider["zdr"] != true {
				t.Fatalf("%s lost ZDR: %v", testCase.lane, lockedProvider)
			}
		})
	}
}

// fakeConfirmationUpstream returns a valid completion for the confirmation
// model and records the last upstream request payload.
func fakeConfirmationUpstream(t *testing.T, captured *map[string]any) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer test-openrouter-key" {
			t.Errorf("upstream Authorization: %q", got)
		}
		if r.Header.Get("X-OpenRouter-Metadata") != "enabled" {
			t.Errorf("metadata header missing")
		}
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if captured != nil {
			*captured = payload
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"id":"gen-c1","object":"chat.completion","created":1755000000,
			"model":"` + confirmationTestModel + `","provider":"deepinfra",
			"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
				"message":{"role":"assistant","content":"verdict"}}],
			"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0.0021,"total_tokens":17}
		}`))
	}))
}

func TestConfirmationChatFullFlow(t *testing.T) {
	var captured map[string]any
	upstream := fakeConfirmationUpstream(t, &captured)
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "judge", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	if cc := w.Header().Get("Cache-Control"); cc != "no-store" {
		t.Fatalf("Cache-Control: %q", cc)
	}
	// Byte-exact sanitized body: the ordinary contract plus the trusted
	// usage.cost (USD) and the trailing provider key.
	want := `{"id":"gen-c1","object":"chat.completion","created":1755000000,"model":"` + confirmationTestModel + `",` +
		`"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"verdict"},"logprobs":null}],` +
		`"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17,"cost":0.0021},"provider":"deepinfra"}`
	if w.Body.String() != want {
		t.Fatalf("sanitized body:\n got %s\nwant %s", w.Body.String(), want)
	}
	// The upstream payload was locked: pinned route with zdr added, usage
	// accounting requested, exactly one non-streamed completion.
	provider, _ := captured["provider"].(map[string]any)
	if provider["zdr"] != true || provider["require_parameters"] != true || provider["allow_fallbacks"] != false {
		t.Fatalf("upstream provider pin: %v", captured["provider"])
	}
	only, _ := provider["only"].([]any)
	if len(only) != 1 || only[0] != confirmationTestProvider {
		t.Fatalf("upstream provider only: %v", provider["only"])
	}
	usage, _ := captured["usage"].(map[string]any)
	if usage["include"] != true {
		t.Fatalf("upstream usage include: %v", captured["usage"])
	}
	if captured["n"] != float64(1) || captured["stream"] != false || captured["max_completion_tokens"] != float64(64) {
		t.Fatalf("upstream n/stream/max_completion_tokens: %v %v %v", captured["n"], captured["stream"], captured["max_completion_tokens"])
	}
	if _, found := captured["max_tokens"]; found {
		t.Fatalf("judge request retained max_tokens: %v", captured)
	}
	if captured["model"] != confirmationTestModel {
		t.Fatalf("upstream model: %v", captured["model"])
	}

	ctx := t.Context()
	var status, provider2 string
	var prompt, completion, cost int64
	if err := f.pool.QueryRow(ctx,
		`SELECT status, prompt_tokens, completion_tokens, cost_microusd, upstream_provider
		 FROM confirmation_inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &prompt, &completion, &cost, &provider2); err != nil {
		t.Fatalf("read request row: %v", err)
	}
	if status != "completed" || prompt != 12 || completion != 5 || cost != 2100 || provider2 != confirmationTestProvider {
		t.Fatalf("request settle: %s %d/%d cost=%d provider=%s", status, prompt, completion, cost, provider2)
	}
	var requestCount, active int
	var grantPrompt, grantCompletion, grantCost int64
	var grantStatus string
	if err := f.pool.QueryRow(ctx,
		`SELECT status, request_count, active_requests, prompt_tokens, completion_tokens, cost_microusd
		 FROM confirmation_inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&grantStatus, &requestCount, &active, &grantPrompt, &grantCompletion, &grantCost); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if grantStatus != "active" || requestCount != 1 || active != 0 ||
		grantPrompt != 12 || grantCompletion != 5 || grantCost != 2100 {
		t.Fatalf("grant accounting: %s count=%d active=%d %d/%d cost=%d",
			grantStatus, requestCount, active, grantPrompt, grantCompletion, grantCost)
	}
}

func TestConfirmationChatProofFailures(t *testing.T) {
	upstream := fakeConfirmationUpstream(t, nil)
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "reader", confirmationTestModel, "openrouter")
	body := []byte(confirmationChatBody())

	t.Run("proof over different bytes", func(t *testing.T) {
		headers := f.signedHeaders(1, uuid.New(), body)
		other := f.signedHeaders(1, uuid.New(), []byte(`{"messages":[]}`))
		headers["X-Ditto-Proof"] = other["X-Ditto-Proof"]
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), headers))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})

	t.Run("generation mismatch", func(t *testing.T) {
		headers := f.signedHeaders(2, uuid.New(), body)
		headers["X-Ditto-Generation"] = "2"
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), headers))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})

	t.Run("unknown grant", func(t *testing.T) {
		headers := f.signedHeaders(1, uuid.New(), body)
		headers["X-Ditto-Grant"] = uuid.New().String()
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), headers))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})

	t.Run("embedding lane cannot call chat", func(t *testing.T) {
		emb := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "embedding",
			config.PinnedEmbeddingModel, config.PinnedEmbeddingProvider)
		headers := emb.signedHeaders(1, uuid.New(), body)
		w := serve(emb.deps, proxyRequest(confirmationChatPath, string(body), headers))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})
}

func TestConfirmationChatRouteAndModelPins(t *testing.T) {
	upstream := fakeConfirmationUpstream(t, nil)
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "judge", confirmationTestModel, "openrouter")

	t.Run("missing provider pin", func(t *testing.T) {
		body := []byte(`{"model":"` + confirmationTestModel + `","messages":[{"role":"user","content":"x"}]}`)
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 403, relayhttp.CodeHTTPException, "confirmation route is not permitted")
	})

	t.Run("wrong provider pin", func(t *testing.T) {
		body := []byte(strings.ReplaceAll(confirmationChatBody(), confirmationTestProvider, "groq"))
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 403, relayhttp.CodeHTTPException, "confirmation route is not permitted")
	})

	t.Run("wrong model", func(t *testing.T) {
		body := []byte(strings.Replace(confirmationChatBody(), confirmationTestModel, "openai/gpt-oss-20b", 1))
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 403, relayhttp.CodeHTTPException, "confirmation model is not permitted")
	})

	t.Run("stream is refused by the shared schema", func(t *testing.T) {
		body := []byte(strings.Replace(confirmationChatBody(), `"max_tokens":64,`, `"max_tokens":64,"stream":true,`, 1))
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 400, relayhttp.CodeHTTPException,
			"unsupported inference parameter: stream (this lane answers with a single non-streaming response)")
	})
}

func TestConfirmationChatDeclines(t *testing.T) {
	upstream := fakeConfirmationUpstream(t, nil)
	defer upstream.Close()
	body := []byte(confirmationChatBody())

	newFixture := func(t *testing.T) *confirmationFixture {
		return newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "judge", confirmationTestModel, "openrouter")
	}
	post := func(f *confirmationFixture, nonce uuid.UUID) *httptest.ResponseRecorder {
		return serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	}

	t.Run("wrong bearer is unattributed", func(t *testing.T) {
		f := newFixture(t)
		headers := f.signedHeaders(1, uuid.New(), body)
		headers["Authorization"] = "Bearer wrong-bearer-value-with-plenty-of-entropy!!"
		w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), headers))
		expectEnvelope(t, w, 429, relayhttp.CodeHTTPException, "confirmation inference declined: unattributed")
	})

	t.Run("revoked grant", func(t *testing.T) {
		f := newFixture(t)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_inference_grants SET status = 'revoked' WHERE grant_id = $1`, f.grantID)
		expectEnvelope(t, post(f, uuid.New()), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: grant_revoked")
	})

	t.Run("expired ticket is lease_expired and the revocation rolls back", func(t *testing.T) {
		f := newFixture(t)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_bundle_tickets SET deadline = now() - interval '1 minute',
			     issued_at = now() - interval '2 hours', status = 'expired'
			 WHERE ticket_id = $1`, f.ticketID)
		expectEnvelope(t, post(f, uuid.New()), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: lease_expired")
		var status string
		if err := f.pool.QueryRow(t.Context(),
			`SELECT status FROM confirmation_inference_grants WHERE grant_id = $1`, f.grantID).Scan(&status); err != nil {
			t.Fatalf("read grant: %v", err)
		}
		if status != "active" {
			t.Fatalf("decline must roll back the revocation write, got %q", status)
		}
	})

	t.Run("request budget exhausted rolls back its writes", func(t *testing.T) {
		f := newFixture(t)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_inference_grants SET request_count = request_budget WHERE grant_id = $1`, f.grantID)
		expectEnvelope(t, post(f, uuid.New()), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: budget_exhausted")
		var status string
		var rows int
		if err := f.pool.QueryRow(t.Context(),
			`SELECT status, (SELECT count(*) FROM confirmation_inference_requests WHERE grant_id = $1)
			 FROM confirmation_inference_grants WHERE grant_id = $1`, f.grantID).Scan(&status, &rows); err != nil {
			t.Fatalf("read grant: %v", err)
		}
		if status != "active" || rows != 0 {
			t.Fatalf("decline must roll back exhaustion + provisional row: %s/%d", status, rows)
		}
	})

	t.Run("token budget exhausted", func(t *testing.T) {
		f := newFixture(t)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_inference_grants SET prompt_tokens = token_budget WHERE grant_id = $1`, f.grantID)
		expectEnvelope(t, post(f, uuid.New()), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: token_budget_exhausted")
	})

	t.Run("cost budget exhausted", func(t *testing.T) {
		f := newFixture(t)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_inference_grants SET cost_microusd = cost_budget_microusd WHERE grant_id = $1`, f.grantID)
		expectEnvelope(t, post(f, uuid.New()), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: cost_budget_exhausted")
	})

	t.Run("nonce replay", func(t *testing.T) {
		f := newFixture(t)
		nonce := uuid.New()
		testutil.SeedSQL(t, f.pool,
			`INSERT INTO confirmation_inference_requests (grant_id, nonce, generation, status, model,
			    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at, completed_at)
			 VALUES ($1, $2, 1, 'completed', $3, 100, 100, 90, 10, 1, now(), now())`,
			f.grantID, nonce, confirmationTestModel)
		expectEnvelope(t, post(f, nonce), 429, relayhttp.CodeHTTPException,
			"confirmation inference declined: nonce_replayed")
	})
}

func TestConfirmationChatProviderFailureChargesReservation(t *testing.T) {
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"error":"boom"}`))
	}))
	defer failing.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, failing.URL), "judge", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	expectEnvelope(t, w, 502, relayhttp.CodeHTTPException, "confirmation provider unavailable")

	ctx := t.Context()
	var status string
	var prompt, completion, cost, reserved int64
	var providerNull bool
	if err := f.pool.QueryRow(ctx,
		`SELECT status, prompt_tokens, completion_tokens, cost_microusd, reserved_tokens, upstream_provider IS NULL
		 FROM confirmation_inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &prompt, &completion, &cost, &reserved, &providerNull); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "failed" || prompt != reserved || completion != 0 || cost != 0 || !providerNull {
		t.Fatalf("failure settle: %s %d/%d cost=%d reserved=%d providerNull=%v",
			status, prompt, completion, cost, reserved, providerNull)
	}
	var active int
	var grantPrompt int64
	if err := f.pool.QueryRow(ctx,
		`SELECT active_requests, prompt_tokens FROM confirmation_inference_grants WHERE grant_id = $1`,
		f.grantID).Scan(&active, &grantPrompt); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if active != 0 || grantPrompt != reserved {
		t.Fatalf("grant accounting after failure: active=%d prompt=%d want %d", active, grantPrompt, reserved)
	}
}

// Python computes `_bounded_provider_cost(decoded) or -1`, so a zero provider
// cost collapses to -1 and is refused as an identity mismatch — reproduced
// deliberately.
func TestConfirmationChatZeroCostIsRefused(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"id":"gen-c2","object":"chat.completion","created":1755000000,
			"model":"` + confirmationTestModel + `","provider":"deepinfra",
			"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
				"message":{"role":"assistant","content":"verdict"}}],
			"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0}
		}`))
	}))
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "judge", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	expectEnvelope(t, w, 502, relayhttp.CodeHTTPException, "provider identity mismatch")

	var status string
	var cost int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, cost_microusd FROM confirmation_inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&status, &cost); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if status != "failed" || cost != 0 {
		t.Fatalf("zero-cost settle: %s cost=%d", status, cost)
	}
}

func TestConfirmationChatSettlementExhaustsSpentBudget(t *testing.T) {
	upstream := fakeConfirmationUpstream(t, nil)
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "reader", confirmationTestModel, "openrouter")
	// One request left in the budget: the settle marks the grant exhausted.
	testutil.SeedSQL(t, f.pool,
		`UPDATE confirmation_inference_grants SET request_count = request_budget - 1 WHERE grant_id = $1`, f.grantID)

	body := []byte(confirmationChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
	if w.Code != 200 {
		t.Fatalf("last budgeted call must succeed: %d %s", w.Code, w.Body.String())
	}
	var status string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status FROM confirmation_inference_grants WHERE grant_id = $1`, f.grantID).Scan(&status); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if status != "exhausted" {
		t.Fatalf("settle must exhaust the spent budget, got %q", status)
	}
	// And the exhausted grant declines terminally on the next call.
	w = serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, 429, relayhttp.CodeHTTPException, "confirmation inference declined: budget_exhausted")
}

func TestConfirmationEmbeddingsFullFlow(t *testing.T) {
	vector := make([]byte, 768)
	for i := range vector {
		vector[i] = byte(i % 256)
	}
	pplx := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":500}}`))
	}))
	defer pplx.Close()
	cfg := testConfig(t, map[string]string{
		"PERPLEXITY_API_KEY":              "test-pplx-key",
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.EmbeddingFallbackURL = pplx.URL
	f := newConfirmationFixture(t, cfg, "embedding", config.PinnedEmbeddingModel, strings.ToLower(config.PinnedEmbeddingProvider))

	nonce := uuid.New()
	// A body large enough that the provider's 500-token usage stays under the
	// byte-derived chargeable ceiling (tokens can never exceed body bytes).
	body := []byte(`{"model":"perplexity/pplx-embed-v1-0.6b","input":["` +
		strings.Repeat("hello memory ", 50) + `"],"dimensions":768,"encoding_format":"float"}`)
	w := serve(f.deps, proxyRequest(confirmationEmbPath, string(body), f.signedHeaders(1, nonce, body)))
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	var resp struct {
		Object string `json:"object"`
		Model  string `json:"model"`
		Data   []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
		Usage struct {
			PromptTokens int64 `json:"prompt_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Object != "list" || resp.Model != config.PinnedEmbeddingModel ||
		len(resp.Data) != 1 || len(resp.Data[0].Embedding) != 768 || resp.Usage.PromptTokens != 500 {
		t.Fatalf("embedding response shape: %s", w.Body.String())
	}

	var status, provider string
	var prompt, cost int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, prompt_tokens, cost_microusd, upstream_provider FROM confirmation_inference_requests
		 WHERE grant_id = $1 AND nonce = $2`, f.grantID, nonce).Scan(&status, &prompt, &cost, &provider); err != nil {
		t.Fatalf("read request: %v", err)
	}
	// Catalog price: 500 tokens * 0.004 = 2 micro-USD (banker's rounding).
	if status != "completed" || prompt != 500 || cost != 2 || provider != confirmationTestProvider {
		t.Fatalf("embedding settle: %s %d %d %s", status, prompt, cost, provider)
	}
	var requestCount, active int
	var grantPrompt int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT request_count, active_requests, prompt_tokens FROM confirmation_inference_grants WHERE grant_id = $1`,
		f.grantID).Scan(&requestCount, &active, &grantPrompt); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if requestCount != 1 || active != 0 || grantPrompt != 500 {
		t.Fatalf("grant embedding accounting: %d %d %d", requestCount, active, grantPrompt)
	}
}

func TestConfirmationEmbeddingsLaneAndIdentityPins(t *testing.T) {
	upstream := fakeConfirmationUpstream(t, nil)
	defer upstream.Close()
	cfg := chatTestConfig(t, upstream.URL)
	body := []byte(embeddingBody())

	t.Run("chat lane cannot call embeddings", func(t *testing.T) {
		f := newConfirmationFixture(t, cfg, "judge", confirmationTestModel, "openrouter")
		w := serve(f.deps, proxyRequest(confirmationEmbPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})

	t.Run("embedding grant with a drifted model is refused", func(t *testing.T) {
		f := newConfirmationFixture(t, cfg, "embedding", "some-other-model", config.PinnedEmbeddingProvider)
		w := serve(f.deps, proxyRequest(confirmationEmbPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 401, relayhttp.CodeHTTPException, "invalid confirmation proof")
	})

	t.Run("embedding decline detail names the embedding lane", func(t *testing.T) {
		f := newConfirmationFixture(t, cfg, "embedding", config.PinnedEmbeddingModel, config.PinnedEmbeddingProvider)
		testutil.SeedSQL(t, f.pool,
			`UPDATE confirmation_inference_grants SET status = 'exhausted' WHERE grant_id = $1`, f.grantID)
		w := serve(f.deps, proxyRequest(confirmationEmbPath, string(body), f.signedHeaders(1, uuid.New(), body)))
		expectEnvelope(t, w, 429, relayhttp.CodeHTTPException, "confirmation embedding declined: budget_exhausted")
	})
}
