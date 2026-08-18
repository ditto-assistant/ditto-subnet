// Command localstack-modelharness is a MINIMAL model-using DittoBench harness for
// Phase-1 localstack validation. It implements the harness wire contract
// (GET /health, POST /seed, POST /run) and answers every /run by actually calling
// the locked model through the validator-provided inference route, so its answers
// genuinely DEPEND on the model — enough to drive the v12 causal model-dependence
// gate to settle with dependent cases. It is not a competitive agent: it does no
// memory retrieval or tool execution, so tool/memory quality is low; the point is
// real, model-dependent inference through the broker.
//
// Inference route: RunRequest.inference_base_url when the validator mints a
// case-scoped one (v10+), else OPENROUTER_BASE_URL. "/chat/completions" is
// appended. The broker forces the model + provider, so no key is needed on the
// source-bound loopback session.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func env(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}

var httpClient = &http.Client{Timeout: 120 * time.Second}

func main() {
	port := env("PORT", "9000")
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /seed", func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, io.LimitReader(r.Body, 64<<20))
		writeJSON(w, 200, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /run", handleRun)
	log.Printf("localstack-modelharness listening on :%s", port)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}

func handleRun(w http.ResponseWriter, r *http.Request) {
	var req protocol.RunRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, 400, map[string]string{"error": "bad run body"})
		return
	}
	base := strings.TrimRight(req.InferenceBaseURL, "/")
	if base == "" {
		base = strings.TrimRight(env("OPENROUTER_BASE_URL", "http://127.0.0.1:11436/v1/inference"), "/")
	}
	answer, pt, ot := callModel(r.Context(), base, req.SystemPrompt, req.UserInput)
	// CONSTANT_ANSWER mode: still make the (attributed) model call above so the
	// model_use gate passes, but IGNORE the model output and return a fixed
	// answer. The counterfactual then sees the answer is unchanged under model
	// ablation => 0 dependent cases => the v12 model_dependence gate fail-closes.
	if c := strings.TrimSpace(os.Getenv("CONSTANT_ANSWER")); c != "" {
		answer = c
	}
	writeJSON(w, 200, protocol.RunResponse{
		FinalText:    answer,
		Answer:       answer,
		ToolCalls:    []protocol.ObservedToolCall{},
		PromptTokens: pt,
		OutputTokens: ot,
		LatencyMs:    1,
	})
}

func callModel(ctx context.Context, base, system, user string) (string, int64, int64) {
	msgs := []map[string]string{}
	if strings.TrimSpace(system) != "" {
		msgs = append(msgs, map[string]string{"role": "system", "content": system})
	}
	msgs = append(msgs, map[string]string{"role": "user", "content": user})
	body, _ := json.Marshal(map[string]any{
		"model":       env("DITTOBENCH_MODEL", "openai/gpt-oss-20b"),
		"messages":    msgs,
		"max_tokens":  512,
		"temperature": 0,
		"stream":      false,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", 0, 0
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer local") // ignored by source-bound broker
	resp, err := httpClient.Do(req)
	if err != nil {
		log.Printf("model call error: %v", err)
		return "", 0, 0
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		log.Printf("model call status %d: %s", resp.StatusCode, string(raw[:min(len(raw), 200)]))
		return "", 0, 0
	}
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int64 `json:"prompt_tokens"`
			CompletionTokens int64 `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		return "", 0, 0
	}
	return strings.TrimSpace(out.Choices[0].Message.Content), out.Usage.PromptTokens, out.Usage.CompletionTokens
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
