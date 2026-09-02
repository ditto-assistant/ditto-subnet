package inference

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/config"
)

func parsePayload(t *testing.T, body string) map[string]any {
	t.Helper()
	dec := json.NewDecoder(bytes.NewReader([]byte(body)))
	dec.UseNumber()
	var decoded any
	if err := dec.Decode(&decoded); err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	payload, ok := decoded.(map[string]any)
	if !ok {
		t.Fatalf("fixture is not an object")
	}
	return payload
}

const minimalMessages = `"messages":[{"role":"user","content":"hi"}]`

func TestValidateRequestSchema(t *testing.T) {
	cases := []struct {
		name    string
		body    string
		wantErr string // "" means valid
	}{
		{"minimal valid", `{` + minimalMessages + `}`, ""},
		{"dropped provider", `{"provider":{},` + minimalMessages + `}`, ""},
		{"dropped route", `{"route":1,` + minimalMessages + `}`, ""},
		{"refused models", `{"models":[],` + minimalMessages + `}`,
			"unsupported inference parameter: models (the model is pinned by the ticket, not chosen by the request)"},
		{"refused miner transforms", `{"transforms":["middle-out"],` + minimalMessages + `}`,
			"unsupported inference parameter: transforms (prompt transforms would change benchmark semantics)"},
		{"unknown keys sorted", `{"zzz":1,"aaa":2,` + minimalMessages + `}`,
			"unsupported inference parameter: aaa, zzz"},
		{"stream true refused", `{"stream":true,` + minimalMessages + `}`,
			"unsupported inference parameter: stream (this lane answers with a single non-streaming response)"},
		{"stream false ok", `{"stream":false,` + minimalMessages + `}`, ""},
		{"stream non-bool", `{"stream":1,` + minimalMessages + `}`, "invalid stream"},
		{"temperature bool", `{"temperature":true,` + minimalMessages + `}`, "invalid temperature"},
		{"temperature range", `{"temperature":2.5,` + minimalMessages + `}`, "invalid temperature"},
		{"temperature ok", `{"temperature":1.5,` + minimalMessages + `}`, ""},
		{"top_p range", `{"top_p":1.01,` + minimalMessages + `}`, "invalid top_p"},
		{"frequency_penalty range", `{"frequency_penalty":-2.5,` + minimalMessages + `}`, "invalid frequency_penalty"},
		{"repetition_penalty zero", `{"repetition_penalty":0,` + minimalMessages + `}`, "invalid repetition_penalty"},
		{"top_k float", `{"top_k":1.5,` + minimalMessages + `}`, "invalid top_k"},
		{"top_k negative", `{"top_k":-1,` + minimalMessages + `}`, "invalid top_k"},
		{"top_k ok", `{"top_k":40,` + minimalMessages + `}`, ""},
		{"logprobs non-bool", `{"logprobs":1,` + minimalMessages + `}`, "invalid logprobs"},
		{"seed float", `{"seed":1.5,` + minimalMessages + `}`, "invalid seed"},
		{"seed out of range", `{"seed":9223372036854775808,` + minimalMessages + `}`, "invalid seed"},
		{"seed min ok", `{"seed":-9223372036854775808,` + minimalMessages + `}`, ""},
		{"stop list too long", `{"stop":["a","b","c","d","e"],` + minimalMessages + `}`, "invalid stop"},
		{"stop empty list", `{"stop":[],` + minimalMessages + `}`, "invalid stop"},
		{"stop string ok", `{"stop":"end",` + minimalMessages + `}`, ""},
		{"n zero", `{"n":0,` + minimalMessages + `}`, "invalid n"},
		{"best_of bool", `{"best_of":true,` + minimalMessages + `}`, "invalid best_of"},
		{"tool_choice bad string", `{"tool_choice":"never",` + minimalMessages + `}`, "invalid tool_choice"},
		{"tool_choice named ok", `{"tool_choice":{"type":"function","function":{"name":"f"}},` + minimalMessages + `}`, ""},
		{"tool_choice extra key", `{"tool_choice":{"type":"function","function":{"name":"f"},"x":1},` + minimalMessages + `}`, "invalid tool_choice"},
		{"messages missing", `{}`, "messages must be non-empty"},
		{"messages empty", `{"messages":[]}`, "messages must be non-empty"},
		{"message bad role", `{"messages":[{"role":"robot","content":"x"}]}`, "invalid message"},
		{"message extra key", `{"messages":[{"role":"user","content":"x","name":"u"}]}`, ""},
		{"tool message with name ok", `{"messages":[{"role":"tool","content":"x","tool_call_id":"1","name":"f"}]}`, ""},
		{"tool message empty name", `{"messages":[{"role":"tool","content":"x","tool_call_id":"1","name":""}]}`, "invalid tool name"},
		{"content parts ok", `{"messages":[{"role":"user","content":[{"type":"text","text":"x"}]}]}`, ""},
		{"content image part", `{"messages":[{"role":"user","content":[{"type":"image_url","image_url":"u"}]}]}`, "text content only"},
		{"assistant tool_calls ok", `{"messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"1","type":"function","function":{"name":"f","arguments":"{}"}}]}]}`, ""},
		{"tool call extra key", `{"messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"1","type":"function","index":0,"function":{"name":"f","arguments":"{}"}}]}]}`, ""},
		{"assistant reasoning_content ok", `{"messages":[{"role":"assistant","content":"","reasoning_content":"think"}]}`, ""},
		{"assistant reasoning alias ok", `{"messages":[{"role":"assistant","content":"","reasoning":"think","reasoning_content":"think"}]}`, ""},
		{"assistant reasoning_details ok", `{"messages":[{"role":"assistant","content":"","reasoning_details":[{"type":"reasoning.text","text":"think"}]}]}`, ""},
		{"assistant reasoning_content non-string", `{"messages":[{"role":"assistant","content":"","reasoning_content":{"x":1}}]}`, "invalid reasoning_content"},
		{"assistant reasoning non-string", `{"messages":[{"role":"assistant","content":"","reasoning":{"effort":"medium"}}]}`, "invalid reasoning"},
		{"assistant reasoning_details non-list", `{"messages":[{"role":"assistant","content":"","reasoning_details":"think"}]}`, "invalid reasoning_details"},
		{"tool calls non-list", `{"messages":[{"role":"assistant","content":null,"tool_calls":{}}]}`, "invalid tool calls"},
		{"tools ok", `{"tools":[{"type":"function","function":{"name":"f","description":"d","parameters":{}}}],` + minimalMessages + `}`, ""},
		{"tools bad type", `{"tools":[{"type":"web","function":{"name":"f"}}],` + minimalMessages + `}`, "invalid function tool"},
		{"tools extra key", `{"tools":[{"type":"function","extra":1,"function":{"name":"f"}}],` + minimalMessages + `}`, "function tools only"},
		{"tools bad parameters", `{"tools":[{"type":"function","function":{"name":"f","parameters":[]}}],` + minimalMessages + `}`, "invalid function tool"},
		{"tools empty name", `{"tools":[{"type":"function","function":{"name":""}}],` + minimalMessages + `}`, "invalid function tool"},
		{"tools duplicate name", `{"tools":[{"type":"function","function":{"name":"f"}},{"type":"function","function":{"name":"f"}}],` + minimalMessages + `}`, "duplicate tool name"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := validateRequestSchema(parsePayload(t, tc.body))
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("want valid, got %q", err.message)
				}
				return
			}
			if err == nil {
				t.Fatalf("want error %q, got valid", tc.wantErr)
			}
			if err.status != 400 || err.message != tc.wantErr {
				t.Fatalf("want 400 %q, got %d %q", tc.wantErr, err.status, err.message)
			}
		})
	}
}

func TestOutputTokenLimit(t *testing.T) {
	limit := func(body string) (int, *httpError) {
		return outputTokenLimit(parsePayload(t, body), 8192)
	}
	if v, err := limit(`{}`); err != nil || v != 8192 {
		t.Fatalf("absent: want 8192, got %d %v", v, err)
	}
	if v, err := limit(`{"max_tokens":100}`); err != nil || v != 100 {
		t.Fatalf("max_tokens: want 100, got %d %v", v, err)
	}
	if v, err := limit(`{"max_tokens":100,"max_completion_tokens":50}`); err != nil || v != 50 {
		t.Fatalf("aliases resolve downward: want 50, got %d %v", v, err)
	}
	if v, err := limit(`{"max_tokens":99999}`); err != nil || v != 8192 {
		t.Fatalf("over-ask clamps: want 8192, got %d %v", v, err)
	}
	if _, err := limit(`{"max_tokens":0}`); err == nil || err.message != "invalid max_tokens" {
		t.Fatalf("zero must refuse: got %v", err)
	}
	if _, err := limit(`{"max_completion_tokens":true}`); err == nil || err.message != "invalid max_completion_tokens" {
		t.Fatalf("bool must refuse: got %v", err)
	}
	if _, err := limit(`{"max_tokens":1.5}`); err == nil || err.message != "invalid max_tokens" {
		t.Fatalf("float must refuse: got %v", err)
	}
}

func TestBenchmarkReasoningForRequest(t *testing.T) {
	reason := func(body string, bench int32) (map[string]any, *httpError) {
		return benchmarkReasoningForRequest(parsePayload(t, body), v7Model, bench)
	}
	// Non-contract model: no reasoning block ever.
	if block, err := benchmarkReasoningForRequest(parsePayload(t, `{}`), "qwen/qwen3-32b", 9); block != nil || err != nil {
		t.Fatalf("non-contract model: want nil, got %v %v", block, err)
	}
	// v7/v8: pinned medium regardless of caller input.
	if block, err := reason(`{"reasoning":{"effort":"high"}}`, 8); err != nil || block["effort"] != "medium" {
		t.Fatalf("v8 pin: got %v %v", block, err)
	}
	// v9: agent-selectable.
	if block, err := reason(`{"reasoning":{"effort":"high"}}`, 9); err != nil || block["effort"] != "high" || block["exclude"] != true {
		t.Fatalf("v9 nested: got %v %v", block, err)
	}
	if block, err := reason(`{"reasoning_effort":"low"}`, 9); err != nil || block["effort"] != "low" {
		t.Fatalf("v9 flat: got %v %v", block, err)
	}
	if block, err := reason(`{}`, 9); err != nil || block["effort"] != "medium" {
		t.Fatalf("v9 default: got %v %v", block, err)
	}
	if block, err := reason(`{"reasoning":{"effort":"low","exclude":true}}`, 9); err != nil || block["effort"] != "low" {
		t.Fatalf("v9 exclude-true shape: got %v %v", block, err)
	}
	for body, want := range map[string]string{
		`{"reasoning":{"effort":"low","exclude":false}}`: "invalid reasoning",
		`{"reasoning":{"effort":"low","extra":1}}`:       "invalid reasoning",
		`{"reasoning":"low"}`:                            "invalid reasoning",
		`{"reasoning":{"effort":"max"}}`:                 "invalid reasoning effort",
		`{"reasoning_effort":"max"}`:                     "invalid reasoning_effort",
	} {
		if _, err := reason(body, 9); err == nil || err.message != want {
			t.Fatalf("%s: want %q, got %v", body, want, err)
		}
	}
	// Matching aliases are fine.
	if block, err := reason(`{"reasoning":{"effort":"low"},"reasoning_effort":"low"}`, 9); err != nil || block["effort"] != "low" {
		t.Fatalf("matching aliases: got %v %v", block, err)
	}
	// Conflicting aliases heal to the nested block instead of 400ing.
	if block, err := reason(`{"reasoning":{"effort":"low"},"reasoning_effort":"high"}`, 9); err != nil || block["effort"] != "low" {
		t.Fatalf("conflicting aliases prefer nested: got %v %v", block, err)
	}
}

func TestLockedUpstreamPayload(t *testing.T) {
	payload := parsePayload(t, `{
		"model":"wrong-model","max_tokens":50,"max_completion_tokens":40,
		"n":5,"best_of":3,"user":"u1","metadata":{"a":1},"store":true,
		"service_tier":"priority","reasoning_effort":"high",
		"temperature":0.5,
		"messages":[
			{"role":"user","content":"hi"},
			{"role":"tool","content":"r","tool_call_id":"1","name":"f"}
		]
	}`)
	upstream, herr := lockedUpstreamPayload(payload, v7Model, 40, 9)
	if herr != nil {
		t.Fatalf("locked payload: %v", herr)
	}
	if upstream["model"] != v7Model {
		t.Fatalf("model must be pinned, got %v", upstream["model"])
	}
	if upstream["max_tokens"] != 40 || upstream["n"] != 1 || upstream["stream"] != false {
		t.Fatalf("pinned values wrong: %v", upstream)
	}
	for _, gone := range []string{"user", "metadata", "store", "best_of", "service_tier", "reasoning_effort", "max_completion_tokens"} {
		if _, present := upstream[gone]; present {
			t.Fatalf("%s must be stripped", gone)
		}
	}
	reasoning := upstream["reasoning"].(map[string]any)
	if reasoning["effort"] != "high" || reasoning["exclude"] != true {
		t.Fatalf("reasoning wrong: %v", reasoning)
	}
	messages := upstream["messages"].([]any)
	toolMsg := messages[1].(map[string]any)
	if _, present := toolMsg["name"]; present {
		t.Fatalf("tool-role name must be stripped upstream")
	}
	indexed := parsePayload(t, `{
		"model":"openai/gpt-oss-20b","reasoning_effort":"medium",
		"messages":[{"role":"assistant","content":"","tool_calls":[{"id":"1","type":"function","index":0,"function":{"name":"f","arguments":"{}"}}]}]
	}`)
	indexedUp, herr := lockedUpstreamPayload(indexed, v7Model, 40, 9)
	if herr != nil {
		t.Fatalf("indexed tool_calls: %v", herr)
	}
	if _, present := indexedUp["reasoning_effort"]; present {
		t.Fatal("reasoning_effort must be stripped")
	}
	calls := indexedUp["messages"].([]any)[0].(map[string]any)["tool_calls"].([]any)
	call := calls[0].(map[string]any)
	index, present := call["index"]
	if !present {
		t.Fatal("tool_call index must be forwarded; OpenRouter accepts it")
	}
	if n, ok := index.(json.Number); !ok || n.String() != "0" {
		t.Fatalf("tool_call index = %v", index)
	}
	if _, present := messages[0].(map[string]any)["content"]; !present {
		t.Fatalf("user message must survive")
	}
	traced := parsePayload(t, `{
		"model":"openai/gpt-oss-20b","reasoning_effort":"medium",
		"messages":[{"role":"assistant","content":"","reasoning_content":"I should call search_memory.","reasoning":"I should call search_memory."}]
	}`)
	tracedUp, herr := lockedUpstreamPayload(traced, v7Model, 40, 9)
	if herr != nil {
		t.Fatalf("reasoning_content lock: %v", herr)
	}
	if _, present := tracedUp["reasoning_effort"]; present {
		t.Fatal("reasoning_effort must be stripped")
	}
	tracedMsg := tracedUp["messages"].([]any)[0].(map[string]any)
	if tracedMsg["reasoning_content"] != "I should call search_memory." {
		t.Fatalf("reasoning_content stripped: %v", tracedMsg)
	}
	if tracedMsg["reasoning"] != "I should call search_memory." {
		t.Fatalf("message reasoning stripped: %v", tracedMsg)
	}
	// The original payload must not have been mutated (tool message keeps
	// its name for the caller's view).
	original := payload["messages"].([]any)[1].(map[string]any)
	if _, present := original["name"]; !present {
		t.Fatalf("original payload mutated")
	}
	if upstream["temperature"] == nil {
		t.Fatalf("forwarded field lost")
	}
	if _, present := upstream["transforms"]; present {
		t.Fatalf("lock must not attach transforms; that is a post-lock platform pin")
	}
}

func TestAttachPlatformMiddleOut(t *testing.T) {
	small := map[string]any{"model": v7Model}
	attachPlatformMiddleOut(small, historicalChatRequestBodyBytes)
	if _, present := small["transforms"]; present {
		t.Fatalf("bodies at the historical 256 KiB cap must not get middle-out")
	}
	attachPlatformMiddleOut(small, historicalChatRequestBodyBytes+1)
	got, _ := small["transforms"].([]any)
	if len(got) != 1 || got[0] != "middle-out" {
		t.Fatalf("oversized body must get platform middle-out, got %v", small["transforms"])
	}
	attachPlatformMiddleOut(nil, historicalChatRequestBodyBytes+1)
}

func TestAdaptProviderRequestCompatibilityAvoidsGroqJSONModeWithTools(t *testing.T) {
	base := func() map[string]any {
		return map[string]any{
			"tools":           []any{map[string]any{"type": "function"}},
			"tool_choice":     "auto",
			"response_format": map[string]any{"type": "json_object"},
		}
	}

	groq := base()
	groqPreferences := providerPreferences(config.RoutingModeAdaptive, "Groq", "")
	adaptProviderRequestCompatibility(groq, groqPreferences)
	if _, present := groq["response_format"]; present {
		t.Fatal("Groq-only JSON-object mode must be removed when tools are active")
	}
	if len(groq["tools"].([]any)) != 1 || groq["tool_choice"] != "auto" {
		t.Fatalf("tool contract changed: %v", groq)
	}

	for name, tc := range map[string]struct {
		payload     map[string]any
		preferences map[string]any
	}{
		"other provider":    {base(), providerPreferences(config.RoutingModeAdaptive, "Amazon Bedrock", "")},
		"tools disabled":    {map[string]any{"tools": []any{map[string]any{"type": "function"}}, "tool_choice": "none", "response_format": map[string]any{"type": "json_object"}}, groqPreferences},
		"structured schema": {map[string]any{"tools": []any{map[string]any{"type": "function"}}, "tool_choice": "auto", "response_format": map[string]any{"type": "json_schema"}}, groqPreferences},
	} {
		t.Run(name, func(t *testing.T) {
			adaptProviderRequestCompatibility(tc.payload, tc.preferences)
			if _, present := tc.payload["response_format"]; !present {
				t.Fatalf("response format unexpectedly removed: %v", tc.payload)
			}
		})
	}

	aggregate := base()
	aggregatePreferences := providerPreferences(config.RoutingModeAggregateThroughput, "nebius", "")
	adaptProviderRequestCompatibility(aggregate, aggregatePreferences)
	if _, present := aggregate["response_format"]; !present {
		t.Fatal("aggregate route should preserve JSON-object mode")
	}
	ignored, _ := aggregatePreferences["ignore"].([]string)
	if !providerListIncludes(ignored, "groq") {
		t.Fatalf("aggregate route must exclude Groq, got %v", aggregatePreferences)
	}

	reliability := base()
	reliabilityPreferences := reliabilityProviderPreferences()
	adaptProviderRequestCompatibility(reliability, reliabilityPreferences)
	if _, present := reliability["response_format"]; !present {
		t.Fatal("reliability route should preserve JSON-object mode")
	}
	order, _ := reliabilityPreferences["order"].([]string)
	if len(order) != 1 || order[0] != "deepinfra" {
		t.Fatalf("reliability route must remove only Groq, got %v", reliabilityPreferences)
	}
}

func TestEstimatedAndChargeableTokens(t *testing.T) {
	if estimatedTokens([]byte{}) != 1 {
		t.Fatalf("empty body estimate floors at 1")
	}
	if estimatedTokens(make([]byte, 9)) != 3 {
		t.Fatalf("ceil(9/4) = 3, got %d", estimatedTokens(make([]byte, 9)))
	}
	if maxChargeableTokens([]byte{}, 10) != 11 {
		t.Fatalf("chargeable floors body at 1")
	}
	if maxChargeableTokens(make([]byte, 100), 10) != 110 {
		t.Fatalf("chargeable = output + len(body)")
	}
}

func TestValidateRequestSchemaRefusedListsEveryOffender(t *testing.T) {
	err := validateRequestSchema(parsePayload(t, `{"functions":[],"function_call":"auto",`+minimalMessages+`}`))
	if err == nil {
		t.Fatal("want refusal")
	}
	if !strings.Contains(err.message, "function_call (use tool_choice instead)") ||
		!strings.Contains(err.message, "functions (use tools instead)") {
		t.Fatalf("both offenders must be named: %q", err.message)
	}
}
