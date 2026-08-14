package inference

import (
	"encoding/json"
	"strings"
	"testing"
)

func decodeProviderBody(t *testing.T, body string) map[string]any {
	t.Helper()
	decoded, ok := decodeJSONNumbers([]byte(body))
	if !ok {
		t.Fatalf("test body is not one JSON document")
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		t.Fatalf("test body is not a JSON object")
	}
	return payload
}

const providerBodyEmptyToolCalls = `{
	"id":"gen-1","object":"chat.completion","created":1755000000,
	"model":"openai/gpt-oss-20b",
	"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
		"message":{"role":"assistant","content":"hi","tool_calls":[]}}],
	"usage":{"prompt_tokens":3,"completion_tokens":2}
}`

// Python's _public_provider_response emits "tool_calls": [] when the provider
// sent an empty (non-null) list, and omits the key only when the provider
// omitted it. A harness that branches on key presence must observe the same
// shape across the Python->Go cutover.
func TestPublicProviderResponseKeepsEmptyToolCallsArray(t *testing.T) {
	payload := decodeProviderBody(t, providerBodyEmptyToolCalls)
	public, herr := publicProviderResponse(payload, []byte(providerBodyEmptyToolCalls))
	if herr != nil {
		t.Fatalf("unexpected error: %v", herr.message)
	}
	raw, err := compactJSON(public)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	if !strings.Contains(string(raw), `"tool_calls":[]`) {
		t.Fatalf("empty tool_calls array must survive sanitization: %s", raw)
	}

	// And the key stays ABSENT when the provider omitted it.
	withoutCalls := strings.Replace(providerBodyEmptyToolCalls, `,"tool_calls":[]`, "", 1)
	payload = decodeProviderBody(t, withoutCalls)
	public, herr = publicProviderResponse(payload, []byte(withoutCalls))
	if herr != nil {
		t.Fatalf("unexpected error: %v", herr.message)
	}
	raw, err = compactJSON(public)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	if strings.Contains(string(raw), "tool_calls") {
		t.Fatalf("absent tool_calls must not materialize a key: %s", raw)
	}
}

// Python rejects usage with prompt+completion > 2^62 using arbitrary-precision
// ints; the Go check must not wrap int64 into accepting it.
func TestBoundedUsageSumOverflow(t *testing.T) {
	cases := map[string]struct {
		usage string
		ok    bool
	}{
		"exactly the bound": {`{"prompt_tokens":4611686018427387904,"completion_tokens":0}`, true},
		"one over":          {`{"prompt_tokens":4611686018427387904,"completion_tokens":1}`, false},
		"int64 wraparound":  {`{"prompt_tokens":4611686018427387904,"completion_tokens":4611686018427387904}`, false},
		"max int64 each":    {`{"prompt_tokens":9223372036854775807,"completion_tokens":9223372036854775807}`, false},
		"ordinary":          {`{"prompt_tokens":12,"completion_tokens":5}`, true},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			var usage map[string]any
			dec := json.NewDecoder(strings.NewReader(tc.usage))
			dec.UseNumber()
			if err := dec.Decode(&usage); err != nil {
				t.Fatalf("decode: %v", err)
			}
			_, _, ok := boundedUsage(map[string]any{"usage": usage})
			if ok != tc.ok {
				t.Fatalf("boundedUsage(%s): want ok=%v, got %v", tc.usage, tc.ok, ok)
			}
		})
	}
}
