//go:build openrouter

package inference

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

// Live OpenRouter proofs for grandmaster-like chat shapes.
//
//	cd services/model-relay
//	go test -tags openrouter ./internal/inference -run OpenRouter -count=1
const (
	openrouterChatURL = "https://openrouter.ai/api/v1/chat/completions"
	openrouterModel   = "openai/gpt-oss-20b"
)

func openrouterAPIKey(t *testing.T) string {
	t.Helper()
	key := strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	if key == "" {
		t.Skip("OPENROUTER_API_KEY is required")
	}
	return key
}

func grandmasterMessages() []any {
	return []any{
		map[string]any{"role": "system", "content": "You are a retrieval agent. Reply with ok."},
		map[string]any{"role": "user", "content": "Search memory for ok, then reply with ok."},
		map[string]any{
			"role":      "assistant",
			"content":   "",
			"reasoning": "I should call search_memory.",
			"tool_calls": []any{
				map[string]any{
					"id":    "call_search_memory_1",
					"type":  "function",
					"index": 0,
					"function": map[string]any{
						"name":      "search_memory",
						"arguments": `{"query":"ok"}`,
					},
				},
			},
		},
		map[string]any{
			"role":         "tool",
			"tool_call_id": "call_search_memory_1",
			"name":         "search_memory",
			"content":      "ok",
		},
		map[string]any{"role": "user", "content": "Reply with the single word ok."},
	}
}

func grandmasterTools() []any {
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

func grandmasterPayload() map[string]any {
	return map[string]any{
		"model":            openrouterModel,
		"messages":         grandmasterMessages(),
		"tools":            grandmasterTools(),
		"temperature":      0,
		"max_tokens":       8,
		"reasoning_effort": "medium",
	}
}

func asSchemaPayload(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal schema payload: %v", err)
	}
	return parsePayload(t, string(raw))
}

func postOpenRouter(t *testing.T, payload map[string]any) (int, string) {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	req, err := http.NewRequest(http.MethodPost, openrouterChatURL, bytes.NewReader(body))
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Authorization", "Bearer "+openrouterAPIKey(t))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("HTTP-Referer", "https://heyditto.ai/")
	req.Header.Set("X-OpenRouter-Title", "Ditto")
	client := &http.Client{Timeout: 60 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("openrouter transport: %v", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		t.Fatalf("read body: %v", err)
	}
	var decoded map[string]any
	message := ""
	if json.Unmarshal(raw, &decoded) == nil {
		if errObj, ok := decoded["error"].(map[string]any); ok {
			if text, ok := errObj["message"].(string); ok {
				message = text
			}
		}
	}
	return resp.StatusCode, message
}

func TestOpenRouterRejectsConflictingReasoningAliases(t *testing.T) {
	status, message := postOpenRouter(t, map[string]any{
		"model":            openrouterModel,
		"messages":         []any{map[string]any{"role": "user", "content": "Reply with ok."}},
		"temperature":      0,
		"max_tokens":       8,
		"reasoning":        map[string]any{"effort": "high"},
		"reasoning_effort": "low",
	})
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d message=%q", status, message)
	}
	if !strings.Contains(message, "reasoning_effort") ||
		!strings.Contains(message, "reasoning.effort") ||
		!strings.Contains(strings.ToLower(message), "conflicting") {
		t.Fatalf("unexpected 400 message: %q", message)
	}
}

func TestOpenRouterAcceptsGrandmasterEchoPayload(t *testing.T) {
	status, message := postOpenRouter(t, grandmasterPayload())
	if status != http.StatusOK {
		t.Fatalf("status=%d message=%q", status, message)
	}
}

func TestOpenRouterAcceptsLockedNestedReasoning(t *testing.T) {
	status, message := postOpenRouter(t, map[string]any{
		"model":       openrouterModel,
		"messages":    []any{map[string]any{"role": "user", "content": "Reply with ok."}},
		"temperature": 0,
		"max_tokens":  8,
		"n":           1,
		"stream":      false,
		"reasoning":   map[string]any{"effort": "medium", "exclude": true},
	})
	if status != http.StatusOK {
		t.Fatalf("status=%d message=%q", status, message)
	}
}

func TestOpenRouterAcceptsHealedConflict(t *testing.T) {
	payload := asSchemaPayload(t, map[string]any{
		"model":            openrouterModel,
		"messages":         []any{map[string]any{"role": "user", "content": "Reply with ok."}},
		"temperature":      0,
		"max_tokens":       8,
		"reasoning":        map[string]any{"effort": "high"},
		"reasoning_effort": "low",
	})
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
	status, message := postOpenRouter(t, upstream)
	if status != http.StatusOK {
		t.Fatalf("status=%d message=%q", status, message)
	}
}

func TestOpenRouterAcceptsHealedGrandmasterPayload(t *testing.T) {
	payload := asSchemaPayload(t, grandmasterPayload())
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
	messages, _ := upstream["messages"].([]any)
	assistant, _ := messages[2].(map[string]any)
	if assistant["reasoning"] != "I should call search_memory." {
		t.Fatalf("assistant extras stripped: %v", assistant)
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
	status, message := postOpenRouter(t, upstream)
	if status != http.StatusOK {
		t.Fatalf("status=%d message=%q", status, message)
	}
}
