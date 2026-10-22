package inference

import (
	"net/http"
	"strings"
	"testing"
)

// liveBackpressureEnvelope is the verbatim body OpenRouter returned for the
// judge-lane 429 that terminated LongMem confirmation bundle
// 05e1ebab-7b36-4424-8fc3-2dc23a524ae7 on 2026-10-21T21:49:19Z, with the
// account identifier redacted. Earlier classifiers were written against a
// hand-authored envelope and rejected this one on four independent gates
// (an added user_id sibling, an added error.metadata, four added metadata
// fields, and attempt 1 rather than 0), which silently disabled every
// receipt-free retry and left the provider circuit breaker unable to open.
//
// Keep this fixture byte-faithful to the wire. A synthetic envelope cannot
// detect the next gateway change; only a captured one can.
const liveBackpressureEnvelope = `{
  "error": {
    "code": 429,
    "message": "Provider returned error",
    "metadata": {
      "headers": {"Retry-After": "30"},
      "is_byok": false,
      "limit_source": "upstream_provider_shared_pool",
      "provider_error_code": "rate_limit_exceeded",
      "provider_name": "Azure",
      "raw": "openai/gpt-4o-2024-08-06 is temporarily rate-limited upstream. Please retry shortly, or add your own key to accumulate your rate limits: https://openrouter.ai/settings/integrations",
      "remedy_hint": "Retry shortly, add your own provider key (https://openrouter.ai/settings/integrations), or route to another provider with provider routing: https://openrouter.ai/docs/features/provider-routing",
      "retry_after_seconds": 30,
      "retry_after_seconds_raw": 30
    }
  },
  "openrouter_metadata": {
    "attempt": 1,
    "endpoints": {
      "available": [{"model": "openai/gpt-4o-2024-08-06", "provider": "Azure", "selected": false}],
      "total": 2
    },
    "generation_time": 114,
    "is_byok": false,
    "region": "ORD",
    "requested": "openai/gpt-4o-2024-08-06",
    "strategy": "direct",
    "summary": "available=1"
  },
  "user_id": "org_REDACTED"
}`

const liveBackpressureModel = "openai/gpt-4o-2024-08-06"

func liveBackpressureResult(body string) *providerHTTPResult {
	return &providerHTTPResult{
		status: http.StatusTooManyRequests,
		header: http.Header{"Retry-After": []string{"30"}},
		body:   []byte(body),
	}
}

func TestReceiptFreeAcceptsLiveGatewayBackpressureEnvelope(t *testing.T) {
	if !providerFailureIsReceiptFree(liveBackpressureResult(liveBackpressureEnvelope), liveBackpressureModel) {
		t.Fatal("live gateway 429 envelope was not classified receipt-free: " +
			"backpressure recovery and the provider circuit breaker are both inert")
	}
	if !providerBackpressureIsReceiptFree(liveBackpressureResult(liveBackpressureEnvelope), liveBackpressureModel) {
		t.Fatal("live gateway 429 envelope did not satisfy the backpressure wrapper")
	}
	if !receiptFreeResultOverload(liveBackpressureResult(liveBackpressureEnvelope), liveBackpressureModel) {
		t.Fatal("live gateway 429 envelope cannot open the provider circuit")
	}
}

// The gateway asking for 30s must not be truncated to the ordinary 5s hint:
// retrying inside a shared-pool window just spends the second attempt.
func TestLiveBackpressureDelayHonoursTheGatewayHint(t *testing.T) {
	got := confirmationBackpressureDelay(liveBackpressureResult(liveBackpressureEnvelope).header)
	if got.Seconds() != 30 {
		t.Fatalf("Retry-After 30 produced %s", got)
	}
}

// Widening the allowlists must not become a denylist: an unrecognized sibling
// of "error" may carry billing, so it still fails closed.
func TestReceiptFreeStillFailsClosedOnUnknownEnvelopeFields(t *testing.T) {
	cases := map[string]string{
		"unknown top level sibling": strings.Replace(liveBackpressureEnvelope,
			`"user_id": "org_REDACTED"`, `"settlement": {"amount_microusd": 0}`, 1),
		"unknown metadata field": strings.Replace(liveBackpressureEnvelope,
			`"region": "ORD",`, `"region": "ORD", "charged_microusd": 0,`, 1),
		"selected endpoint": strings.Replace(liveBackpressureEnvelope,
			`"selected": false`, `"selected": true`, 1),
		"second dispatch": strings.Replace(liveBackpressureEnvelope,
			`"attempt": 1,`, `"attempt": 2,`, 1),
		"receipt in error metadata": strings.Replace(liveBackpressureEnvelope,
			`"is_byok": false,`, `"is_byok": false, "usage": {"total_tokens": 7},`, 1),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if body == liveBackpressureEnvelope {
				t.Fatal("fixture substitution did not apply; this case proves nothing")
			}
			if providerFailureIsReceiptFree(liveBackpressureResult(body), liveBackpressureModel) {
				t.Fatal("ambiguous envelope was classified receipt-free")
			}
		})
	}
}
