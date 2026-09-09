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

func TestReceiptFreeGatewayFailureRequiresUnreceipted502Envelope(t *testing.T) {
	canonical := phaseTrace{
		status: http.StatusOK,
		body:   []byte(`{"error":{"code":502,"message":"bad gateway"}}`),
	}
	if !receiptFreeGatewayFailure([]phaseTrace{canonical}, "openai/gpt-oss-20b", "groq") {
		t.Fatal("canonical receipt-free 502 was not recoverable")
	}

	for name, phase := range map[string]phaseTrace{
		"http 400": {
			status: http.StatusBadRequest,
			body:   []byte(`{"error":{"code":400,"message":"bad request"}}`),
		},
		"receipt bearing": {
			status: http.StatusBadGateway,
			body:   []byte(`{"error":{"code":502,"message":"bad gateway"},"usage":{"cost":0}}`),
		},
		"ambiguous body": {status: http.StatusBadGateway, body: []byte(`bad gateway`)},
		"timeout": {
			status: http.StatusBadGateway, timedOut: true,
			body: []byte(`{"error":{"code":502,"message":"bad gateway"}}`),
		},
		"miner generation": {
			status: http.StatusOK, errorCode: providerGenerationInvalidCode,
			body: []byte(`{"error":{"code":502,"message":"Tool choice is none, but model called a tool"}}`),
		},
	} {
		t.Run(name, func(t *testing.T) {
			if receiptFreeGatewayFailure([]phaseTrace{phase}, "openai/gpt-oss-20b", "groq") {
				t.Fatal("unsafe gateway response authorized a relay retry")
			}
		})
	}
}
