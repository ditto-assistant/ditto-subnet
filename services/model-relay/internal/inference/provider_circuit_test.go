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

// The exact envelope OpenRouter returned for every failed Wonder v6 retest
// call on 2026-10-31 (03:59-04:09Z, 4,121 calls, zero tokens).
const gatewayCreditBody = `{"error":{"message":"Insufficient credits. Add more using https://openrouter.ai/settings/credits","code":402,"metadata":{"limit_source":"openrouter_credits","remedy_hint":"Add credits at https://openrouter.ai/settings/credits, or lower max_tokens / prompt size to fit your remaining balance."}}}`

func creditPhase(route, body string) phaseTrace {
	return phaseTrace{route: route, status: http.StatusPaymentRequired, body: []byte(body)}
}

func TestGatewayCreditExhaustionOpensProviderCircuit(t *testing.T) {
	status, ok := gatewayCreditExhausted([]phaseTrace{creditPhase("openrouter", gatewayCreditBody)})
	if !ok || status != http.StatusPaymentRequired {
		t.Fatalf("observed credit refusal = (%d, %v), want (402, true)", status, ok)
	}
	messageOnly := `{"error":{"message":"Insufficient credits. Add more.","code":402}}`
	if _, ok := gatewayCreditExhausted([]phaseTrace{creditPhase("openrouter", messageOnly)}); !ok {
		t.Fatal("message-only credit refusal did not open the circuit")
	}
}

func TestGatewayCreditExhaustionRejectsOtherRefusals(t *testing.T) {
	cases := map[string][]phaseTrace{
		"no phases":           nil,
		"ditto router wallet": {creditPhase(dittoRouterRoute, gatewayCreditBody)},
		"wrong status":        {{route: "openrouter", status: 403, body: []byte(gatewayCreditBody)}},
		"code mismatch":       {creditPhase("openrouter", `{"error":{"message":"Insufficient credits","code":403}}`)},
		"other 402":           {creditPhase("openrouter", `{"error":{"message":"payment required","code":402}}`)},
		"receipt beside error": {creditPhase("openrouter",
			`{"error":{"message":"Insufficient credits","code":402},"usage":{"prompt_tokens":3}}`)},
		"completion beside error": {creditPhase("openrouter",
			`{"error":{"message":"Insufficient credits","code":402},"choices":[]}`)},
		"not json":             {creditPhase("openrouter", `Insufficient credits`)},
		"mixed with transport": {creditPhase("openrouter", gatewayCreditBody), {route: "openrouter", timedOut: true}},
	}
	for name, phases := range cases {
		if _, ok := gatewayCreditExhausted(phases); ok {
			t.Errorf("%s opened the provider circuit", name)
		}
	}
}

func TestReceiptFreeOverloadRequiresEveryPhaseToBeCanonicalBackpressure(t *testing.T) {
	body := receiptFreeEnvelope(429, "openai/gpt-oss-20b")
	status, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, body), overloadPhase(429, body)},
		"openai/gpt-oss-20b",
	)
	if !ok || status != 429 {
		t.Fatalf("canonical overload = (%d, %v), want (429, true)", status, ok)
	}

	receipted := `{"error":{"code":429,"message":"rate limited"},"usage":{"cost":0}}`
	if _, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, receipted)},
		"openai/gpt-oss-20b",
	); ok {
		t.Fatal("receipt-bearing 429 opened the provider circuit")
	}

	if _, ok := receiptFreeOverload(
		[]phaseTrace{overloadPhase(429, body), {timedOut: true}},
		"openai/gpt-oss-20b",
	); ok {
		t.Fatal("mixed backpressure/transport exhaustion opened the provider circuit")
	}
}
