package inference

import (
	"net/http"
	"testing"
)

func overloadPhase(status int, body string) phaseTrace {
	return phaseTrace{
		status:  status,
		headers: http.Header{"Retry-After": []string{"2"}},
		body:    []byte(body),
	}
}

func TestReceiptFreeOverloadRequiresEveryPhaseToBeCanonicalBackpressure(t *testing.T) {
	body := `{"error":{"code":429,"message":"rate limited"}}`
	status, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, body), overloadPhase(429, body)},
		"openai/gpt-oss-20b",
		"deepinfra",
	)
	if !ok || status != 429 {
		t.Fatalf("canonical overload = (%d, %v), want (429, true)", status, ok)
	}

	receipted := `{"error":{"code":429,"message":"rate limited"},"usage":{"cost":0}}`
	if _, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, receipted)},
		"openai/gpt-oss-20b",
		"deepinfra",
	); ok {
		t.Fatal("receipt-bearing 429 opened the provider circuit")
	}

	if _, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, body), {timedOut: true}},
		"openai/gpt-oss-20b",
		"deepinfra",
	); ok {
		t.Fatal("mixed backpressure/transport exhaustion opened the provider circuit")
	}
}
