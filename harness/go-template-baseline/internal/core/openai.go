// Package core implements the baseline DittoCore handler.
//
// The handler forwards each challenge's prompt + tool schemas to an
// OpenAI-compatible chat completions endpoint and maps the model's
// emitted tool_calls directly to MinerResponse.ToolCalls. There is no
// argument cleanup, no retry, and no caching; this is the floor a
// real miner should beat, not a competitive submission.
//
// Environment variables:
//
//   - OPENAI_API_KEY: bearer token (required; an empty key yields a
//     refusal so the validator can still score the case as zero).
//   - OPENAI_BASE_URL: defaults to https://api.openai.com/v1; point at
//     a self-hosted compatible server (e.g. vLLM, llama.cpp) to run
//     the baseline without OpenAI credentials.
//   - OPENAI_MODEL: defaults to gpt-4o-mini.
package core

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// OpenAIHandler is the baseline DittoCore handler.
type OpenAIHandler struct {
	APIKey  string
	BaseURL string
	Model   string
	HTTP    *http.Client
}

// NewOpenAIHandler reads the OPENAI_* env vars and constructs a handler.
// The handler is safe to use without a key — Handle returns a refusal
// when APIKey is empty so the harness still runs end-to-end against the
// validator without leaking expensive API calls during testing.
func NewOpenAIHandler() *OpenAIHandler {
	h := &OpenAIHandler{
		APIKey:  os.Getenv("OPENAI_API_KEY"),
		BaseURL: env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
		Model:   env("OPENAI_MODEL", "gpt-4o-mini"),
		HTTP:    &http.Client{Timeout: 30 * time.Second},
	}
	return h
}

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// Handle runs one DittoCore challenge against the configured chat
// completions endpoint and returns the model's tool_calls verbatim.
func (h *OpenAIHandler) Handle(ctx context.Context, req bittensor.ChallengeRequest) (bittensor.MinerResponse, error) {
	if h.APIKey == "" {
		return bittensor.MinerResponse{Refusal: "missing_api_key"}, nil
	}

	body := chatCompletionRequest{
		Model: h.Model,
		Messages: []chatMessage{
			{Role: "user", Content: req.Prompt},
		},
		Tools: toolsForRequest(req.ToolSchemas),
	}
	buf, err := json.Marshal(body)
	if err != nil {
		return bittensor.MinerResponse{}, fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost,
		h.BaseURL+"/chat/completions", bytes.NewReader(buf))
	if err != nil {
		return bittensor.MinerResponse{}, err
	}
	httpReq.Header.Set("Authorization", "Bearer "+h.APIKey)
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")

	resp, err := h.HTTP.Do(httpReq)
	if err != nil {
		return bittensor.MinerResponse{Refusal: "upstream_error"}, nil
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Soak the body so callers can debug from stderr without
		// blocking on a slow stream.
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return bittensor.MinerResponse{Refusal: fmt.Sprintf("upstream_status_%d", resp.StatusCode)}, nil
	}
	var parsed chatCompletionResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return bittensor.MinerResponse{Refusal: "upstream_decode"}, nil
	}

	out := bittensor.MinerResponse{
		FinalAnswer: choiceText(parsed),
	}
	for hop, tc := range choiceToolCalls(parsed) {
		out.ToolCalls = append(out.ToolCalls, bittensor.ToolCall{
			Hop:  hop + 1,
			Name: tc.Function.Name,
			Args: tc.Function.Arguments,
		})
	}
	return out, nil
}

func choiceText(r chatCompletionResponse) string {
	if len(r.Choices) == 0 {
		return ""
	}
	return r.Choices[0].Message.Content
}

func choiceToolCalls(r chatCompletionResponse) []toolCallObject {
	if len(r.Choices) == 0 {
		return nil
	}
	return r.Choices[0].Message.ToolCalls
}

func toolsForRequest(schemas []bittensor.ToolSchema) []toolObject {
	if len(schemas) == 0 {
		return nil
	}
	out := make([]toolObject, len(schemas))
	for i, s := range schemas {
		out[i] = toolObject{
			Type: "function",
			Function: toolFunction{
				Name:        s.Name,
				Description: s.Description,
				Parameters:  s.Parameters,
			},
		}
	}
	return out
}

// ─── OpenAI chat completions wire types ──────────────────────────────────────

type chatCompletionRequest struct {
	Model    string        `json:"model"`
	Messages []chatMessage `json:"messages"`
	Tools    []toolObject  `json:"tools,omitempty"`
}

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type toolObject struct {
	Type     string       `json:"type"`
	Function toolFunction `json:"function"`
}

type toolFunction struct {
	Name        string         `json:"name"`
	Description string         `json:"description,omitempty"`
	Parameters  map[string]any `json:"parameters,omitempty"`
}

type chatCompletionResponse struct {
	Choices []struct {
		Message struct {
			Content   string           `json:"content"`
			ToolCalls []toolCallObject `json:"tool_calls"`
		} `json:"message"`
	} `json:"choices"`
}

type toolCallObject struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}
