package inference

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

const (
	providerMaxAttempts          = 3
	providerRetryAfterMaxSeconds = 5

	pplxEmbedContractModel = "perplexity/pplx-embed-v1-0.6b"
	pplxEmbedResponseModel = "pplx-embed-v1-0.6b"
)

func isProviderRetryStatus(status int) bool {
	switch status {
	case 408, 429, 500, 502, 503, 504:
		return true
	}
	return false
}

// sleepFunc is injectable for tests; the default respects context
// cancellation.
type sleepFunc func(ctx context.Context, d time.Duration)

func defaultSleep(ctx context.Context, d time.Duration) {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

// providerHTTPResult is the buffered outcome of one logical provider request
// (the httpx.Response analog: the entire body is read before returning).
type providerHTTPResult struct {
	status        int
	header        http.Header
	body          []byte
	bodyOverLimit bool
	attempts      int
}

// providerCallError mirrors _ProviderCallError.
type providerCallError struct {
	attempts int
	timedOut bool
}

func (e *providerCallError) Error() string { return "provider request failed" }

// providerRetryAfterSeconds mirrors _provider_retry_after_seconds: a bounded
// backoff hint, clamp(int(Retry-After), 1, 5), non-integer -> 1.
func providerRetryAfterSeconds(header http.Header) int {
	raw := ""
	if header != nil {
		raw = header.Get("Retry-After")
	}
	seconds, err := strconv.Atoi(trimSpace(raw))
	if err != nil {
		return 1
	}
	if seconds < 1 {
		return 1
	}
	if seconds > providerRetryAfterMaxSeconds {
		return providerRetryAfterMaxSeconds
	}
	return seconds
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t') {
		end--
	}
	return s[start:end]
}

// providerIsBackpressure mirrors _provider_is_backpressure: 429, or 503 with
// a non-empty Retry-After.
func providerIsBackpressure(status int, header http.Header) bool {
	if status == 429 {
		return true
	}
	if status != 503 || header == nil {
		return false
	}
	return trimSpace(header.Get("Retry-After")) != ""
}

// classifyTransportError buckets a Do/read error into the httpx taxonomy:
// dial (ConnectError/ConnectTimeout — retryable) vs non-dial timeout
// (fail immediately, timed_out) vs other transport failure.
func classifyTransportError(err error) (dial bool, timeout bool) {
	var opErr *net.OpError
	if errors.As(err, &opErr) && opErr.Op == "dial" {
		return true, opErr.Timeout()
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return false, true
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return false, true
	}
	return false, false
}

// postProviderWithRetry mirrors _post_provider_with_retry: one logical
// provider request under the shared bounded retry policy. Only connection
// establishment failures and explicit transient HTTP statuses are repeated;
// a read failure is ambiguous (the provider may have billed the request) and
// fails closed. Each attempt gets its own timeout of timeoutSeconds. The
// response body is buffered up to maxBody+1 bytes.
func postProviderWithRetry(ctx context.Context, client *http.Client, url string, payload any,
	headers map[string]string, maxBody int64, timeoutSeconds int, retryBackpressure bool, sleep sleepFunc) (*providerHTTPResult, *providerCallError) {

	encoded, err := json.Marshal(payload)
	if err != nil {
		return nil, &providerCallError{attempts: 1, timedOut: false}
	}
	var lastDialTimeout bool
	for attempt := 1; attempt <= providerMaxAttempts; attempt++ {
		result, derr := postOnce(ctx, client, url, encoded, headers, maxBody, timeoutSeconds)
		if derr != nil {
			dial, timedOut := classifyTransportError(derr)
			if dial {
				lastDialTimeout = timedOut
				if attempt == providerMaxAttempts {
					return nil, &providerCallError{attempts: attempt, timedOut: lastDialTimeout}
				}
				sleep(ctx, backoffDelay(attempt))
				continue
			}
			return nil, &providerCallError{attempts: attempt, timedOut: timedOut}
		}
		if isProviderRetryStatus(result.status) && attempt < providerMaxAttempts &&
			(retryBackpressure || !providerIsBackpressure(result.status, result.header)) {
			var delay time.Duration
			if providerIsBackpressure(result.status, result.header) {
				delay = time.Duration(providerRetryAfterSeconds(result.header)) * time.Second
			} else {
				delay = backoffDelay(attempt)
			}
			sleep(ctx, delay)
			continue
		}
		result.attempts = attempt
		return result, nil
	}
	// Unreachable: the loop always returns.
	return nil, &providerCallError{attempts: providerMaxAttempts, timedOut: false}
}

func backoffDelay(attempt int) time.Duration {
	return time.Duration(float64(250*time.Millisecond) * math.Pow(2, float64(attempt-1)))
}

// postOnce performs one POST, buffering the response body (like httpx does
// inside client.post). A body-read failure is returned as a transport error,
// exactly as httpx surfaces it from the same call.
func postOnce(ctx context.Context, client *http.Client, url string, body []byte,
	headers map[string]string, maxBody int64, timeoutSeconds int) (*providerHTTPResult, error) {

	attemptCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSeconds)*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	buffered, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if err != nil {
		return nil, err
	}
	return &providerHTTPResult{
		status:        resp.StatusCode,
		header:        resp.Header,
		body:          buffered,
		bodyOverLimit: int64(len(buffered)) > maxBody,
	}, nil
}

// openrouterHeaders mirrors _openrouter_headers. No inbound client headers
// are ever forwarded upstream.
func openrouterHeaders(apiKey string, includeMetadata bool) map[string]string {
	headers := map[string]string{
		"Authorization":      "Bearer " + apiKey,
		"Content-Type":       "application/json",
		"HTTP-Referer":       "https://heyditto.ai/",
		"X-OpenRouter-Title": "Ditto",
	}
	if includeMetadata {
		headers["X-OpenRouter-Metadata"] = "enabled"
	}
	return headers
}

// providerPreferences mirrors _provider_preferences.
func providerPreferences(routingMode, provider string, quantization string) map[string]any {
	if routingMode == config.RoutingModeAggregateThroughput {
		return map[string]any{
			"sort":            "throughput",
			"ignore":          []string{"coreweave"},
			"allow_fallbacks": true,
			"data_collection": "deny",
			"zdr":             true,
		}
	}
	preferences := map[string]any{
		"only":            []string{provider},
		"allow_fallbacks": false,
		"data_collection": "deny",
		"zdr":             true,
	}
	if quantization != "" {
		preferences["quantizations"] = []string{quantization}
	}
	return preferences
}

// reliabilityProviderPreferences mirrors _reliability_provider_preferences
// (the phase-1 recovery route in aggregate mode).
func reliabilityProviderPreferences() map[string]any {
	return map[string]any{
		"order":           []string{"deepinfra", "groq"},
		"ignore":          []string{"coreweave"},
		"allow_fallbacks": false,
		"data_collection": "deny",
		"zdr":             true,
	}
}

// decodeJSONNumbers parses bytes with UseNumber. ok=false means the body
// was not one JSON document (response.json() raising ValueError); a JSON
// null is ok=true with a nil value, matching Python's None.
func decodeJSONNumbers(body []byte) (any, bool) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil {
		return nil, false
	}
	// Trailing garbage means it was not one JSON document.
	if dec.More() {
		return nil, false
	}
	return decoded, true
}

// openrouterAttemptCount mirrors _openrouter_attempt_count.
func openrouterAttemptCount(decoded any) int {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return 0
	}
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok {
		return 0
	}
	if n, ok := isIntLiteral(metadata["attempt"]); ok {
		if v, err := n.Int64(); err == nil && 1 <= v && v <= 100 {
			return int(v)
		}
	}
	if attempts, ok := metadata["attempts"].([]any); ok && len(attempts) <= 100 {
		return len(attempts)
	}
	return 0
}

// openrouterLastAttemptedProvider mirrors _openrouter_last_attempted_provider.
func openrouterLastAttemptedProvider(decoded any) string {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return ""
	}
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok {
		return ""
	}
	attempts, ok := metadata["attempts"].([]any)
	if !ok || len(attempts) < 1 || len(attempts) > 100 {
		return ""
	}
	last, ok := attempts[len(attempts)-1].(map[string]any)
	if !ok {
		return ""
	}
	provider, ok := last["provider"].(string)
	if !ok || len(provider) < 1 || len(provider) > 120 {
		return ""
	}
	return provider
}

// upstreamProviderIdentity mirrors _upstream_provider: the actual provider
// from opt-in router metadata, with bounded legacy fallback. An empty string
// with nil error means "unknown" (None).
func upstreamProviderIdentity(payload map[string]any) (string, *httpError) {
	metadata, present := payload["openrouter_metadata"]
	if !present || metadata == nil {
		provider, ok := payload["provider"].(string)
		if ok && len(provider) >= 1 && len(provider) <= 120 {
			return provider, nil
		}
		return "", nil
	}
	metadataMap, ok := metadata.(map[string]any)
	if !ok {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	endpoints, _ := metadataMap["endpoints"].(map[string]any)
	availableRaw, ok := endpoints["available"]
	var available []any
	if ok {
		available, ok = availableRaw.([]any)
	}
	if !ok {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	var selected []any
	for _, raw := range available {
		endpoint, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if sel, ok := endpoint["selected"].(bool); ok && sel {
			selected = append(selected, endpoint["provider"])
		}
	}
	if len(selected) != 1 {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	provider, ok := selected[0].(string)
	if !ok || len(provider) < 1 || len(provider) > 120 {
		return "", httpErrorf(502, "provider identity mismatch")
	}
	return provider, nil
}

// boundedUsage mirrors _bounded_usage: prompt/completion must be
// non-negative non-bool ints with sum <= 2^62. Provider-supplied cost is
// ignored here.
func boundedUsage(payload map[string]any) (prompt, completion int64, ok bool) {
	usage, isMap := payload["usage"].(map[string]any)
	if !isMap {
		return 0, 0, false
	}
	pn, pInt := isIntLiteral(usage["prompt_tokens"])
	cn, cInt := isIntLiteral(usage["completion_tokens"])
	if !pInt || !cInt {
		return 0, 0, false
	}
	p, perr := pn.Int64()
	c, cerr := cn.Int64()
	if perr != nil || cerr != nil || p < 0 || c < 0 {
		return 0, 0, false
	}
	// Overflow-safe form of Python's arbitrary-precision p+c > 2^62: both
	// operands are non-negative, so p+c can wrap int64 (e.g. p = c = 2^62)
	// and a direct sum comparison would accept what Python rejects.
	if p > (1<<62)-c {
		return 0, 0, false
	}
	return p, c, true
}

// boundedProviderCost mirrors _bounded_provider_cost: usage.cost as bounded
// integer micro-USD (finite, 0 <= cost <= 100 USD), banker's rounding like
// Python round(). -1 means invalid.
func boundedProviderCost(payload map[string]any) int64 {
	usage, isMap := payload["usage"].(map[string]any)
	if !isMap {
		return -1
	}
	n, ok := asNumber(usage["cost"])
	if !ok {
		return -1
	}
	cost := numFloat(n)
	if math.IsNaN(cost) || math.IsInf(cost, 0) || cost < 0 || cost > 100 {
		return -1
	}
	return int64(math.RoundToEven(cost * 1_000_000))
}

// phaseErrorCode mirrors _phase_error_code.
func phaseErrorCode(err *httpError) string {
	switch err.message {
	case "provider response is too large":
		return "response_too_large"
	case "invalid provider response":
		return "invalid_provider_response"
	case "provider identity mismatch":
		return "provider_identity_mismatch"
	case "inference provider unavailable":
		return "provider_unavailable"
	}
	return "provider_unavailable"
}

func providerRejectionIsRouteObservable(status int) bool {
	return status >= 400 && status != 400 && status != 422
}

// --- chat sanitization ------------------------------------------------

type publicToolFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type publicToolCall struct {
	ID       string             `json:"id"`
	Type     string             `json:"type"`
	Function publicToolFunction `json:"function"`
}

type publicChatMessage struct {
	Role    string  `json:"role"`
	Content *string `json:"content"`
	// Pointer-to-slice, NOT a bare slice with omitempty: Python emits
	// "tool_calls": [] when the provider sent an empty list (tool_calls is
	// not None) and omits the key only when the provider omitted it. A bare
	// omitempty slice would elide the empty list and change the response
	// shape for harnesses that branch on key presence.
	ToolCalls *[]publicToolCall `json:"tool_calls,omitempty"`
}

type publicChatChoice struct {
	Index        json.RawMessage   `json:"index"`
	FinishReason string            `json:"finish_reason"`
	Message      publicChatMessage `json:"message"`
	Logprobs     json.RawMessage   `json:"logprobs"`
}

type publicChatUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
}

type publicChatResponse struct {
	ID      string             `json:"id"`
	Object  string             `json:"object"`
	Created json.RawMessage    `json:"created"`
	Model   string             `json:"model"`
	Choices []publicChatChoice `json:"choices"`
	Usage   publicChatUsage    `json:"usage"`
}

// rawChoiceLogprobs extracts choices[0].logprobs from the raw provider body,
// compacted, so the pass-through preserves provider key order. nil means
// absent (rendered as JSON null, matching Python's .get()).
func rawChoiceLogprobs(body []byte) json.RawMessage {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		return nil
	}
	var choices []json.RawMessage
	if err := json.Unmarshal(top["choices"], &choices); err != nil || len(choices) != 1 {
		return nil
	}
	var choice map[string]json.RawMessage
	if err := json.Unmarshal(choices[0], &choice); err != nil {
		return nil
	}
	raw, ok := choice["logprobs"]
	if !ok {
		return nil
	}
	var compact bytes.Buffer
	if err := json.Compact(&compact, raw); err != nil {
		return nil
	}
	return compact.Bytes()
}

// publicProviderResponse mirrors _public_provider_response: the trust
// boundary between OpenRouter's additive surface and the harness contract.
func publicProviderResponse(payload map[string]any, rawBody []byte) (*publicChatResponse, *httpError) {
	responseID, idOk := payload["id"].(string)
	createdNum, createdOk := isIntLiteral(payload["created"])
	model, modelOk := payload["model"].(string)
	choicesRaw, choicesOk := payload["choices"].([]any)
	if !idOk || len(responseID) < 1 || len(responseID) > 256 ||
		!createdOk ||
		payload["object"] != "chat.completion" ||
		!modelOk || !choicesOk || len(choicesRaw) != 1 {
		return nil, httpErrorf(502, "invalid provider response")
	}
	choice, ok := choicesRaw[0].(map[string]any)
	if !ok {
		return nil, httpErrorf(502, "inference provider unavailable")
	}
	if _, hasError := choice["error"]; hasError {
		return nil, httpErrorf(502, "inference provider unavailable")
	}
	indexNum, indexOk := isIntLiteral(choice["index"])
	finishReason, frOk := choice["finish_reason"].(string)
	message, msgOk := choice["message"].(map[string]any)
	validFinish := frOk && (finishReason == "stop" || finishReason == "length" ||
		finishReason == "tool_calls" || finishReason == "content_filter")
	if !indexOk || !validFinish || !msgOk || message["role"] != "assistant" {
		return nil, httpErrorf(502, "invalid provider response")
	}
	var content *string
	if raw, present := message["content"]; present && raw != nil {
		s, ok := raw.(string)
		if !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
		content = &s
	}
	publicMessage := publicChatMessage{Role: "assistant", Content: content}
	if rawCalls, present := message["tool_calls"]; present && rawCalls != nil {
		calls, ok := rawCalls.([]any)
		if !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
		publicCalls := make([]publicToolCall, 0, len(calls))
		for _, rawCall := range calls {
			call, ok := rawCall.(map[string]any)
			if !ok {
				return nil, httpErrorf(502, "invalid provider response")
			}
			fn, _ := call["function"].(map[string]any)
			id, idOk := call["id"].(string)
			name, nameOk := "", false
			args, argsOk := "", false
			if fn != nil {
				name, nameOk = fn["name"].(string)
				args, argsOk = fn["arguments"].(string)
			}
			if call["type"] != "function" || !idOk || fn == nil || !nameOk || !argsOk {
				return nil, httpErrorf(502, "invalid provider response")
			}
			publicCalls = append(publicCalls, publicToolCall{
				ID:       id,
				Type:     "function",
				Function: publicToolFunction{Name: name, Arguments: args},
			})
		}
		publicMessage.ToolCalls = &publicCalls
	}
	prompt, completion, usageOk := boundedUsage(payload)
	if !usageOk {
		return nil, httpErrorf(502, "invalid provider response")
	}
	if raw, present := choice["logprobs"]; present && raw != nil {
		if _, ok := raw.(map[string]any); !ok {
			return nil, httpErrorf(502, "invalid provider response")
		}
	}
	return &publicChatResponse{
		ID:      responseID,
		Object:  "chat.completion",
		Created: json.RawMessage(createdNum.String()),
		Model:   model,
		Choices: []publicChatChoice{{
			Index:        json.RawMessage(indexNum.String()),
			FinishReason: finishReason,
			Message:      publicMessage,
			Logprobs:     rawChoiceLogprobs(rawBody),
		}},
		Usage: publicChatUsage{
			PromptTokens:     prompt,
			CompletionTokens: completion,
			TotalTokens:      prompt + completion,
		},
	}, nil
}

// compactJSON marshals without HTML escaping and without the encoder's
// trailing newline — the closest Go equivalent of
// json.dumps(..., separators=(",", ":")).
func compactJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	out := buf.Bytes()
	if n := len(out); n > 0 && out[n-1] == '\n' {
		out = out[:n-1]
	}
	return out, nil
}

// --- chat provider orchestration --------------------------------------

type chatCompletionResult struct {
	raw                []byte
	promptTokens       int64
	completionTokens   int64
	costMicrousd       int64
	upstreamProvider   string
	upstreamAttempts   int
	openrouterAttempts int
	fallbackPhase      int
}

type chatProviderExhausted struct {
	upstreamAttempts   int
	openrouterAttempts int
	fallbackPhase      int
	terminalErrorCode  string
	timedOut           bool
	upstreamProvider   string
	routeObservable    bool
}

func (e *chatProviderExhausted) Error() string { return e.terminalErrorCode }

// completeChatWithRecovery mirrors _complete_chat_with_recovery: the fast
// OpenRouter phase, then (aggregate mode only) the bounded reliable route.
func completeChatWithRecovery(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	payload map[string]any, model, expectedProvider, expectedQuantization string,
	expectedPromptPrice, expectedCompletionPrice *float64, sleep sleepFunc) (*chatCompletionResult, *chatProviderExhausted) {

	aggregate := cfg.RoutingMode == config.RoutingModeAggregateThroughput
	type phaseSpec struct {
		phase   int
		payload map[string]any
	}
	primary := make(map[string]any, len(payload)+1)
	for k, v := range payload {
		primary[k] = v
	}
	primary["provider"] = providerPreferences(cfg.RoutingMode, expectedProvider, expectedQuantization)
	phases := []phaseSpec{{phase: 0, payload: primary}}
	if aggregate {
		reliable := make(map[string]any, len(payload)+1)
		for k, v := range payload {
			reliable[k] = v
		}
		reliable["provider"] = reliabilityProviderPreferences()
		phases = append(phases, phaseSpec{phase: 1, payload: reliable})
	}
	headers := openrouterHeaders(cfg.OpenRouterAPIKey, true)

	totalAttempts := 0
	routerAttempts := 0
	lastCode := "provider_unavailable"
	lastTimedOut := false
	lastPhase := 0
	lastProvider := ""
	routeObservable := false
	for _, spec := range phases {
		lastPhase = spec.phase
		result, callErr := postProviderWithRetry(ctx, client, cfg.UpstreamURL, spec.payload, headers,
			cfg.ResponseBodyBytes, cfg.TimeoutSeconds, true, sleep)
		if callErr != nil {
			totalAttempts += callErr.attempts
			lastTimedOut = callErr.timedOut
			if callErr.timedOut {
				lastCode = "provider_timeout"
			} else {
				lastCode = "provider_transport"
			}
			routeObservable = true
			continue
		}
		totalAttempts += result.attempts
		if result.bodyOverLimit {
			lastCode = "response_too_large"
			continue
		}
		decoded, _ := decodeJSONNumbers(result.body)
		routerAttempts += openrouterAttemptCount(decoded)
		if provider := openrouterLastAttemptedProvider(decoded); provider != "" {
			lastProvider = provider
		}
		if result.status >= 400 {
			lastTimedOut = result.status == 408 || result.status == 504
			lastCode = "upstream_http_" + strconv.Itoa(result.status)
			routeObservable = routeObservable || providerRejectionIsRouteObservable(result.status)
			continue
		}
		routeObservable = true
		decodedMap, ok := decoded.(map[string]any)
		if !ok {
			lastCode = "invalid_provider_response"
			continue
		}
		if m, ok := decodedMap["model"].(string); !ok || m != model {
			lastCode = "provider_identity_mismatch"
			continue
		}
		phaseResult, herr := func() (*chatCompletionResult, *httpError) {
			providerValue, herr := upstreamProviderIdentity(decodedMap)
			if herr != nil {
				return nil, herr
			}
			if providerValue == "" || (!aggregate && providerValue != expectedProvider) {
				return nil, httpErrorf(502, "provider identity mismatch")
			}
			prompt, completion, usageOk := boundedUsage(decodedMap)
			if !usageOk {
				return nil, httpErrorf(502, "invalid provider response")
			}
			var cost int64
			if aggregate {
				cost = boundedProviderCost(decodedMap)
				if cost < 0 {
					return nil, httpErrorf(502, "invalid provider response")
				}
			} else {
				if expectedPromptPrice == nil || expectedCompletionPrice == nil {
					return nil, httpErrorf(502, "invalid provider response")
				}
				cost = int64((float64(prompt)**expectedPromptPrice +
					float64(completion)**expectedCompletionPrice) * 1_000_000)
			}
			public, herr := publicProviderResponse(decodedMap, result.body)
			if herr != nil {
				return nil, herr
			}
			raw, err := compactJSON(public)
			if err != nil {
				return nil, httpErrorf(502, "invalid provider response")
			}
			return &chatCompletionResult{
				raw:              raw,
				promptTokens:     prompt,
				completionTokens: completion,
				costMicrousd:     cost,
				upstreamProvider: providerValue,
			}, nil
		}()
		if herr != nil {
			lastCode = phaseErrorCode(herr)
			continue
		}
		phaseResult.upstreamAttempts = totalAttempts
		phaseResult.openrouterAttempts = routerAttempts
		phaseResult.fallbackPhase = spec.phase
		return phaseResult, nil
	}
	return nil, &chatProviderExhausted{
		upstreamAttempts:   totalAttempts,
		openrouterAttempts: routerAttempts,
		fallbackPhase:      lastPhase,
		terminalErrorCode:  lastCode,
		timedOut:           lastTimedOut,
		upstreamProvider:   lastProvider,
		routeObservable:    routeObservable,
	}
}

// --- embedding provider ------------------------------------------------

type embeddingProviderResult struct {
	result   *providerHTTPResult
	attempts int
	direct   bool
}

// postEmbeddingProvider mirrors _post_embedding_provider: direct Perplexity
// first when the key is configured, then OpenRouter for the same frozen
// model. Neither route retries backpressure in place.
func postEmbeddingProvider(ctx context.Context, client *http.Client, cfg config.InferenceProxyConfig,
	inputs []string, sleep sleepFunc) (*embeddingProviderResult, *providerCallError) {

	var direct *providerHTTPResult
	var directErr *providerCallError
	if cfg.PerplexityAPIKey != "" {
		direct, directErr = postProviderWithRetry(ctx, client, cfg.EmbeddingFallbackURL,
			map[string]any{
				"model":           pplxEmbedResponseModel,
				"input":           inputs,
				"dimensions":      cfg.EmbeddingDimensions,
				"encoding_format": "base64_int8",
			},
			map[string]string{
				"Authorization": "Bearer " + cfg.PerplexityAPIKey,
				"Content-Type":  "application/json",
			},
			cfg.EmbeddingResponseBodyBytes, cfg.TimeoutSeconds, false, sleep)
		if direct != nil && direct.status < 400 {
			return &embeddingProviderResult{result: direct, attempts: direct.attempts, direct: true}, nil
		}
		if direct != nil && !isProviderRetryStatus(direct.status) {
			return &embeddingProviderResult{result: direct, attempts: direct.attempts, direct: true}, nil
		}
	}
	openrouter, orErr := postProviderWithRetry(ctx, client, cfg.EmbeddingUpstreamURL,
		map[string]any{
			"model":           cfg.EmbeddingModel,
			"input":           inputs,
			"dimensions":      cfg.EmbeddingDimensions,
			"encoding_format": "float",
			"provider": map[string]any{
				"order":           []string{cfg.EmbeddingProvider},
				"allow_fallbacks": false,
				"data_collection": "deny",
			},
		},
		openrouterHeaders(cfg.OpenRouterAPIKey, false),
		cfg.EmbeddingResponseBodyBytes, cfg.TimeoutSeconds, false, sleep)
	if orErr != nil {
		priorAttempts := 0
		if directErr != nil {
			priorAttempts = directErr.attempts
		}
		return nil, &providerCallError{attempts: priorAttempts + orErr.attempts, timedOut: orErr.timedOut}
	}
	priorAttempts := 0
	if direct != nil {
		priorAttempts = direct.attempts
	} else if directErr != nil {
		priorAttempts = directErr.attempts
	}
	return &embeddingProviderResult{
		result:   openrouter,
		attempts: priorAttempts + openrouter.attempts,
		direct:   false,
	}, nil
}

// --- embedding sanitization --------------------------------------------

// perplexityEmbeddingResponse mirrors _perplexity_embedding_response:
// convert the direct API's base64 signed-INT8 vectors to the reviewed float
// wire (byte/128).
func perplexityEmbeddingResponse(decoded any) (any, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return nil, httpErrorf(502, "invalid provider response")
	}
	data, ok := payload["data"].([]any)
	if !ok {
		return nil, httpErrorf(502, "invalid provider response")
	}
	converted := make([]any, 0, len(data))
	for _, rawItem := range data {
		item, _ := rawItem.(map[string]any)
		var encoded string
		encodedOk := false
		if item != nil {
			encoded, encodedOk = item["embedding"].(string)
		}
		if !encodedOk {
			return nil, httpErrorf(502, "invalid provider response")
		}
		raw, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			return nil, httpErrorf(502, "invalid provider response")
		}
		vector := make([]any, len(raw))
		for i, b := range raw {
			vector[i] = float64(int8(b)) / 128
		}
		out := make(map[string]any, len(item))
		for k, v := range item {
			out[k] = v
		}
		out["embedding"] = vector
		converted = append(converted, out)
	}
	out := make(map[string]any, len(payload))
	for k, v := range payload {
		out[k] = v
	}
	out["data"] = converted
	return out, nil
}

type publicEmbeddingItem struct {
	Object    string `json:"object"`
	Index     int    `json:"index"`
	Embedding []any  `json:"embedding"`
}

type publicEmbeddingUsage struct {
	PromptTokens int64 `json:"prompt_tokens"`
	TotalTokens  int64 `json:"total_tokens"`
}

type publicEmbeddingResponse struct {
	Object string                `json:"object"`
	Model  string                `json:"model"`
	Data   []publicEmbeddingItem `json:"data"`
	Usage  publicEmbeddingUsage  `json:"usage"`
}

// publicEmbeddingResponseFrom mirrors _public_embedding_response: envelope,
// ordering, arity, and vector length are validated; individual elements are
// deliberately NOT float-validated (the broker re-validates them).
func publicEmbeddingResponseFrom(decoded any, model string, dimensions, inputCount int) (*publicEmbeddingResponse, int64, *httpError) {
	payload, ok := decoded.(map[string]any)
	if !ok {
		return nil, 0, httpErrorf(502, "provider identity mismatch")
	}
	responseModel, _ := payload["model"].(string)
	modelOk := responseModel == model
	if model == pplxEmbedContractModel && responseModel == pplxEmbedResponseModel {
		modelOk = true
	}
	if !modelOk {
		return nil, 0, httpErrorf(502, "provider identity mismatch")
	}
	data, dataOk := payload["data"].([]any)
	usage, _ := payload["usage"].(map[string]any)
	var promptTokens int64
	tokensOk := false
	if usage != nil {
		if n, ok := isIntLiteral(usage["prompt_tokens"]); ok {
			if v, err := n.Int64(); err == nil {
				promptTokens = v
				tokensOk = true
			}
		}
	}
	if !dataOk || len(data) != inputCount || !tokensOk || promptTokens < 0 {
		return nil, 0, httpErrorf(502, "invalid provider response")
	}
	publicData := make([]publicEmbeddingItem, 0, len(data))
	for expectedIndex, rawItem := range data {
		item, itemOk := rawItem.(map[string]any)
		var vector []any
		vectorOk := false
		indexOk := false
		if itemOk {
			if n, ok := asNumber(item["index"]); ok {
				indexOk = numFloat(n) == float64(expectedIndex)
			}
			switch v := item["embedding"].(type) {
			case []any:
				vector, vectorOk = v, true
			case []float64:
				vector = make([]any, len(v))
				for i, f := range v {
					vector[i] = f
				}
				vectorOk = true
			}
		}
		if !itemOk || !indexOk || !vectorOk || len(vector) != dimensions {
			return nil, 0, httpErrorf(502, "invalid provider response")
		}
		publicData = append(publicData, publicEmbeddingItem{
			Object:    "embedding",
			Index:     expectedIndex,
			Embedding: vector,
		})
	}
	return &publicEmbeddingResponse{
		Object: "list",
		Model:  model,
		Data:   publicData,
		Usage:  publicEmbeddingUsage{PromptTokens: promptTokens, TotalTokens: promptTokens},
	}, promptTokens, nil
}
