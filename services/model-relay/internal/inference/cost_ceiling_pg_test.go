// Real-Postgres tests for the per-run cost ceiling (#3271): list-price ledger
// settlement, off/shadow/enforce admission, the X-Ditto-Ledger-Cost-Microusd
// evidence header, and 4110 on both lanes. Same fixture and skip rule as
// proxy_pg_test.go.
package inference

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/google/uuid"
	dto "github.com/prometheus/client_model/go"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/metrics"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

// The fake chat upstream reports 12 prompt + 5 completion tokens and a
// provider cost of $0.0021 (2100 micro-USD). At the stamped list prices below
// (1 and 2 micro-USD per token) one call books 12*1 + 5*2 = 22 micro-USD of
// ledger cost, whatever the provider bills.
const (
	ledgerPromptPrice     = 1e-6 // USD per token
	ledgerCompletionPrice = 2e-6
	ledgerPerChatCall     = 22
	providerCostPerCall   = 2100
)

// stampCostCeiling writes the mint-time stamps a grant would carry. ceiling
// < 0 leaves the ceiling NULL.
func (f *pgFixture) stampCostCeiling(t *testing.T, ceiling int64, mode string) {
	t.Helper()
	var ceilingArg any
	if ceiling >= 0 {
		ceilingArg = ceiling
	}
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants
		 SET cost_ceiling_microusd = $2, cost_ceiling_mode = $3,
		     ledger_prompt_price_per_token = $4, ledger_completion_price_per_token = $5
		 WHERE grant_id = $1`,
		f.grantID, ceilingArg, mode, ledgerPromptPrice, ledgerCompletionPrice)
}

func (f *pgFixture) chatCall(t *testing.T) *httptest.ResponseRecorder {
	t.Helper()
	body := []byte(chatBody)
	return serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", string(body),
		f.signedProxyHeaders(1, uuid.New(), body)))
}

func (f *pgFixture) embeddingCall(t *testing.T) *httptest.ResponseRecorder {
	t.Helper()
	body := []byte(embeddingBody())
	return serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(body),
		f.signedProxyHeaders(1, uuid.New(), body)))
}

type grantLedger struct {
	status       string
	ledger       int64
	cost         int64
	wouldDecline int64
}

func (f *pgFixture) readLedger(t *testing.T) grantLedger {
	t.Helper()
	var g grantLedger
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, ledger_cost_microusd, cost_microusd, cost_ceiling_would_decline
		 FROM inference_grants WHERE grant_id = $1`, f.grantID).
		Scan(&g.status, &g.ledger, &g.cost, &g.wouldDecline); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	return g
}

// fakeEmbeddingUpstream answers the direct Perplexity path with 5 tokens.
func fakeEmbeddingUpstream() *httptest.Server {
	vector := make([]byte, 768)
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":5}}`))
	}))
}

func ceilingTestConfig(t *testing.T, chatURL, embeddingURL string) *config.Config {
	cfg := testConfig(t, map[string]string{
		"PERPLEXITY_API_KEY":              "test-pplx-key",
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.UpstreamURL = chatURL
	cfg.Inference.EmbeddingFallbackURL = embeddingURL
	return cfg
}

func newCeilingFixture(t *testing.T) *pgFixture {
	t.Helper()
	chat := fakeChatUpstream(t, nil)
	t.Cleanup(chat.Close)
	embed := fakeEmbeddingUpstream()
	t.Cleanup(embed.Close)
	f := newPGFixture(t, ceilingTestConfig(t, chat.URL, embed.URL))
	f.seedRoute(t)
	return f
}

func counterValue(t *testing.T, c interface{ Write(*dto.Metric) error }) float64 {
	t.Helper()
	var m dto.Metric
	if err := c.Write(&m); err != nil {
		t.Fatalf("read counter: %v", err)
	}
	return m.GetCounter().GetValue()
}

func expectLedgerHeader(t *testing.T, w *httptest.ResponseRecorder, want string) {
	t.Helper()
	if got := w.Header().Get("X-Ditto-Ledger-Cost-Microusd"); got != want {
		t.Fatalf("X-Ditto-Ledger-Cost-Microusd: want %q, got %q", want, got)
	}
}

func TestCostCeilingOffModeBooksLedgerButSendsNothing(t *testing.T) {
	f := newCeilingFixture(t)
	// Off mode with list prices stamped: the ledger is booked (it is the data
	// the operator sizes the ceiling from) but nothing on the wire changes.
	f.stampCostCeiling(t, -1, "off")
	w := f.chatCall(t)
	if w.Code != 200 {
		t.Fatalf("want 200, got %d: %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "")
	g := f.readLedger(t)
	if g.ledger != ledgerPerChatCall || g.cost != providerCostPerCall || g.status != "active" {
		t.Fatalf("off-mode settle: %+v", g)
	}
	var requestLedger int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT ledger_cost_microusd FROM inference_requests WHERE grant_id = $1`, f.grantID).
		Scan(&requestLedger); err != nil {
		t.Fatalf("read request: %v", err)
	}
	if requestLedger != ledgerPerChatCall {
		t.Fatalf("request ledger: %d", requestLedger)
	}
}

func TestCostCeilingNullCeilingHasNoEffect(t *testing.T) {
	f := newCeilingFixture(t)
	// A mode with no ceiling (the board refuses this, but a grant row could
	// carry it) is inert: far past any ceiling, still admitted, no header.
	f.stampCostCeiling(t, -1, "enforce")
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET ledger_cost_microusd = 1000000000 WHERE grant_id = $1`, f.grantID)
	for _, w := range []*httptest.ResponseRecorder{f.chatCall(t), f.embeddingCall(t)} {
		if w.Code != 200 {
			t.Fatalf("null ceiling must admit: %d %s", w.Code, w.Body.String())
		}
		expectLedgerHeader(t, w, "")
	}
	if g := f.readLedger(t); g.status != "active" || g.wouldDecline != 0 {
		t.Fatalf("null ceiling touched the grant: %+v", g)
	}
}

func TestCostCeilingShadowAdmitsAndCounts(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 10, "shadow")
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET ledger_cost_microusd = 50 WHERE grant_id = $1`, f.grantID)
	chatBefore := counterValue(t, metrics.CostCeilingWouldDecline.WithLabelValues(kindChat))
	embBefore := counterValue(t, metrics.CostCeilingWouldDecline.WithLabelValues(kindEmbedding))

	w := f.chatCall(t)
	if w.Code != 200 {
		t.Fatalf("shadow must never decline: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "72") // 50 seeded + 22 settled
	w = f.embeddingCall(t)
	if w.Code != 200 {
		t.Fatalf("shadow must never decline embeddings: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "72") // 5 tokens at 0.004 rounds to 0

	g := f.readLedger(t)
	if g.status != "active" || g.wouldDecline != 2 || g.ledger != 72 {
		t.Fatalf("shadow accounting: %+v", g)
	}
	if got := counterValue(t, metrics.CostCeilingWouldDecline.WithLabelValues(kindChat)) - chatBefore; got != 1 {
		t.Fatalf("chat would-decline metric delta: %v", got)
	}
	if got := counterValue(t, metrics.CostCeilingWouldDecline.WithLabelValues(kindEmbedding)) - embBefore; got != 1 {
		t.Fatalf("embedding would-decline metric delta: %v", got)
	}
}

func TestCostCeilingEnforceCrossingCallFinishesThenBothLanes4110(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 30, "enforce")

	// 0 < 30: admitted, settles 22.
	w := f.chatCall(t)
	if w.Code != 200 {
		t.Fatalf("first call: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "22")
	// 22 < 30: admitted. This call crosses the ceiling; it still finishes
	// and is billed (settled receipts only, the 4104 rule).
	w = f.chatCall(t)
	if w.Code != 200 {
		t.Fatalf("crossing call must finish: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "44")
	g := f.readLedger(t)
	if g.ledger != 44 || g.cost != 2*providerCostPerCall || g.status != "exhausted" {
		t.Fatalf("crossing settle must bill and exhaust: %+v", g)
	}

	// Every later call, on either lane, is 4110 -- not 4104/4102.
	w = f.chatCall(t)
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "inference grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "44")
	w = f.embeddingCall(t)
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "44")

	var rows int
	if err := f.pool.QueryRow(t.Context(),
		`SELECT count(*) FROM inference_requests WHERE grant_id = $1`, f.grantID).Scan(&rows); err != nil {
		t.Fatalf("count: %v", err)
	}
	if rows != 2 {
		t.Fatalf("declined calls must not leave request rows: %d", rows)
	}
}

func TestCostCeilingEnforceWinsOverTokenWallOnExhaustedGrant(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 30, "enforce")
	// Both walls are spent; the cost ceiling names the decline on both lanes.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants
		 SET status = 'exhausted', ledger_cost_microusd = 30,
		     prompt_tokens = token_budget, request_count = request_budget
		 WHERE grant_id = $1`, f.grantID)
	expectEnvelope(t, f.chatCall(t), 429, relayhttp.CodeDeclineCostCeiling, "inference grant has spent its cost ceiling")
	expectEnvelope(t, f.embeddingCall(t), 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
}

func TestCostCeilingEnforceAdmissionStepDeclinesAndRollsBack(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 30, "enforce")
	// Settled spend at the ceiling on a still-active grant (e.g. a settle
	// that predates the stamp): the admission step itself declines.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET ledger_cost_microusd = 30 WHERE grant_id = $1`, f.grantID)
	w := f.embeddingCall(t)
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "30")
	if g := f.readLedger(t); g.status != "active" {
		t.Fatalf("decline must roll back its exhausted write: %+v", g)
	}
}

func TestCostCeilingEmbeddingLaneRefusedAfterChatDrivenCeiling(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 20, "enforce")
	if w := f.embeddingCall(t); w.Code != 200 {
		t.Fatalf("embedding before the ceiling: %d %s", w.Code, w.Body.String())
	}
	if w := f.chatCall(t); w.Code != 200 {
		t.Fatalf("chat crossing call: %d %s", w.Code, w.Body.String())
	}
	if g := f.readLedger(t); g.status != "exhausted" || g.ledger != 22 {
		t.Fatalf("chat crossing must exhaust: %+v", g)
	}
	w := f.embeddingCall(t)
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "22")
}

func TestCostCeilingEmbeddingSettlesAtStampedPrice(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 8, "enforce")
	// 2 micro-USD per token: 5 tokens -> 10, replacing the hard-coded 0.004.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET embedding_price_microusd_per_token = 2 WHERE grant_id = $1`, f.grantID)
	w := f.embeddingCall(t)
	if w.Code != 200 {
		t.Fatalf("embedding: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "10")
	var reqLedger, reqCost, grantEmbCost int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT r.ledger_cost_microusd, r.cost_microusd, g.embedding_cost_microusd
		 FROM inference_requests r JOIN inference_grants g USING (grant_id)
		 WHERE r.grant_id = $1`, f.grantID).Scan(&reqLedger, &reqCost, &grantEmbCost); err != nil {
		t.Fatalf("read: %v", err)
	}
	if reqLedger != 10 || reqCost != 10 || grantEmbCost != 10 {
		t.Fatalf("stamped embedding price: ledger=%d cost=%d grant=%d", reqLedger, reqCost, grantEmbCost)
	}
	// The embedding lane alone exhausts an enforced ceiling, and chat follows.
	expectEnvelope(t, f.chatCall(t), 429, relayhttp.CodeDeclineCostCeiling, "inference grant has spent its cost ceiling")
}

func TestCostCeilingStaleReclamationChargesZeroLedger(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 1000, "enforce")
	staleNonce := uuid.New()
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_requests (grant_id, nonce, generation, status, request_kind, model,
		    reserved_tokens, max_chargeable_tokens, prompt_tokens, completion_tokens, cost_microusd, started_at)
		 VALUES ($1, $2, 1, 'started', 'chat', $3, 900000, 900000, 0, 0, 0, now() - interval '1 hour')`,
		f.grantID, staleNonce, pgTestModel)
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET active_requests = 1, request_count = 1 WHERE grant_id = $1`, f.grantID)

	w := f.chatCall(t)
	if w.Code != 200 {
		t.Fatalf("chat: %d %s", w.Code, w.Body.String())
	}
	var status string
	var staleLedger int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, ledger_cost_microusd FROM inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, staleNonce).Scan(&status, &staleLedger); err != nil {
		t.Fatalf("read stale: %v", err)
	}
	if status != "canceled" || staleLedger != 0 {
		t.Fatalf("stale reclamation: status=%s ledger=%d", status, staleLedger)
	}
	if g := f.readLedger(t); g.ledger != ledgerPerChatCall {
		t.Fatalf("the reservation estimate must never reach the ledger: %+v", g)
	}
	expectLedgerHeader(t, w, "22")
}

func TestCostCeilingUpstreamFailureBooksZeroLedger(t *testing.T) {
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer failing.Close()
	f := newPGFixture(t, chatTestConfig(t, failing.URL))
	f.seedRoute(t)
	f.stampCostCeiling(t, 100, "enforce")
	w := f.chatCall(t)
	expectEnvelope(t, w, 502, relayhttp.CodeHTTPException, "inference provider unavailable")
	// The authenticated provider-failure response still carries the evidence.
	expectLedgerHeader(t, w, "0")
	if g := f.readLedger(t); g.ledger != 0 {
		t.Fatalf("missing usage must book no ledger cost: %+v", g)
	}
}

// TestCostCeilingSyntheticThirdLaneJoinsByBookingLedger is the extension
// point: a lane the relay does not know about (no tokens, no request counts on
// the chat or embedding columns) joins the run's ceiling purely by adding to
// ledger_cost_microusd at its settlement, and both existing lanes are refused.
func TestCostCeilingSyntheticThirdLaneJoinsByBookingLedger(t *testing.T) {
	f := newCeilingFixture(t)
	f.stampCostCeiling(t, 500, "enforce")
	// The third lane's settlement: ledger only, exactly the column chat and
	// embeddings add to.
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET ledger_cost_microusd = ledger_cost_microusd + 500 WHERE grant_id = $1`,
		f.grantID)
	var prompt, completion, embTokens int64
	if err := f.pool.QueryRow(t.Context(),
		`SELECT prompt_tokens, completion_tokens, embedding_tokens FROM inference_grants WHERE grant_id = $1`,
		f.grantID).Scan(&prompt, &completion, &embTokens); err != nil {
		t.Fatalf("read: %v", err)
	}
	if prompt+completion+embTokens != 0 {
		t.Fatalf("the synthetic lane must not touch unit walls")
	}
	expectEnvelope(t, f.chatCall(t), 429, relayhttp.CodeDeclineCostCeiling, "inference grant has spent its cost ceiling")
	expectEnvelope(t, f.embeddingCall(t), 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
}

// exchangeWithCeiling runs a real /exchange against a pending grant stamped
// with the given ceiling (ceiling < 0 = NULL).
func exchangeWithCeiling(t *testing.T, ceiling int64, mode string) *httptest.ResponseRecorder {
	t.Helper()
	secret, pub, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatalf("keypair: %v", err)
	}
	hotkey := ss58Encode(pub.Encode())
	upstream := fakeChatUpstream(t, nil)
	t.Cleanup(upstream.Close)
	f := newPGFixtureWithHotkey(t, chatTestConfig(t, upstream.URL), hotkey)
	testutil.SeedSQL(t, f.pool, `UPDATE inference_grants SET status = 'pending', bearer_digest = NULL, broker_public_key = NULL, generation = 0`)
	f.stampCostCeiling(t, ceiling, mode)

	brokerPub, _, _ := ed25519.GenerateKey(nil)
	brokerKey := base64.URLEncoding.EncodeToString(brokerPub)
	nonce := uuid.New()
	requestedAt := time.Now().UTC().Truncate(time.Microsecond)
	sig, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"),
		exchangeMessage(hotkey, f.grantID, brokerKey, nonce, requestedAt)))
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	encoded := sig.Encode()
	body, _ := json.Marshal(map[string]any{
		"validator_hotkey":  hotkey,
		"grant_id":          f.grantID.String(),
		"broker_public_key": brokerKey,
		"nonce":             nonce.String(),
		"requested_at":      isoformatMicro(requestedAt),
		"signature":         hex.EncodeToString(encoded[:]),
	})
	w := postExchange(f.deps, string(body), hotkey)
	if w.Code != 200 {
		t.Fatalf("exchange: %d %s", w.Code, w.Body.String())
	}
	return w
}

func TestExchangeCarriesCostCeilingOnlyWhenLive(t *testing.T) {
	for _, tc := range []struct {
		name    string
		ceiling int64
		mode    string
		want    bool
	}{
		{"enforce", 123456, "enforce", true},
		{"shadow", 123456, "shadow", true},
		{"off with ceiling", 123456, "off", false},
		{"null ceiling", -1, "enforce", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := exchangeWithCeiling(t, tc.ceiling, tc.mode)
			var raw map[string]json.RawMessage
			if err := json.Unmarshal(w.Body.Bytes(), &raw); err != nil {
				t.Fatalf("decode: %v", err)
			}
			_, hasCeiling := raw["cost_ceiling_microusd"]
			_, hasMode := raw["cost_ceiling_mode"]
			if !tc.want {
				if hasCeiling || hasMode ||
					w.Header().Get("X-Ditto-Cost-Ceiling-Microusd") != "" ||
					w.Header().Get("X-Ditto-Cost-Ceiling-Mode") != "" {
					t.Fatalf("no live ceiling must leave the exchange unchanged: %s %v", w.Body.String(), w.Header())
				}
				return
			}
			if string(raw["cost_ceiling_microusd"]) != "123456" || string(raw["cost_ceiling_mode"]) != `"`+tc.mode+`"` {
				t.Fatalf("body fields: %s", w.Body.String())
			}
			if w.Header().Get("X-Ditto-Cost-Ceiling-Microusd") != "123456" ||
				w.Header().Get("X-Ditto-Cost-Ceiling-Mode") != tc.mode {
				t.Fatalf("fallback headers: %v", w.Header())
			}
		})
	}
}

// decisionCeilingFixture is a decision-lane grant (pin, 42000 micro-USD per
// million input tokens) with chat and embeddings wired too, stamped with an
// enforced ceiling. One decision call books 20 (476 input tokens).
func decisionCeilingFixture(t *testing.T, ceiling int64) *pgFixture {
	t.Helper()
	var calls atomic.Int32
	decision := decisionUpstream(t, decisionPin, http.StatusOK, nil, &calls)
	f := decisionFixture(t, decision.URL, true)
	chat := fakeChatUpstream(t, nil)
	t.Cleanup(chat.Close)
	embed := fakeEmbeddingUpstream()
	t.Cleanup(embed.Close)
	f.deps.Cfg.Inference.UpstreamURL = chat.URL
	f.deps.Cfg.Inference.EmbeddingFallbackURL = embed.URL
	f.deps.Cfg.Inference.PerplexityAPIKey = "test-pplx-key"
	f.seedRoute(t)
	f.stampCostCeiling(t, ceiling, "enforce")
	return f
}

func TestCostCeilingDecisionLaneBooksLedgerAndIsRefusedWith4110(t *testing.T) {
	f := decisionCeilingFixture(t, 30)
	// 0 < 30: admitted, books 20 on both the lane meter and the ledger.
	w := postDecision(f, uuid.New())
	if w.Code != http.StatusOK {
		t.Fatalf("first decision: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "20")
	// 20 < 30: admitted; crosses (40) and finishes.
	w = postDecision(f, uuid.New())
	if w.Code != http.StatusOK {
		t.Fatalf("crossing decision must finish: %d %s", w.Code, w.Body.String())
	}
	expectLedgerHeader(t, w, "40")
	var decisionCost, ledger int64
	var status string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT decision_cost_microusd, ledger_cost_microusd, status FROM inference_grants WHERE grant_id = $1`,
		f.grantID).Scan(&decisionCost, &ledger, &status); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if decisionCost != 40 || ledger != 40 || status != "exhausted" {
		t.Fatalf("decision ledger: lane=%d ledger=%d status=%s", decisionCost, ledger, status)
	}
	// The run's ceiling ends every lane with 4110.
	w = postDecision(f, uuid.New())
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "decision grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "40")
	expectEnvelope(t, f.chatCall(t), 429, relayhttp.CodeDeclineCostCeiling, "inference grant has spent its cost ceiling")
	expectEnvelope(t, f.embeddingCall(t), 429, relayhttp.CodeDeclineCostCeiling, "embedding grant has spent its cost ceiling")
}

func TestCostCeilingChatDrivenCeilingRefusesTheDecisionLane(t *testing.T) {
	f := decisionCeilingFixture(t, 20)
	if w := f.chatCall(t); w.Code != http.StatusOK {
		t.Fatalf("chat crossing call: %d %s", w.Code, w.Body.String())
	}
	w := postDecision(f, uuid.New())
	expectEnvelope(t, w, 429, relayhttp.CodeDeclineCostCeiling, "decision grant has spent its cost ceiling")
	expectLedgerHeader(t, w, "22")
}
