package inference

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
)

// confirmationOKBody is a canonical receipt-bearing completion for the reader.
const confirmationOKBody = `{
	"id":"gen-ok","object":"chat.completion","created":1755000000,
	"model":"` + confirmationTestModel + `","provider":"deepinfra",
	"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
		"message":{"role":"assistant","content":"verdict"}}],
	"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0.00001}
}`

// scriptedUpstream answers each call with the next status in script; any call
// past the end succeeds. A receipt-free 5xx carries the gateway's routing
// metadata proving no endpoint was selected.
func scriptedUpstream(t *testing.T, script []int, calls *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := int(calls.Add(1))
		if n <= len(script) {
			w.WriteHeader(script[n-1])
			_, _ = w.Write([]byte(receiptFreeEnvelope(script[n-1], confirmationTestModel)))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(confirmationOKBody))
	}))
}

func activatedReaderFixture(t *testing.T, upstreamURL string) (*confirmationFixture, *[]time.Duration) {
	t.Helper()
	cfg := chatTestConfig(t, upstreamURL)
	cfg.Inference.ConfirmationBackpressureRetry = true
	f := newConfirmationFixture(t, cfg, "reader", confirmationTestModel, "openrouter")
	var sleeps []time.Duration
	f.deps.Sleep = func(_ context.Context, delay time.Duration) { sleeps = append(sleeps, delay) }
	return f, &sleeps
}

func serveReader(t *testing.T, f *confirmationFixture) (int, uuid.UUID) {
	t.Helper()
	nonce := uuid.New()
	body := []byte(confirmationReaderChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	return w.Code, nonce
}

// The case the measured reader failures need: a transient receipt-free 5xx is
// retried, the call completes, and the retries are recorded for the budget.
func TestConfirmationRetriesAReceiptFreeServerErrorUntilItSucceeds(t *testing.T) {
	var calls atomic.Int32
	upstream := scriptedUpstream(t, []int{502, 503}, &calls)
	defer upstream.Close()
	f, sleeps := activatedReaderFixture(t, upstream.URL)

	code, nonce := serveReader(t, f)

	if code != http.StatusOK {
		t.Fatalf("status = %d after transient 502/503; the retry did not recover", code)
	}
	if calls.Load() != 3 {
		t.Fatalf("upstream calls = %d, want 3 (502, 503, success)", calls.Load())
	}
	// Full jitter: each wait is within its exponential ceiling (2s, then 4s).
	if len(*sleeps) != 2 || (*sleeps)[0] > 2*time.Second || (*sleeps)[1] > 4*time.Second {
		t.Fatalf("backoff waits = %v, want two jittered waits within [0,2s] and [0,4s]", *sleeps)
	}
	row := readFailureDetail(t, f, nonce)
	if row.status != "completed" || !row.attempts.Valid || row.attempts.Int32 != 3 {
		t.Fatalf("settled row = %+v, want completed with upstream_attempts 3", row)
	}
}

// Four attempts, then stop: the per-call cap holds even if the upstream never
// recovers, and the failure is recorded as what it was.
func TestConfirmationTransientRetryStopsAtTheAttemptCap(t *testing.T) {
	var calls atomic.Int32
	upstream := scriptedUpstream(t, []int{502, 502, 502, 502, 502, 502}, &calls)
	defer upstream.Close()
	f, _ := activatedReaderFixture(t, upstream.URL)

	code, nonce := serveReader(t, f)

	if code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 after exhausting retries", code)
	}
	if int(calls.Load()) != confirmationTransientMaxAttempts {
		t.Fatalf("upstream calls = %d, want the cap of %d", calls.Load(), confirmationTransientMaxAttempts)
	}
	row := readFailureDetail(t, f, nonce)
	if row.kind.String != "upstream_status" || !row.receiptFree.Valid || !row.receiptFree.Bool ||
		row.attempts.Int32 != int32(confirmationTransientMaxAttempts) {
		t.Fatalf("settled row = %+v", row)
	}
}

// A 5xx without the gateway's proof of non-selection may have been billed and
// must never be re-dispatched, however transient it looks.
func TestConfirmationDoesNotRetryAServerErrorWithoutReceiptFreeProof(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":{"code":502,"message":"upstream error"}}`))
	}))
	defer upstream.Close()
	f, sleeps := activatedReaderFixture(t, upstream.URL)

	_, _ = serveReader(t, f)

	if calls.Load() != 1 || len(*sleeps) != 0 {
		t.Fatalf("calls = %d, sleeps = %v; a 5xx without proof must be single-shot", calls.Load(), *sleeps)
	}
}

// A timeout may have reached the gateway and been billed: never retried.
func TestConfirmationDoesNotRetryATimeout(t *testing.T) {
	var calls atomic.Int32
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer func() { close(release); upstream.Close() }()
	cfg := chatTestConfig(t, upstream.URL)
	cfg.Inference.ConfirmationBackpressureRetry = true
	cfg.Inference.TimeoutSeconds = 1
	f := newConfirmationFixture(t, cfg, "reader", confirmationTestModel, "openrouter")

	_, _ = serveReader(t, f)

	if calls.Load() != 1 {
		t.Fatalf("upstream calls = %d on a timeout, want 1 (never retried)", calls.Load())
	}
}

// A connection that never opened provably reached no gateway: retried, and the
// call recovers once the upstream is listening.
func TestConfirmationRetriesADialFailure(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := listener.Addr().String()
	_ = listener.Close() // nothing listens: the first dial is refused

	var calls atomic.Int32
	var dials atomic.Int32
	f, sleeps := activatedReaderFixture(t, "http://"+addr+"/v1/chat/completions")
	f.deps.Sleep = func(_ context.Context, delay time.Duration) {
		*sleeps = append(*sleeps, delay)
		if dials.Add(1) == 1 {
			// Start listening between attempts, as a recovering upstream would.
			ln, err := net.Listen("tcp", addr)
			if err != nil {
				t.Errorf("relisten: %v", err)
				return
			}
			server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(confirmationOKBody))
			})}
			go func() { _ = server.Serve(ln) }()
			t.Cleanup(func() { _ = server.Close() })
		}
	}

	code, nonce := serveReader(t, f)

	if code != http.StatusOK || calls.Load() != 1 || len(*sleeps) != 1 {
		t.Fatalf("code=%d served=%d sleeps=%v; a refused dial must be retried once and recover",
			code, calls.Load(), *sleeps)
	}
	if row := readFailureDetail(t, f, nonce); row.attempts.Int32 != 2 {
		t.Fatalf("upstream_attempts = %v, want 2 (refused dial, then success)", row.attempts)
	}
}

// The per-grant budget: once a grant has spent its retries, further calls are
// single-shot, so a degraded upstream cannot be multiplied across a bundle.
func TestConfirmationGrantRetryBudgetMakesCallsSingleShotWhenSpent(t *testing.T) {
	var calls atomic.Int32
	upstream := scriptedUpstream(t, []int{502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502, 502}, &calls)
	defer upstream.Close()
	f, _ := activatedReaderFixture(t, upstream.URL)
	// Spend the budget: mark one settled call as having used every retry.
	_, spentNonce := serveReader(t, f)
	if _, err := f.pool.Exec(t.Context(),
		`UPDATE confirmation_inference_requests SET upstream_attempts = $1 WHERE grant_id = $2 AND nonce = $3`,
		1+confirmationGrantRetryBudget, f.grantID, spentNonce); err != nil {
		t.Fatalf("spend budget: %v", err)
	}
	before := calls.Load()

	_, _ = serveReader(t, f)

	if got := calls.Load() - before; got != 1 {
		t.Fatalf("upstream calls with the budget spent = %d, want 1 (single-shot)", got)
	}
}
