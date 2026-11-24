package inference

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

// The scorer's broker gives the relay one fixed HTTP client timeout for the
// whole chat or embedding request (#2973). The recovery ladder therefore has
// one budget, TimeoutSeconds, shared by every attempt it makes; these tests
// use a 2 s budget so the arithmetic is visible without slowing the suite.

const ladderTestBudget = 2 * time.Second

// stall answers after delay, or as soon as the client gives up.
func stall(r *http.Request, delay time.Duration) bool {
	select {
	case <-time.After(delay):
		return true
	case <-r.Context().Done():
		return false
	}
}

func ladderChatConfig(upstreamURL string) config.InferenceProxyConfig {
	cfg := aggregateChatConfig(upstreamURL)
	cfg.TimeoutSeconds = int(ladderTestBudget / time.Second)
	return cfg
}

func TestChatRecoveryLadderIsBoundedByOneTimeout(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			// A receipt-free 502 that leaves more than half the budget is
			// retried -- but the retry inherits only what is left of it.
			if stall(r, ladderTestBudget*4/10) {
				w.WriteHeader(http.StatusBadGateway)
				_, _ = w.Write([]byte(receiptFreeEnvelope(502, "openai/gpt-oss-20b")))
			}
			return
		}
		if stall(r, 2*ladderTestBudget) {
			_, _ = w.Write([]byte(validAggregateChatBody))
		}
	}))
	defer upstream.Close()

	started := time.Now()
	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), ladderChatConfig(upstream.URL),
		map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil, defaultSleep,
	)
	elapsed := time.Since(started)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if got := calls.Load(); got != providerSafeRetryMaxAttempts {
		t.Fatalf("upstream calls=%d want %d", got, providerSafeRetryMaxAttempts)
	}
	if elapsed > ladderTestBudget+250*time.Millisecond {
		t.Fatalf("ladder ran %s, past its %s budget", elapsed, ladderTestBudget)
	}
	if !exhausted.timedOut || exhausted.terminalErrorCode != "provider_timeout" {
		t.Fatalf("terminal=%q timedOut=%v", exhausted.terminalErrorCode, exhausted.timedOut)
	}
	if len(exhausted.phases) != 2 || exhausted.phases[0].errorCode != "upstream_http_502" {
		t.Fatalf("phase trace=%+v", exhausted.phases)
	}
}

func TestChatRecoveryLadderSkipsARetryThatCannotFit(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		// The receipt-free 502 arrives after more than half the budget: a
		// retry would start with too little time to finish and could only
		// end in an ambiguous, possibly billed, timeout.
		if stall(r, ladderTestBudget*6/10) {
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte(receiptFreeEnvelope(502, "openai/gpt-oss-20b")))
		}
	}))
	defer upstream.Close()

	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), ladderChatConfig(upstream.URL),
		map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(context.Context, time.Duration) { t.Fatal("must not wait for a retry that cannot fit") },
	)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if got := calls.Load(); got != 1 || exhausted.upstreamAttempts != 1 {
		t.Fatalf("calls=%d attempts=%d want a single attempt", got, exhausted.upstreamAttempts)
	}
	// The truthful outcome is the clean, receipt-free rejection in hand.
	if exhausted.timedOut || exhausted.terminalErrorCode != "upstream_http_502" {
		t.Fatalf("terminal=%q timedOut=%v", exhausted.terminalErrorCode, exhausted.timedOut)
	}
}

func ladderEmbeddingConfig(directURL, routerURL string) config.InferenceProxyConfig {
	return config.InferenceProxyConfig{
		OpenRouterAPIKey: "router-key", PerplexityAPIKey: "direct-key",
		EmbeddingFallbackURL: directURL, EmbeddingUpstreamURL: routerURL,
		EmbeddingModel: "perplexity/pplx-embed-v1-0.6b", EmbeddingProvider: "perplexity",
		EmbeddingDimensions: 768, EmbeddingResponseBodyBytes: 1024,
		TimeoutSeconds: int(ladderTestBudget / time.Second),
	}
}

const directEmbeddingBackpressure = `{"error":{"code":429,"message":"rate limited","type":"request_rate_limit_exceeded"}}`

func TestEmbeddingFallbackSharesTheDirectRouteBudget(t *testing.T) {
	direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if stall(r, ladderTestBudget*4/10) {
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(directEmbeddingBackpressure))
		}
	}))
	defer direct.Close()
	router := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if stall(r, 2*ladderTestBudget) {
			w.WriteHeader(http.StatusOK)
		}
	}))
	defer router.Close()

	started := time.Now()
	result, callErr := postEmbeddingProvider(context.Background(), direct.Client(),
		ladderEmbeddingConfig(direct.URL, router.URL), []string{"memory"}, defaultSleep)
	elapsed := time.Since(started)
	if callErr == nil || !callErr.timedOut {
		t.Fatalf("result=%+v error=%+v, want the fallback to time out", result, callErr)
	}
	if elapsed > ladderTestBudget+250*time.Millisecond {
		t.Fatalf("embedding ladder ran %s, past its %s budget", elapsed, ladderTestBudget)
	}
}

// postEmbeddingProvider serves both the scored /embeddings route and the
// confirmation embedding lane, and the broker forwards both with its 100 s
// client, so this bound covers both. The confirmation handler is pinned end to
// end by TestConfirmationEmbeddingsSkipFallThroughThatCannotFit.
func TestEmbeddingFallbackIsSkippedWhenItCannotFit(t *testing.T) {
	var routerCalls atomic.Int32
	direct := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if stall(r, ladderTestBudget*6/10) {
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(directEmbeddingBackpressure))
		}
	}))
	defer direct.Close()
	router := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		routerCalls.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer router.Close()

	result, callErr := postEmbeddingProvider(context.Background(), direct.Client(),
		ladderEmbeddingConfig(direct.URL, router.URL), []string{"memory"}, defaultSleep)
	if callErr != nil || result == nil || !result.direct || result.result.status != http.StatusTooManyRequests {
		t.Fatalf("result=%+v error=%+v, want the direct 429 returned as-is", result, callErr)
	}
	if routerCalls.Load() != 0 || result.attempts != 1 || len(result.phases) != 1 {
		t.Fatalf("router calls=%d attempts=%d phases=%d", routerCalls.Load(), result.attempts, len(result.phases))
	}
}
