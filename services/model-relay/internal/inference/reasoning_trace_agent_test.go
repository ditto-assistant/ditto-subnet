package inference

import (
	"encoding/json"
	"testing"
)

const reasoningTraceText = "I should call search_memory."

// crownRecallPayload mirrors apps/platform/.../harnesses/reasoning_trace_agent.py:
// Crown/rig-core CompletionsClient recall with assistant reasoning_content.
func crownRecallPayload() map[string]any {
	return map[string]any{
		"model": v7Model,
		"messages": []any{
			map[string]any{"role": "system", "content": "You are a retrieval agent. Reply with ok."},
			map[string]any{"role": "user", "content": "Search memory for ok, then reply with ok."},
			map[string]any{
				"role":              "assistant",
				"content":           "",
				"reasoning_content": reasoningTraceText,
				"tool_calls":        crownToolCalls(),
			},
			map[string]any{
				"role":         "tool",
				"tool_call_id": "call_search_memory_1",
				"name":         "search_memory",
				"content":      "ok",
			},
			map[string]any{"role": "user", "content": "Reply with the single word ok."},
		},
		"tools":            crownTools(),
		"seed":             1,
		"temperature":      0,
		"max_tokens":       8,
		"reasoning_effort": "medium",
	}
}

func crownToolCalls() []any {
	return []any{
		map[string]any{
			"id":    "call_search_memory_1",
			"type":  "function",
			"index": 0,
			"function": map[string]any{
				"name":      "search_memory",
				"arguments": `{"query":"ok"}`,
			},
		},
	}
}

func crownTools() []any {
	return []any{
		map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        "search_memory",
				"description": "Search long-term memory.",
				"parameters": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"query": map[string]any{"type": "string"},
					},
					"required": []any{"query"},
				},
			},
		},
	}
}

func TestCrownReasoningContentIsForwardedNotStripped(t *testing.T) {
	payload := asSchemaPayloadFromMap(t, crownRecallPayload())
	if err := validateRequestSchema(payload); err != nil {
		t.Fatalf("schema: %v", err)
	}
	upstream, herr := lockedUpstreamPayload(payload, v7Model, 8, 9)
	if herr != nil {
		t.Fatalf("lock: %v", herr)
	}
	if _, present := upstream["reasoning_effort"]; present {
		t.Fatal("reasoning_effort leaked")
	}
	reasoning, _ := upstream["reasoning"].(map[string]any)
	if reasoning["effort"] != "medium" || reasoning["exclude"] != true {
		t.Fatalf("reasoning=%v", upstream["reasoning"])
	}
	messages, _ := upstream["messages"].([]any)
	assistant, _ := messages[2].(map[string]any)
	if assistant["reasoning_content"] != reasoningTraceText {
		t.Fatalf("reasoning_content stripped: %v", assistant)
	}
	calls, _ := assistant["tool_calls"].([]any)
	call, _ := calls[0].(map[string]any)
	index, ok := call["index"].(json.Number)
	if !ok || index.String() != "0" {
		t.Fatalf("tool_call index stripped: %v", call)
	}
	tool, _ := messages[3].(map[string]any)
	if _, present := tool["name"]; present {
		t.Fatalf("tool name leaked: %v", tool)
	}
}

func TestConflictingRequestAliasesKeepReasoningContent(t *testing.T) {
	payload := asSchemaPayloadFromMap(t, crownRecallPayload())
	payload["reasoning"] = map[string]any{"effort": "high"}
	payload["reasoning_effort"] = "low"
	if err := validateRequestSchema(payload); err != nil {
		t.Fatalf("schema: %v", err)
	}
	upstream, herr := lockedUpstreamPayload(payload, v7Model, 8, 9)
	if herr != nil {
		t.Fatalf("lock: %v", herr)
	}
	if _, present := upstream["reasoning_effort"]; present {
		t.Fatal("reasoning_effort leaked")
	}
	reasoning, _ := upstream["reasoning"].(map[string]any)
	if reasoning["effort"] != "high" || reasoning["exclude"] != true {
		t.Fatalf("reasoning=%v", upstream["reasoning"])
	}
	assistant, _ := upstream["messages"].([]any)[2].(map[string]any)
	if assistant["reasoning_content"] != reasoningTraceText {
		t.Fatalf("reasoning_content stripped: %v", assistant)
	}
}

func asSchemaPayloadFromMap(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal schema payload: %v", err)
	}
	return parsePayload(t, string(raw))
}
