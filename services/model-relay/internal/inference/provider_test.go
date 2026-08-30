package inference

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestConfirmationReaderBackpressureDelay(t *testing.T) {
	tests := map[string]struct {
		retryAfter string
		want       time.Duration
	}{
		"missing defaults to ten seconds":    {want: 10 * time.Second},
		"invalid defaults to ten seconds":    {retryAfter: "later", want: 10 * time.Second},
		"plus sign defaults to ten seconds":  {retryAfter: "+1", want: 10 * time.Second},
		"underscore defaults to ten seconds": {retryAfter: "6_0", want: 10 * time.Second},
		"full width defaults to ten seconds": {retryAfter: "６０", want: 10 * time.Second},
		"zero defaults to ten seconds":       {retryAfter: "0", want: 10 * time.Second},
		"negative defaults to ten seconds":   {retryAfter: "-3", want: 10 * time.Second},
		"one second is honored":              {retryAfter: "1", want: time.Second},
		"sixty seconds is honored":           {retryAfter: "60", want: 60 * time.Second},
		"over sixty is clamped":              {retryAfter: "120", want: 60 * time.Second},
		"huge integer is clamped":            {retryAfter: "9223372036854775808", want: 60 * time.Second},
	}
	for name, tc := range tests {
		t.Run(name, func(t *testing.T) {
			header := make(http.Header)
			if tc.retryAfter != "" {
				header.Set("Retry-After", tc.retryAfter)
			}
			if got := confirmationReaderBackpressureDelay(header); got != tc.want {
				t.Fatalf("delay: got %s want %s", got, tc.want)
			}
		})
	}
	multiple := make(http.Header)
	multiple.Add("Retry-After", "1")
	multiple.Add("Retry-After", "60")
	if got := confirmationReaderBackpressureDelay(multiple); got != 10*time.Second {
		t.Fatalf("duplicate Retry-After: got %s want 10s", got)
	}
}

func TestConfirmationReaderBackpressureDoesNotWaitForRetryDeadline(t *testing.T) {
	var calls int
	var slept time.Duration
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		calls++
		return &http.Response{
			StatusCode: http.StatusTooManyRequests,
			Header:     http.Header{"Retry-After": []string{"60"}},
			Body: io.NopCloser(strings.NewReader(
				`{"error":{"code":429,"message":"rate limited"}}`,
			)),
			Request: request,
		}, nil
	})}
	const elapsedBudget = 30 * time.Second

	result, callErr := postProviderWithRetryPolicy(
		context.Background(),
		client,
		"https://provider.invalid/v1/chat/completions",
		map[string]any{"model": "fixed/model"},
		nil,
		1024,
		5,
		providerRetryPolicy{
			retryBackpressure:              true,
			backpressureMaxAttempts:        confirmationReaderBackpressureMaxAttempts,
			requireReceiptFreeBackpressure: true,
			maxElapsed:                     elapsedBudget,
		},
		func(_ context.Context, delay time.Duration) { slept = delay },
	)
	if result == nil || result.status != http.StatusTooManyRequests || callErr != nil {
		t.Fatalf("deadline result=%v error=%v", result, callErr)
	}
	if calls != 1 {
		t.Fatalf("deadline attempts: got %d want 1", calls)
	}
	if slept != 0 {
		t.Fatalf("single-shot provider call slept for %s", slept)
	}
}

func TestConfirmationReader503KeepsOrdinaryAttemptCap(t *testing.T) {
	var calls int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"error":{"code":503,"message":"capacity"}}`))
	}))
	defer upstream.Close()

	result, callErr := postProviderWithRetryPolicy(
		context.Background(), upstream.Client(), upstream.URL,
		map[string]any{"model": "fixed/model"}, nil, 1024, 5,
		providerRetryPolicy{
			retryBackpressure:              true,
			backpressureMaxAttempts:        confirmationReaderBackpressureMaxAttempts,
			requireReceiptFreeBackpressure: true,
			maxElapsed:                     confirmationReaderBackpressureMaxElapsed,
		},
		func(context.Context, time.Duration) {},
	)
	if callErr != nil || result == nil || result.status != http.StatusServiceUnavailable || result.attempts != providerMaxAttempts {
		t.Fatalf("503 result=%v error=%v", result, callErr)
	}
	if calls != providerMaxAttempts {
		t.Fatalf("503 attempts: got %d want %d", calls, providerMaxAttempts)
	}
}

func TestConfirmationReaderBackpressureDoesNotScheduleCancellationWait(t *testing.T) {
	var calls int
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":{"code":429,"message":"capacity"}}`))
	}))
	defer upstream.Close()
	ctx, cancel := context.WithCancel(context.Background())

	result, callErr := postProviderWithRetryPolicy(
		ctx, upstream.Client(), upstream.URL,
		map[string]any{"model": "fixed/model"}, nil, 1024, 5,
		providerRetryPolicy{
			retryBackpressure:              true,
			backpressureMaxAttempts:        confirmationReaderBackpressureMaxAttempts,
			requireReceiptFreeBackpressure: true,
			maxElapsed:                     confirmationReaderBackpressureMaxElapsed,
		},
		func(context.Context, time.Duration) { cancel() },
	)
	if result == nil || result.status != http.StatusTooManyRequests || callErr != nil {
		t.Fatalf("cancel result=%v error=%v", result, callErr)
	}
	if calls != 1 {
		t.Fatalf("cancel attempts: got %d want 1", calls)
	}
}

func TestProviderBackpressureReceiptProofFailsClosed(t *testing.T) {
	canonicalMetadata := `"openrouter_metadata":{
		"requested":"fixed/model","strategy":"direct","attempt":0,
		"endpoints":{"total":1,"available":[{
			"provider":"DeepInfra","model":"fixed/model","selected":false
		}]}
	}`
	tests := map[string]struct {
		body string
		want bool
	}{
		"string error is not canonical":  {body: `{"error":"rate limited"}`},
		"canonical object error":         {body: `{"error":{"code":429,"message":"rate limited"}}`, want: true},
		"canonical route metadata":       {body: `{"error":{"code":429,"message":"rate limited"},` + canonicalMetadata + `}`, want: true},
		"missing error":                  {body: `{}`},
		"malformed":                      {body: `{"error":`},
		"non-finite number":              {body: `{"error":{"code":429,"message":"rate limited"},"value":NaN}`},
		"positive infinity":              {body: `{"error":{"code":429,"message":"rate limited"},"value":Infinity}`},
		"negative infinity":              {body: `{"error":{"code":429,"message":"rate limited"},"value":-Infinity}`},
		"duplicate error":                {body: `{"error":"first","error":"second"}`},
		"missing code":                   {body: `{"error":{"message":"rate limited"}}`},
		"mismatched code":                {body: `{"error":{"code":503,"message":"rate limited"}}`},
		"missing message":                {body: `{"error":{"code":429}}`},
		"empty message":                  {body: `{"error":{"code":429,"message":""}}`},
		"unknown error field":            {body: `{"error":{"code":429,"message":"rate limited","provider_name":"DeepInfra"}}`},
		"unknown billing object":         {body: `{"error":{"code":429,"message":"rate limited"},"billing":{"amount_microusd":0}}`},
		"unknown response id":            {body: `{"error":{"code":429,"message":"rate limited"},"response_id":"r"}`},
		"unknown token usage":            {body: `{"error":{"code":429,"message":"rate limited"},"token_usage":{}}`},
		"top level usage":                {body: `{"error":{"code":429,"message":"rate limited"},"usage":{}}`},
		"nested usage":                   {body: `{"error":{"code":429,"message":"rate limited","metadata":{"usage":{}}}}`},
		"cost":                           {body: `{"error":{"code":429,"message":"rate limited"},"cost":0}`},
		"generation":                     {body: `{"error":{"code":429,"message":"rate limited"},"generation":"gen-1"}`},
		"generation id":                  {body: `{"error":{"code":429,"message":"rate limited"},"generation_id":"gen-1"}`},
		"provider":                       {body: `{"error":{"code":429,"message":"rate limited"},"provider":"DeepInfra"}`},
		"choices":                        {body: `{"error":{"code":429,"message":"rate limited"},"choices":[]}`},
		"receipt":                        {body: `{"error":{"code":429,"message":"rate limited"},"receipt":{"id":"r"}}`},
		"receipt id":                     {body: `{"error":{"code":429,"message":"rate limited"},"receipt_id":"r"}`},
		"billed flag":                    {body: `{"error":{"code":429,"message":"rate limited"},"billed":false}`},
		"nested charged flag":            {body: `{"error":{"code":429,"message":"rate limited","metadata":{"charged":false}}}`},
		"nested provider":                {body: `{"error":{"code":429,"message":"rate limited","metadata":{"provider":"DeepInfra"}}}`},
		"nested cost in list":            {body: `{"error":{"code":429,"message":"rate limited","metadata":[{"cost":0}]}}`},
		"nested receipt":                 {body: `{"error":{"code":429,"message":"rate limited","metadata":{"receipt":"r"}}}`},
		"nested choices in list":         {body: `{"error":{"code":429,"message":"rate limited","metadata":[{"choices":[]}]}}`},
		"metadata attempt one":           {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"attempt":0`, `"attempt":1`, 1) + `}`},
		"metadata negative zero attempt": {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"attempt":0`, `"attempt":-0`, 1) + `}`},
		"metadata attempts present":      {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"attempt":0,`, `"attempt":0,"attempts":[],`, 1) + `}`},
		"metadata endpoint selected":     {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"selected":false`, `"selected":true`, 1) + `}`},
		"metadata requested mismatch":    {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"requested":"fixed/model"`, `"requested":"other/model"`, 1) + `}`},
		"metadata model mismatch":        {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"model":"fixed/model"`, `"model":"other/model"`, 1) + `}`},
		"metadata provider mismatch":     {body: `{"error":{"code":429,"message":"rate limited"},` + strings.Replace(canonicalMetadata, `"provider":"DeepInfra"`, `"provider":"Azure"`, 1) + `}`},
		"metadata null":                  {body: `{"error":{"code":429,"message":"rate limited"},"openrouter_metadata":null}`},
	}
	for name, tc := range tests {
		t.Run(name, func(t *testing.T) {
			got := providerBackpressureIsReceiptFree(&providerHTTPResult{
				status: http.StatusTooManyRequests,
				body:   []byte(tc.body),
			}, "fixed/model", "deepinfra")
			if got != tc.want {
				t.Fatalf("receipt-free classification: got %v want %v", got, tc.want)
			}
		})
	}
	if providerBackpressureIsReceiptFree(&providerHTTPResult{
		status: http.StatusInternalServerError,
		body:   []byte(`{"error":"boom"}`),
	}, "fixed/model", "deepinfra") {
		t.Fatal("non-429/503 response was classified as receipt-free backpressure")
	}
}

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

const structuredOutputFailureBody = `{"error":{"message":"Upstream error from Groq: Failed to validate JSON. Please adjust your prompt. See 'failed_generation' for more details.","code":502}}`
const unexpectedToolFailureBody = `{"error":{"message":"Upstream error from Groq: Tool choice is none, but model called a tool","code":502}}`
const undeclaredToolFailureBody = `{"error":{"message":"Upstream error from Groq: Tool call validation failed: tool call validation failed: attempted to call tool 'calendar_search_events' which was not in request.tools","code":502}}`
const invalidToolParametersFailureBody = "{\"error\":{\"message\":\"Upstream error from Groq: Tool call validation failed: tool call validation failed: parameters for tool create_workflow did not match schema: errors: [`/schedule`: expected string, but got null]\",\"code\":502}}"
const validAggregateChatBody = `{
	"id":"gen-1","object":"chat.completion","created":1755000000,
	"model":"openai/gpt-oss-20b","provider":"groq",
	"choices":[{"index":0,"finish_reason":"stop","logprobs":null,
		"message":{"role":"assistant","content":"hi"}}],
	"usage":{"prompt_tokens":3,"completion_tokens":2,"cost":0.0001}
}`

func aggregateChatConfig(upstreamURL string) config.InferenceProxyConfig {
	return config.InferenceProxyConfig{
		OpenRouterAPIKey:  "test-key",
		UpstreamURL:       upstreamURL,
		RoutingMode:       config.RoutingModeAggregateThroughput,
		ResponseBodyBytes: 1 << 20,
		TimeoutSeconds:    5,
	}
}

func TestProviderErrorEnvelopePrecedesIdentityValidation(t *testing.T) {
	var calls int
	var slept time.Duration
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		_, _ = w.Write([]byte(structuredOutputFailureBody))
	}))
	defer upstream.Close()

	payload := map[string]any{
		"model": "openai/gpt-oss-20b",
		"messages": []any{
			map[string]any{"role": "user", "content": "answer exactly"},
		},
		"response_format": map[string]any{
			"type": "json_schema",
			"json_schema": map[string]any{
				"name":   "answer",
				"schema": map[string]any{"type": "object"},
				"strict": true,
			},
		},
	}
	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL), payload,
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(_ context.Context, delay time.Duration) { slept += delay },
	)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if calls != 1 || exhausted.upstreamAttempts != 1 || exhausted.fallbackPhase != 0 {
		t.Fatalf("calls/attempts/phase=%d/%d/%d", calls, exhausted.upstreamAttempts, exhausted.fallbackPhase)
	}
	if slept != 0 {
		t.Fatalf("miner-owned generation error slept for %s", slept)
	}
	if exhausted.terminalErrorCode != providerGenerationInvalidCode {
		t.Fatalf("terminal code=%q want %s", exhausted.terminalErrorCode, providerGenerationInvalidCode)
	}
	if len(exhausted.phases) != 1 || exhausted.phases[0].errorCode != providerGenerationInvalidCode {
		t.Fatalf("phase trace=%+v", exhausted.phases)
	}
}

func TestRecoverableGenerationErrorRemainsMinerOwnedAtHTTP502(t *testing.T) {
	var calls int
	var slept time.Duration
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(unexpectedToolFailureBody))
	}))
	defer upstream.Close()

	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL),
		map[string]any{
			"model":    "openai/gpt-oss-20b",
			"messages": []any{map[string]any{"role": "user", "content": "hello"}},
		},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(_ context.Context, delay time.Duration) { slept += delay },
	)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if calls != 1 || exhausted.upstreamAttempts != 1 || slept != 0 {
		t.Fatalf("calls/attempts/sleep=%d/%d/%s", calls, exhausted.upstreamAttempts, slept)
	}
	if exhausted.terminalErrorCode != providerGenerationInvalidCode {
		t.Fatalf("terminal code=%q want %s", exhausted.terminalErrorCode, providerGenerationInvalidCode)
	}
	if got := chatProviderFailure(exhausted).headers[minerRecoverableFailureHeader]; got != minerRecoverableGeneration {
		t.Fatalf("failure class=%q want %q", got, minerRecoverableGeneration)
	}
}

func TestReceiptFree429RetriesOnceAndRecovers(t *testing.T) {
	var calls int
	var slept time.Duration
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls == 1 {
			w.Header().Set("Retry-After", "2")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":{"code":429,"message":"rate limited"}}`))
			return
		}
		_, _ = w.Write([]byte(validAggregateChatBody))
	}))
	defer upstream.Close()

	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL),
		map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(_ context.Context, delay time.Duration) { slept += delay },
	)
	if completed == nil || exhausted != nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if calls != providerSafeRetryMaxAttempts || completed.upstreamAttempts != providerSafeRetryMaxAttempts || slept != 2*time.Second {
		t.Fatalf("calls/attempts/sleep=%d/%d/%s", calls, completed.upstreamAttempts, slept)
	}
	if len(completed.phases) != providerSafeRetryMaxAttempts ||
		completed.phases[0].errorCode != "upstream_http_429" || completed.phases[1].errorCode != "" {
		t.Fatalf("phase trace=%+v", completed.phases)
	}
}

func TestReceiptFreeHTTP502RetriesOnceAndRecovers(t *testing.T) {
	var calls int
	var slept time.Duration
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte(`{"error":{"code":502,"message":"bad gateway"}}`))
			return
		}
		_, _ = w.Write([]byte(validAggregateChatBody))
	}))
	defer upstream.Close()

	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL),
		map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(_ context.Context, delay time.Duration) { slept += delay },
	)
	if completed == nil || exhausted != nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if calls != providerSafeRetryMaxAttempts || completed.upstreamAttempts != providerSafeRetryMaxAttempts || slept != providerGatewayRetryDelay {
		t.Fatalf("calls/attempts/sleep=%d/%d/%s", calls, completed.upstreamAttempts, slept)
	}
}

func TestReceiptBearing429And502RemainSingleShot(t *testing.T) {
	for _, status := range []int{http.StatusTooManyRequests, http.StatusBadGateway} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			var calls int
			var slept time.Duration
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls++
				w.WriteHeader(status)
				_, _ = w.Write([]byte(`{"error":{"code":` + strconv.Itoa(status) + `,"message":"failed"},"usage":{"cost":0}}`))
			}))
			defer upstream.Close()

			completed, exhausted := completeChatWithRecovery(
				context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL),
				map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
				"openai/gpt-oss-20b", "openrouter", "", nil, nil,
				func(_ context.Context, delay time.Duration) { slept += delay },
			)
			if completed != nil || exhausted == nil {
				t.Fatalf("result=%v exhausted=%v", completed, exhausted)
			}
			if calls != 1 || exhausted.upstreamAttempts != 1 || slept != 0 {
				t.Fatalf("calls/attempts/sleep=%d/%d/%s", calls, exhausted.upstreamAttempts, slept)
			}
		})
	}
}

func TestHTTP400ProviderRejectionRemainsSingleShot(t *testing.T) {
	var calls int
	var slept time.Duration
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":{"code":400,"message":"invalid request"}}`))
	}))
	defer upstream.Close()

	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL),
		map[string]any{"model": "openai/gpt-oss-20b", "messages": []any{}},
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(_ context.Context, delay time.Duration) { slept += delay },
	)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if calls != 1 || exhausted.upstreamAttempts != 1 || slept != 0 {
		t.Fatalf("calls/attempts/sleep=%d/%d/%s", calls, exhausted.upstreamAttempts, slept)
	}
	if exhausted.terminalErrorCode != "upstream_http_400" {
		t.Fatalf("terminal code=%q", exhausted.terminalErrorCode)
	}
}

func TestStructuredOutputProviderErrorRequiresStrictSchema(t *testing.T) {
	var response map[string]any
	decoder := json.NewDecoder(strings.NewReader(structuredOutputFailureBody))
	decoder.UseNumber()
	if err := decoder.Decode(&response); err != nil {
		t.Fatal(err)
	}
	for name, request := range map[string]map[string]any{
		"missing response format": {},
		"non-schema format":       {"response_format": map[string]any{"type": "json_object"}},
		"non-strict schema": {"response_format": map[string]any{
			"type": "json_schema", "json_schema": map[string]any{"strict": false},
		}},
	} {
		t.Run(name, func(t *testing.T) {
			if minerRecoverableProviderError(request, response) {
				t.Fatal("classified a non-strict request as miner-recoverable")
			}
		})
	}
}

func TestUnexpectedToolProviderErrorRequiresToolChoiceNone(t *testing.T) {
	var response map[string]any
	decoder := json.NewDecoder(strings.NewReader(unexpectedToolFailureBody))
	decoder.UseNumber()
	if err := decoder.Decode(&response); err != nil {
		t.Fatal(err)
	}
	if !minerRecoverableProviderError(map[string]any{"messages": []any{}}, response) {
		t.Fatal("exact no-tools generation failure was not miner-recoverable")
	}
	if !minerRecoverableProviderError(map[string]any{"tool_choice": nil}, response) {
		t.Fatal("null tool choice was not treated as omitted")
	}
	for name, request := range map[string]map[string]any{
		"required tool":  {"tool_choice": "required"},
		"declared tools": {"tools": []any{map[string]any{"type": "function"}}},
	} {
		t.Run(name, func(t *testing.T) {
			if minerRecoverableProviderError(request, response) {
				t.Fatal("tool-enabled request was marked miner-recoverable")
			}
		})
	}
}

func TestUndeclaredToolProviderErrorRequiresToolAbsentFromRequest(t *testing.T) {
	var response map[string]any
	decoder := json.NewDecoder(strings.NewReader(undeclaredToolFailureBody))
	decoder.UseNumber()
	if err := decoder.Decode(&response); err != nil {
		t.Fatal(err)
	}
	otherTool := map[string]any{"type": "function", "function": map[string]any{"name": "gmail_send"}}
	if !minerRecoverableProviderError(map[string]any{"tools": []any{otherTool}}, response) {
		t.Fatal("hallucinated undeclared tool was not miner-recoverable")
	}
	declaredTool := map[string]any{"type": "function", "function": map[string]any{"name": "calendar_search_events"}}
	if minerRecoverableProviderError(map[string]any{"tools": []any{declaredTool}}, response) {
		t.Fatal("provider rejection of a declared tool was marked miner-recoverable")
	}
}

func TestInvalidToolParametersProviderErrorRequiresDeclaredTool(t *testing.T) {
	var response map[string]any
	decoder := json.NewDecoder(strings.NewReader(invalidToolParametersFailureBody))
	decoder.UseNumber()
	if err := decoder.Decode(&response); err != nil {
		t.Fatal(err)
	}
	declaredTool := map[string]any{"type": "function", "function": map[string]any{"name": "create_workflow"}}
	if !minerRecoverableProviderError(map[string]any{"tools": []any{declaredTool}}, response) {
		t.Fatal("generated arguments for a declared tool were not miner-recoverable")
	}
	if minerRecoverableProviderError(map[string]any{"tools": []any{}}, response) {
		t.Fatal("parameter failure for an undeclared tool was marked miner-recoverable")
	}
}

func TestInvalidToolParametersFlowToRecoverableFailureClass(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(invalidToolParametersFailureBody))
	}))
	defer upstream.Close()

	declaredTool := map[string]any{"type": "function", "function": map[string]any{"name": "create_workflow"}}
	payload := map[string]any{
		"model":    "openai/gpt-oss-20b",
		"messages": []any{map[string]any{"role": "user", "content": "create it"}},
		"tools":    []any{declaredTool},
	}
	completed, exhausted := completeChatWithRecovery(
		context.Background(), upstream.Client(), aggregateChatConfig(upstream.URL), payload,
		"openai/gpt-oss-20b", "openrouter", "", nil, nil,
		func(context.Context, time.Duration) {},
	)
	if completed != nil || exhausted == nil {
		t.Fatalf("result=%v exhausted=%v", completed, exhausted)
	}
	if exhausted.terminalErrorCode != providerGenerationInvalidCode {
		t.Fatalf("terminal code=%q want %s", exhausted.terminalErrorCode, providerGenerationInvalidCode)
	}
	failure := chatProviderFailure(exhausted)
	if got := failure.headers[minerRecoverableFailureHeader]; got != minerRecoverableGeneration {
		t.Fatalf("failure class=%q want %q", got, minerRecoverableGeneration)
	}
}

func TestInvalidToolParametersProviderErrorKeepsOtherFailuresClosed(t *testing.T) {
	declaredTool := map[string]any{"type": "function", "function": map[string]any{"name": "create_workflow"}}
	request := map[string]any{"tools": []any{declaredTool}}
	for name, body := range map[string]string{
		"schema definition rejection": `{"error":{"message":"Upstream error from Groq: invalid JSON schema for tool create_workflow: 'required' present but 'properties' is missing","code":502}}`,
		"generic parameter text":      `{"error":{"message":"parameters for tool create_workflow did not match schema: errors: bad value","code":502}}`,
		"missing error detail":        `{"error":{"message":"Upstream error from Groq: Tool call validation failed: tool call validation failed: parameters for tool create_workflow did not match schema: errors:","code":502}}`,
		"non-502 envelope":            `{"error":{"message":"Upstream error from Groq: Tool call validation failed: tool call validation failed: parameters for tool create_workflow did not match schema: errors: bad value","code":400}}`,
	} {
		t.Run(name, func(t *testing.T) {
			var response map[string]any
			decoder := json.NewDecoder(strings.NewReader(body))
			decoder.UseNumber()
			if err := decoder.Decode(&response); err != nil {
				t.Fatal(err)
			}
			if minerRecoverableProviderError(request, response) {
				t.Fatal("non-generation failure was marked miner-recoverable")
			}
		})
	}
}

func TestChatProviderFailureMarksOnlyRecoverableGenerationErrors(t *testing.T) {
	recoverable := chatProviderFailure(&chatProviderExhausted{terminalErrorCode: providerGenerationInvalidCode})
	if recoverable.status != http.StatusBadGateway || recoverable.message != "inference provider unavailable" {
		t.Fatalf("public failure changed: %+v", recoverable)
	}
	if got := recoverable.headers[minerRecoverableFailureHeader]; got != minerRecoverableGeneration {
		t.Fatalf("failure class=%q want %q", got, minerRecoverableGeneration)
	}

	ordinary := chatProviderFailure(&chatProviderExhausted{terminalErrorCode: "provider_unavailable"})
	if len(ordinary.headers) != 0 {
		t.Fatalf("ordinary provider failure was marked recoverable: %+v", ordinary.headers)
	}
	for _, terminalCode := range []string{"upstream_http_401", "upstream_http_429", "provider_transport", "provider_timeout"} {
		failure := chatProviderFailure(&chatProviderExhausted{terminalErrorCode: terminalCode})
		if len(failure.headers) != 0 {
			t.Fatalf("%s exposed private classification: %+v", terminalCode, failure.headers)
		}
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
