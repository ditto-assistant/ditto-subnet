package inference

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"
)

// openRouterErrorEnvelope is the body OpenRouter documents for a failure after
// it has already sent 200: an id and an error object, nothing else. The example
// shape is from its errors-and-debugging guide and matches public reports
// (e.g. "Upstream error from Nvidia: Service temporarily overloaded", 502).
func openRouterErrorEnvelope(code int) string {
	return fmt.Sprintf(`{"id":"gen-1761868800-envelope","error":{"code":%d,`+
		`"message":"Upstream error: service temporarily overloaded",`+
		`"metadata":{"error_type":"provider_unavailable"}}}`, code)
}

// envelopeUpstream answers each call with a 200 error envelope carrying the
// next code in script; any call past the end succeeds.
func envelopeUpstream(t *testing.T, script []int, calls *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := int(calls.Add(1))
		w.Header().Set("Content-Type", "application/json")
		if n <= len(script) {
			_, _ = w.Write([]byte(openRouterErrorEnvelope(script[n-1])))
			return
		}
		_, _ = w.Write([]byte(confirmationOKBody))
	}))
}

func readErrorCode(t *testing.T, f *confirmationFixture, row failureDetailRow) pgtype.Int4 {
	t.Helper()
	var code pgtype.Int4
	if err := f.pool.QueryRow(t.Context(),
		`SELECT upstream_error_code FROM confirmation_inference_requests
		  WHERE grant_id = $1 ORDER BY completed_at DESC LIMIT 1`, f.grantID).Scan(&code); err != nil {
		t.Fatalf("read error code: %v", err)
	}
	return code
}

// The bundle 7fa29ad3 case: a transient error inside a 200 is retried, and the
// call completes instead of ending the bundle.
func TestConfirmationRetriesATransientErrorEnvelopeInA200(t *testing.T) {
	var calls atomic.Int32
	upstream := envelopeUpstream(t, []int{502}, &calls)
	defer upstream.Close()
	f, sleeps := activatedReaderFixture(t, upstream.URL)

	code, nonce := serveReader(t, f)

	if code != http.StatusOK {
		t.Fatalf("status = %d after a transient 200 error envelope; the retry did not recover", code)
	}
	if calls.Load() != 2 || len(*sleeps) != 1 {
		t.Fatalf("calls = %d sleeps = %v, want one retry", calls.Load(), *sleeps)
	}
	if row := readFailureDetail(t, f, nonce); row.status != "completed" || row.attempts.Int32 != 2 {
		t.Fatalf("settled row = %+v, want completed after 2 attempts", row)
	}
}

// Exhausted, it is recorded as what it is -- not as an identity mismatch, which
// is what it looked like before because the envelope has no model field.
func TestConfirmationRecordsAPersistentErrorEnvelopeAsItsOwnKind(t *testing.T) {
	var calls atomic.Int32
	upstream := envelopeUpstream(t, []int{503, 503, 503, 503, 503, 503}, &calls)
	defer upstream.Close()
	f, _ := activatedReaderFixture(t, upstream.URL)

	code, nonce := serveReader(t, f)

	if code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 after exhausting retries", code)
	}
	if int(calls.Load()) != confirmationTransientMaxAttempts {
		t.Fatalf("calls = %d, want the cap of %d", calls.Load(), confirmationTransientMaxAttempts)
	}
	row := readFailureDetail(t, f, nonce)
	if row.kind.String != "upstream_error_envelope" || row.httpStatus.Int32 != 200 {
		t.Fatalf("kind/status = %v/%v, want upstream_error_envelope/200", row.kind, row.httpStatus)
	}
	if code := readErrorCode(t, f, row); !code.Valid || code.Int32 != 503 {
		t.Fatalf("upstream_error_code = %v, want 503", code)
	}
	// Not proven receipt-free: OpenRouter documents the upstream may charge for
	// prompt processing even with no content. It must stay unknown.
	if row.receiptFree.Valid {
		t.Fatalf("receipt_free = %v on an error envelope, want NULL", row.receiptFree)
	}
}

// A permanent error inside a 200 (bad request, auth, payment, policy) is never
// retried: repeating it cannot succeed and only spends the budget.
func TestConfirmationDoesNotRetryAPermanentErrorEnvelope(t *testing.T) {
	for _, permanent := range []int{400, 401, 402, 403} {
		var calls atomic.Int32
		upstream := envelopeUpstream(t, []int{permanent, permanent}, &calls)
		f, sleeps := activatedReaderFixture(t, upstream.URL)

		_, nonce := serveReader(t, f)
		upstream.Close()

		if calls.Load() != 1 || len(*sleeps) != 0 {
			t.Fatalf("code %d: calls = %d sleeps = %v, want single-shot", permanent, calls.Load(), *sleeps)
		}
		if row := readFailureDetail(t, f, nonce); row.kind.String != "upstream_error_envelope" {
			t.Fatalf("code %d: kind = %v, want upstream_error_envelope", permanent, row.kind)
		}
	}
}

// A 200 that carries an error AND content is not the envelope: it may be a
// partial, billed response, and the strict classifier must refuse it.
func TestConfirmationDoesNotTreatAnErrorWithChoicesAsAnEnvelope(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"id":"gen-x","error":{"code":502,"message":"partial"},` +
			`"choices":[{"index":0,"finish_reason":"error","message":{"role":"assistant","content":"par"}}],` +
			`"usage":{"prompt_tokens":10,"completion_tokens":1,"cost":0.00001}}`))
	}))
	defer upstream.Close()
	f, sleeps := activatedReaderFixture(t, upstream.URL)

	_, _ = serveReader(t, f)

	if calls.Load() != 1 || len(*sleeps) != 0 {
		t.Fatalf("calls = %d sleeps = %v; a 200 with content and usage must never be re-sent", calls.Load(), *sleeps)
	}
}
