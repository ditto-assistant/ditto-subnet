package inference

import (
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"

	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

// A no-chat grant (inference_mode = 'no_chat', docs/no-inference-mode.md)
// refuses every chat call as model_not_permitted before any provider or
// ledger work, while embeddings and the decision lane stay open.

func noChatFixture(t *testing.T) (*pgFixture, *atomic.Int32, *atomic.Int32, *atomic.Int32) {
	t.Helper()
	var chatCalls, embedCalls, decisionCalls atomic.Int32
	chat := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chatCalls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"gen-1","object":"chat.completion","created":1755000000,` +
			`"model":"openai/gpt-oss-20b","provider":"deepinfra","choices":[{"index":0,"finish_reason":"stop",` +
			`"message":{"role":"assistant","content":"hi"}}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}`))
	}))
	t.Cleanup(chat.Close)
	vector := make([]byte, 768)
	embed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		embedCalls.Add(1)
		_, _ = w.Write([]byte(`{"model":"pplx-embed-v1-0.6b","data":[{"index":0,"embedding":"` +
			base64.StdEncoding.EncodeToString(vector) + `"}],"usage":{"prompt_tokens":5}}`))
	}))
	t.Cleanup(embed.Close)
	decisions := decisionUpstream(t, decisionPin, http.StatusOK, nil, &decisionCalls)

	cfg := testConfig(t, map[string]string{
		"PERPLEXITY_API_KEY":              "test-pplx-key",
		"DITTO_INFERENCE_TIMEOUT_SECONDS": "5",
	})
	cfg.Inference.UpstreamURL = chat.URL
	cfg.Inference.EmbeddingFallbackURL = embed.URL
	cfg.Inference.DecisionUpstreamURL = decisions.URL
	f := newPGFixture(t, cfg)
	f.seedRoute(t)
	testutil.SeedSQL(t, f.pool,
		`INSERT INTO inference_concurrency_settings_revisions
		    (parent_revision, scope, settings, checksum, reason, actor)
		 VALUES (0, '*', '{"decision_lane":{"enabled":true,"model":"typesafe/jev-1.14"}}'::jsonb,
		         $1, 'open the decision lane', 'test-operator')`,
		strings.Repeat("ab", 32))
	testutil.SeedSQL(t, f.pool,
		`UPDATE inference_grants SET decision_model = $2, decision_input_microusd_per_mtok = 42000,
		     decision_request_budget = 100, decision_token_budget = 1000000, inference_mode = 'no_chat'
		 WHERE grant_id = $1`, f.grantID, decisionPin)
	if err := f.deps.Settings.Refresh(t.Context()); err != nil {
		t.Fatalf("refresh settings: %v", err)
	}
	return f, &chatCalls, &embedCalls, &decisionCalls
}

func TestNoChatGrantRefusesChatBeforeProviderAndLedger(t *testing.T) {
	f, chatCalls, _, _ := noChatFixture(t)

	body := []byte(chatBody)
	w := serve(f.deps, proxyRequest("/api/v1/inference/chat/completions", chatBody, f.signedProxyHeaders(1, uuid.New(), body)))
	expectEnvelope(t, w, http.StatusTooManyRequests, relayhttp.CodeDeclineModelNotPermitted, "inference grant does not permit this model")

	var rows int
	var requestCount int32
	var status string
	if err := f.pool.QueryRow(t.Context(),
		`SELECT (SELECT count(*) FROM inference_requests WHERE grant_id = $1), request_count, status
		 FROM inference_grants WHERE grant_id = $1`, f.grantID).Scan(&rows, &requestCount, &status); err != nil {
		t.Fatalf("read grant: %v", err)
	}
	if chatCalls.Load() != 0 || rows != 0 || requestCount != 0 || status != "active" {
		t.Fatalf("a refused no-chat call must touch neither the provider nor the ledger: calls=%d rows=%d count=%d status=%s",
			chatCalls.Load(), rows, requestCount, status)
	}
}

func TestNoChatGrantKeepsEmbeddingsAndDecisions(t *testing.T) {
	f, chatCalls, embedCalls, decisionCalls := noChatFixture(t)

	embedding := []byte(embeddingBody())
	w := serve(f.deps, proxyRequest("/api/v1/inference/embeddings", string(embedding), f.signedProxyHeaders(1, uuid.New(), embedding)))
	if w.Code != http.StatusOK {
		t.Fatalf("embeddings on a no-chat grant: want 200, got %d: %s", w.Code, w.Body.String())
	}
	w = postDecision(f, uuid.New())
	if w.Code != http.StatusOK {
		t.Fatalf("decisions on a no-chat grant: want 200, got %d: %s", w.Code, w.Body.String())
	}
	if chatCalls.Load() != 0 || embedCalls.Load() != 1 || decisionCalls.Load() != 1 {
		t.Fatalf("upstream calls: chat=%d embed=%d decision=%d", chatCalls.Load(), embedCalls.Load(), decisionCalls.Load())
	}
}

func TestNoChatGrantRequiresADecisionPin(t *testing.T) {
	f := newPGFixture(t, testConfig(t, nil))
	_, err := f.pool.Exec(t.Context(),
		`UPDATE inference_grants SET inference_mode = 'no_chat' WHERE grant_id = $1`, f.grantID)
	if err == nil || !strings.Contains(err.Error(), "inference_mode") {
		t.Fatalf("a no-chat grant without a decision pin must violate the mode check, got %v", err)
	}
	_, err = f.pool.Exec(t.Context(),
		`UPDATE inference_grants SET inference_mode = 'none' WHERE grant_id = $1`, f.grantID)
	if err == nil {
		t.Fatalf("an unknown inference mode must violate the mode check")
	}
}
