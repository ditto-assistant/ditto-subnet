package inference

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// httpError mirrors a Python HTTPException: status + detail, rendered as the
// 3002 envelope by the handlers.
type httpError struct {
	status  int
	message string
	headers map[string]string
}

func (e *httpError) Error() string { return fmt.Sprintf("%d: %s", e.status, e.message) }

func httpErrorf(status int, format string, args ...any) *httpError {
	return &httpError{status: status, message: fmt.Sprintf(format, args...)}
}

// Field fates, mirroring endpoints/inference.py. The default for a known
// field is FORWARD; every pin/drop/refusal is explicit.
var pinnedRequestFields = map[string]struct{}{
	"model": {}, "max_tokens": {}, "max_completion_tokens": {}, "n": {},
	"best_of": {}, "reasoning": {}, "reasoning_effort": {}, "include_reasoning": {},
	"service_tier": {}, "usage": {}, "prompt_cache_key": {},
}

var droppedRequestFields = map[string]struct{}{
	"user": {}, "metadata": {}, "safety_identifier": {}, "store": {}, "stream_options": {},
	"provider": {}, "route": {}, "preset": {},
}

var forwardedRequestFields = map[string]struct{}{
	"messages": {}, "tools": {}, "tool_choice": {}, "parallel_tool_calls": {},
	"temperature": {}, "top_p": {}, "top_k": {}, "min_p": {}, "top_a": {},
	"seed": {}, "stop": {}, "frequency_penalty": {}, "presence_penalty": {},
	"repetition_penalty": {}, "logit_bias": {}, "logprobs": {},
	"response_format": {}, "structured_outputs": {}, "prediction": {},
	"verbosity": {}, "stream": {},
}

var refusedRequestFields = map[string]string{
	"models":             "the model is pinned by the ticket, not chosen by the request",
	"transforms":         "prompt transforms would change benchmark semantics",
	"plugins":            "server-side plugins are not available on this lane",
	"web_search_options": "server-side web search is not available on this lane",
	"functions":          "use tools instead",
	"function_call":      "use tool_choice instead",
	"audio":              "this lane serves text completions only",
	"modalities":         "this lane serves text completions only",
	"top_logprobs":       "logprobs is supported but top_logprobs is not: it would exceed this lane's response size limit",
}

func isAllowedRequestField(key string) bool {
	if _, ok := pinnedRequestFields[key]; ok {
		return true
	}
	if _, ok := droppedRequestFields[key]; ok {
		return true
	}
	_, ok := forwardedRequestFields[key]
	return ok
}

// JSON type helpers over decoder.UseNumber() values. json.Number preserves
// the literal, so Python's int/float distinction (a "." or exponent makes it
// a float) is recoverable, and bool is a distinct Go type already.

func asNumber(v any) (json.Number, bool) {
	n, ok := v.(json.Number)
	return n, ok
}

// isIntLiteral reports whether the value is a JSON integer literal
// (no fraction, no exponent) — Python's isinstance(value, int).
func isIntLiteral(v any) (json.Number, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return "", false
	}
	s := n.String()
	if strings.ContainsAny(s, ".eE") {
		return "", false
	}
	return n, true
}

// numFloat parses any JSON number to float64. Overflow yields ±Inf, which
// range checks treat like Python treats non-finite floats.
func numFloat(n json.Number) float64 {
	f, err := strconv.ParseFloat(n.String(), 64)
	if err != nil {
		if strings.HasPrefix(n.String(), "-") {
			return math.Inf(-1)
		}
		return math.Inf(1)
	}
	return f
}

// intAtLeast reports value >= min for an integer literal of arbitrary
// magnitude (Python ints are unbounded; a positive overflow still satisfies
// ">= min" for small min).
func intAtLeast(n json.Number, min int64) bool {
	if v, err := n.Int64(); err == nil {
		return v >= min
	}
	// Overflowed int64: sign decides.
	return !strings.HasPrefix(n.String(), "-")
}

// validateRequestSchema mirrors _validate_request_schema check-for-check,
// including error-message precedence.
func validateRequestSchema(payload map[string]any) *httpError {
	// Named refusals first.
	var refused []string
	for key := range payload {
		if _, ok := refusedRequestFields[key]; ok {
			refused = append(refused, key)
		}
	}
	if len(refused) > 0 {
		sort.Strings(refused)
		reasons := make([]string, len(refused))
		for i, key := range refused {
			reasons[i] = fmt.Sprintf("%s (%s)", key, refusedRequestFields[key])
		}
		return httpErrorf(400, "unsupported inference parameter: %s", strings.Join(reasons, "; "))
	}
	var unknown []string
	for key := range payload {
		if !isAllowedRequestField(key) {
			unknown = append(unknown, key)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return httpErrorf(400, "unsupported inference parameter: %s", strings.Join(unknown, ", "))
	}
	// Numeric knobs: finite int/float, bool rejected.
	for _, name := range []string{"temperature", "top_p", "frequency_penalty", "presence_penalty", "min_p", "top_a", "repetition_penalty"} {
		if v, present := payload[name]; present && v != nil {
			n, ok := asNumber(v)
			if !ok {
				return httpErrorf(400, "invalid %s", name)
			}
			f := numFloat(n)
			if math.IsNaN(f) || math.IsInf(f, 0) {
				return httpErrorf(400, "invalid %s", name)
			}
		}
	}
	for _, name := range []string{"frequency_penalty", "presence_penalty"} {
		if v, present := payload[name]; present && v != nil {
			f := numFloat(v.(json.Number))
			if !(-2 <= f && f <= 2) {
				return httpErrorf(400, "invalid %s", name)
			}
		}
	}
	for _, name := range []string{"min_p", "top_a"} {
		if v, present := payload[name]; present && v != nil {
			f := numFloat(v.(json.Number))
			if !(0 <= f && f <= 1) {
				return httpErrorf(400, "invalid %s", name)
			}
		}
	}
	if v, present := payload["repetition_penalty"]; present && v != nil {
		f := numFloat(v.(json.Number))
		if !(0 < f && f <= 2) {
			return httpErrorf(400, "invalid repetition_penalty")
		}
	}
	if v, present := payload["top_k"]; present && v != nil {
		n, ok := isIntLiteral(v)
		if !ok || !intAtLeast(n, 0) {
			return httpErrorf(400, "invalid top_k")
		}
	}
	if v, present := payload["logprobs"]; present {
		if _, ok := v.(bool); !ok {
			return httpErrorf(400, "invalid logprobs")
		}
	}
	if v, present := payload["temperature"]; present && v != nil {
		f := numFloat(v.(json.Number))
		if !(0 <= f && f <= 2) {
			return httpErrorf(400, "invalid temperature")
		}
	}
	if v, present := payload["top_p"]; present && v != nil {
		f := numFloat(v.(json.Number))
		if !(0 <= f && f <= 1) {
			return httpErrorf(400, "invalid top_p")
		}
	}
	if v, present := payload["seed"]; present && v != nil {
		n, ok := isIntLiteral(v)
		if !ok {
			return httpErrorf(400, "invalid seed")
		}
		// Python bounds: -(2**63) <= seed < 2**63, exactly the int64 range.
		if _, err := n.Int64(); err != nil {
			return httpErrorf(400, "invalid seed")
		}
	}
	if v, present := payload["stop"]; present && v != nil {
		valid := false
		if _, ok := v.(string); ok {
			valid = true
		} else if list, ok := v.([]any); ok && len(list) >= 1 && len(list) <= 4 {
			valid = true
			for _, item := range list {
				if _, ok := item.(string); !ok {
					valid = false
					break
				}
			}
		}
		if !valid {
			return httpErrorf(400, "invalid stop")
		}
	}
	for _, name := range []string{"parallel_tool_calls", "stream", "store"} {
		if v, present := payload[name]; present {
			if _, ok := v.(bool); !ok {
				return httpErrorf(400, "invalid %s", name)
			}
		}
	}
	if v, present := payload["stream"]; present {
		if b, ok := v.(bool); ok && b {
			return httpErrorf(400, "unsupported inference parameter: stream (this lane answers with a single non-streaming response)")
		}
	}
	for _, name := range []string{"n", "best_of"} {
		if v, present := payload[name]; present && v != nil {
			n, ok := isIntLiteral(v)
			if !ok || !intAtLeast(n, 1) {
				return httpErrorf(400, "invalid %s", name)
			}
		}
	}
	if v, present := payload["tool_choice"]; present && v != nil {
		if err := validateToolChoice(v); err != nil {
			return err
		}
	}
	if err := validateMessages(payload["messages"]); err != nil {
		return err
	}
	tools, present := payload["tools"]
	if !present {
		tools = []any{}
	}
	return validateTools(tools)
}

func validateToolChoice(v any) *httpError {
	if s, ok := v.(string); ok {
		if s == "none" || s == "auto" || s == "required" {
			return nil
		}
		return httpErrorf(400, "invalid tool_choice")
	}
	m, ok := v.(map[string]any)
	if !ok || len(m) != 2 {
		return httpErrorf(400, "invalid tool_choice")
	}
	typ, hasType := m["type"]
	fn, hasFn := m["function"]
	if !hasType || !hasFn || typ != "function" {
		return httpErrorf(400, "invalid tool_choice")
	}
	fnMap, ok := fn.(map[string]any)
	if !ok || len(fnMap) != 1 {
		return httpErrorf(400, "invalid tool_choice")
	}
	name, ok := fnMap["name"].(string)
	if !ok || name == "" {
		return httpErrorf(400, "invalid tool_choice")
	}
	return nil
}

var messageAllowedKeys = map[string]map[string]struct{}{
	"system":    {"role": {}, "content": {}},
	"user":      {"role": {}, "content": {}},
	"assistant": {"role": {}, "content": {}, "tool_calls": {}},
	"tool":      {"role": {}, "content": {}, "tool_call_id": {}, "name": {}},
}

func validateMessages(v any) *httpError {
	messages, ok := v.([]any)
	if !ok || len(messages) == 0 {
		return httpErrorf(400, "messages must be non-empty")
	}
	for _, raw := range messages {
		message, ok := raw.(map[string]any)
		if !ok {
			return httpErrorf(400, "invalid message")
		}
		role, ok := message["role"].(string)
		if !ok {
			return httpErrorf(400, "invalid message")
		}
		if _, ok := messageAllowedKeys[role]; !ok {
			return httpErrorf(400, "invalid message")
		}
		// Extra keys are dropped in lockedUpstreamPayload, not refused.
		// Miners echo provider-additive fields (assistant reasoning, tool_call
		// index) and killing the run over them is the Cooking-class bug.
		content, hasContent := message["content"]
		if hasContent && content != nil {
			if _, isStr := content.(string); !isStr {
				if !isTextParts(content) {
					return httpErrorf(400, "text content only")
				}
			}
		}
		if role == "tool" {
			if nameValue, present := message["name"]; present {
				name, ok := nameValue.(string)
				if !ok || name == "" {
					return httpErrorf(400, "invalid tool name")
				}
			}
		}
		toolCallsValue, present := message["tool_calls"]
		if !present {
			continue
		}
		toolCalls, ok := toolCallsValue.([]any)
		if !ok {
			return httpErrorf(400, "invalid tool calls")
		}
		for _, callRaw := range toolCalls {
			call, ok := callRaw.(map[string]any)
			if !ok {
				return httpErrorf(400, "invalid tool call")
			}
			fn, _ := call["function"].(map[string]any)
			id, idOk := call["id"].(string)
			_ = id
			if call["type"] != "function" || !idOk || fn == nil {
				return httpErrorf(400, "invalid tool call")
			}
			if _, ok := fn["name"].(string); !ok {
				return httpErrorf(400, "invalid tool call")
			}
			if _, ok := fn["arguments"].(string); !ok {
				return httpErrorf(400, "invalid tool call")
			}
		}
	}
	return nil
}

func isTextParts(content any) bool {
	parts, ok := content.([]any)
	if !ok || len(parts) == 0 {
		return false
	}
	for _, raw := range parts {
		part, ok := raw.(map[string]any)
		if !ok || len(part) != 2 {
			return false
		}
		if part["type"] != "text" {
			return false
		}
		if _, ok := part["text"].(string); !ok {
			return false
		}
	}
	return true
}

func validateTools(v any) *httpError {
	tools, ok := v.([]any)
	if !ok {
		return httpErrorf(400, "invalid tools")
	}
	for _, raw := range tools {
		tool, ok := raw.(map[string]any)
		if !ok {
			return httpErrorf(400, "function tools only")
		}
		for key := range tool {
			if key != "type" && key != "function" {
				return httpErrorf(400, "function tools only")
			}
		}
		fn, _ := tool["function"].(map[string]any)
		if tool["type"] != "function" || fn == nil {
			return httpErrorf(400, "invalid function tool")
		}
		for key := range fn {
			switch key {
			case "name", "description", "parameters", "strict":
			default:
				return httpErrorf(400, "invalid function tool")
			}
		}
		if _, ok := fn["name"].(string); !ok {
			return httpErrorf(400, "invalid function tool")
		}
		if params, present := fn["parameters"]; present {
			if _, ok := params.(map[string]any); !ok {
				return httpErrorf(400, "invalid function tool")
			}
		}
	}
	return nil
}

// outputTokenLimit mirrors _output_token_limit: normalize the two OpenAI
// aliases downward against the ticket ceiling.
func outputTokenLimit(payload map[string]any, maximum int) (int, *httpError) {
	result := int64(maximum)
	for _, name := range []string{"max_tokens", "max_completion_tokens"} {
		v, present := payload[name]
		if !present || v == nil {
			continue
		}
		n, ok := isIntLiteral(v)
		if !ok {
			return 0, httpErrorf(400, "invalid %s", name)
		}
		value, err := n.Int64()
		if err != nil {
			// Arbitrarily large positive int: an over-ask, clamps to the
			// ceiling; negative overflow is < 1.
			if strings.HasPrefix(n.String(), "-") {
				return 0, httpErrorf(400, "invalid %s", name)
			}
			continue
		}
		if value < 1 {
			return 0, httpErrorf(400, "invalid %s", name)
		}
		if value < result {
			result = value
		}
	}
	return int(result), nil
}

// Reasoning contract (inference_routing.py + _benchmark_reasoning_for_request).
const (
	v7Model                = "openai/gpt-oss-20b"
	defaultReasoningEffort = "medium"
)

func isReasoningEffort(s string) bool { return s == "low" || s == "medium" || s == "high" }

// benchmarkReasoningForRequest resolves the provider reasoning block. A nil
// map with nil error means "no reasoning contract for this model".
func benchmarkReasoningForRequest(payload map[string]any, model string, benchVersion int32) (map[string]any, *httpError) {
	if model != v7Model {
		return nil, nil
	}
	if benchVersion < 9 {
		return map[string]any{"effort": "medium", "exclude": true}, nil
	}
	var nestedEffort, flatEffort string
	if nestedRaw, present := payload["reasoning"]; present {
		nested, ok := nestedRaw.(map[string]any)
		if !ok {
			return nil, httpErrorf(400, "invalid reasoning")
		}
		// Allowed shapes: {"effort"} or {"effort","exclude"} with
		// exclude exactly true.
		effortRaw, hasEffort := nested["effort"]
		excludeRaw, hasExclude := nested["exclude"]
		switch {
		case hasEffort && len(nested) == 1:
		case hasEffort && hasExclude && len(nested) == 2:
			if b, ok := excludeRaw.(bool); !ok || !b {
				return nil, httpErrorf(400, "invalid reasoning")
			}
		default:
			return nil, httpErrorf(400, "invalid reasoning")
		}
		effort, ok := effortRaw.(string)
		if !ok || !isReasoningEffort(effort) {
			return nil, httpErrorf(400, "invalid reasoning effort")
		}
		nestedEffort = effort
	}
	if flatRaw, present := payload["reasoning_effort"]; present {
		effort, ok := flatRaw.(string)
		if !ok || !isReasoningEffort(effort) {
			return nil, httpErrorf(400, "invalid reasoning_effort")
		}
		flatEffort = effort
	}
	if nestedEffort != "" && flatEffort != "" && nestedEffort != flatEffort {
		return nil, httpErrorf(400, "conflicting reasoning effort")
	}
	effort := nestedEffort
	if effort == "" {
		effort = flatEffort
	}
	if effort == "" {
		effort = defaultReasoningEffort
	}
	return map[string]any{"effort": effort, "exclude": true}, nil
}

// lockedUpstreamPayload mirrors _locked_upstream_payload: force consensus
// model/reasoning/output fields before provider routing.
func lockedUpstreamPayload(payload map[string]any, model string, maxTokens int, benchVersion int32) (map[string]any, *httpError) {
	upstream := make(map[string]any, len(payload)+4)
	for k, v := range payload {
		upstream[k] = v
	}
	for field := range droppedRequestFields {
		delete(upstream, field)
	}
	for _, field := range []string{"best_of", "reasoning_effort", "include_reasoning", "service_tier", "usage", "prompt_cache_key", "max_completion_tokens"} {
		delete(upstream, field)
	}
	upstream["model"] = model
	if messages, ok := upstream["messages"].([]any); ok {
		upstream["messages"] = sanitizeUpstreamMessages(messages)
	}
	upstream["max_tokens"] = maxTokens
	upstream["n"] = 1
	upstream["stream"] = false
	reasoning, herr := benchmarkReasoningForRequest(payload, model, benchVersion)
	if herr != nil {
		return nil, herr
	}
	if reasoning == nil {
		delete(upstream, "reasoning")
	} else {
		upstream["reasoning"] = reasoning
	}
	return upstream, nil
}

func sanitizeUpstreamMessages(messages []any) []any {
	stripped := make([]any, len(messages))
	for i, raw := range messages {
		message, ok := raw.(map[string]any)
		if !ok {
			stripped[i] = raw
			continue
		}
		role, _ := message["role"].(string)
		allowed := messageAllowedKeys[role]
		clean := make(map[string]any, len(allowed))
		for key, value := range message {
			if _, ok := allowed[key]; !ok {
				continue
			}
			if role == "tool" && key == "name" {
				continue
			}
			if key == "tool_calls" {
				clean[key] = sanitizeUpstreamToolCalls(value)
				continue
			}
			clean[key] = value
		}
		stripped[i] = clean
	}
	return stripped
}

func sanitizeUpstreamToolCalls(raw any) any {
	calls, ok := raw.([]any)
	if !ok {
		return raw
	}
	out := make([]any, 0, len(calls))
	for _, callRaw := range calls {
		call, ok := callRaw.(map[string]any)
		if !ok {
			continue
		}
		fn, _ := call["function"].(map[string]any)
		cleanFn := map[string]any{}
		if fn != nil {
			if name, present := fn["name"]; present {
				cleanFn["name"] = name
			}
			if arguments, present := fn["arguments"]; present {
				cleanFn["arguments"] = arguments
			}
		}
		out = append(out, map[string]any{
			"id":       call["id"],
			"type":     call["type"],
			"function": cleanFn,
		})
	}
	return out
}

// estimatedTokens mirrors _estimated_tokens: ceil(len/4), floored at 1.
func estimatedTokens(body []byte) int64 {
	n := (int64(len(body)) + 3) / 4
	if n < 1 {
		return 1
	}
	return n
}

// maxChargeableTokens mirrors _max_chargeable_tokens.
func maxChargeableTokens(body []byte, outputTokens int64) int64 {
	b := int64(len(body))
	if b < 1 {
		b = 1
	}
	return outputTokens + b
}
