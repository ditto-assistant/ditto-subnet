package inference

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

// failureDetailRow reads the four columns the relay now writes on a failed
// confirmation call. They exist because every confirmation provider failure
// used to settle as an indistinguishable "failed, no provider, no cost" row.
type failureDetailRow struct {
	status      string
	kind        pgtype.Text
	httpStatus  pgtype.Int4
	attempts    pgtype.Int4
	receiptFree pgtype.Bool
}

func readFailureDetail(t *testing.T, f *confirmationFixture, nonce uuid.UUID) failureDetailRow {
	t.Helper()
	var row failureDetailRow
	if err := f.pool.QueryRow(t.Context(),
		`SELECT status, failure_kind, upstream_http_status, upstream_attempts, receipt_free
		   FROM confirmation_inference_requests WHERE grant_id = $1 AND nonce = $2`,
		f.grantID, nonce).Scan(&row.status, &row.kind, &row.httpStatus, &row.attempts, &row.receiptFree); err != nil {
		t.Fatalf("read failure detail: %v", err)
	}
	return row
}

func TestConfirmationFailureDetailRecordsAnUpstreamStatus(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":{"code":502,"message":"upstream unavailable"}}`))
	}))
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "reader", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationReaderChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	expectEnvelope(t, w, 502, relayhttp.CodeHTTPException, "confirmation provider unavailable")

	row := readFailureDetail(t, f, nonce)
	if row.status != "failed" || row.kind.String != "upstream_status" || !row.kind.Valid {
		t.Fatalf("kind: status=%s kind=%v", row.status, row.kind)
	}
	if !row.httpStatus.Valid || row.httpStatus.Int32 != http.StatusBadGateway {
		t.Fatalf("upstream_http_status = %v, want 502", row.httpStatus)
	}
	if !row.attempts.Valid || row.attempts.Int32 != 1 {
		t.Fatalf("upstream_attempts = %v, want 1", row.attempts)
	}
	// receipt_free is the relay's own classification; it must be recorded as a
	// known value either way, never left null on an upstream rejection.
	if !row.receiptFree.Valid {
		t.Fatalf("receipt_free unknown on an upstream rejection: %v", row.receiptFree)
	}
}

func TestConfirmationFailureDetailRecordsATransportTimeout(t *testing.T) {
	release := make(chan struct{})
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer func() { close(release); upstream.Close() }()
	cfg := chatTestConfig(t, upstream.URL)
	cfg.Inference.TimeoutSeconds = 1
	f := newConfirmationFixture(t, cfg, "reader", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationReaderChatBody())
	started := time.Now()
	_ = serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	if elapsed := time.Since(started); elapsed > 10*time.Second {
		t.Fatalf("timeout path took %s", elapsed)
	}

	row := readFailureDetail(t, f, nonce)
	if row.status != "failed" || row.kind.String != "transport_timeout" {
		t.Fatalf("kind: status=%s kind=%v", row.status, row.kind)
	}
	// No response arrived, so there is no status to record -- and no receipt,
	// which makes a transport failure receipt-free by construction.
	if row.httpStatus.Valid {
		t.Fatalf("upstream_http_status = %v on a timeout, want NULL", row.httpStatus)
	}
	if !row.receiptFree.Valid || !row.receiptFree.Bool {
		t.Fatalf("receipt_free = %v on a timeout, want true", row.receiptFree)
	}
}

func TestConfirmationFailureDetailLeavesACompletedCallNull(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"id":"gen-ok","object":"chat.completion","created":1755000000,
			"model":"` + confirmationTestModel + `","provider":"deepinfra",
			"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
				"message":{"role":"assistant","content":"verdict"}}],
			"usage":{"prompt_tokens":12,"completion_tokens":5,"cost":0.00001}
		}`))
	}))
	defer upstream.Close()
	f := newConfirmationFixture(t, chatTestConfig(t, upstream.URL), "reader", confirmationTestModel, "openrouter")

	nonce := uuid.New()
	body := []byte(confirmationReaderChatBody())
	w := serve(f.deps, proxyRequest(confirmationChatPath, string(body), f.signedHeaders(1, nonce, body)))
	if w.Code != http.StatusOK {
		t.Fatalf("completed call returned %d: %s", w.Code, w.Body.String())
	}

	row := readFailureDetail(t, f, nonce)
	if row.status != "completed" || row.kind.Valid || row.httpStatus.Valid || row.attempts.Valid || row.receiptFree.Valid {
		t.Fatalf("completed call carries failure detail: %+v", row)
	}
}
