// Command localstack-relay is a LOCAL-DEV model-pinning gateway that stands in
// for the validator's :11434 model-relay so a scored bench run can be exercised
// end to end on one workstation with real OpenRouter inference.
//
// It is NOT a production/consensus component. The certified relay is
// compat/model-relay/cmd/model-relay, which is code-frozen to the pre-v7 locked
// model (qwen/qwen3-32b) and the OpenRouterRelayProfileRevision. A scored bench
// v7+ run rejects that identity: relay_preflight.go:requireTokenAccounting
// requires Model == llm.V7HarnessModel and, for bench_version >= 9, the route
// profile == llm.V9AggregateProfileRevision. This binary reports exactly that
// identity (imported from internal/llm so it can never drift from the scorer),
// pins every upstream request to the locked v7+ model, forwards to real
// OpenRouter, and serves the /health accounting the scored path reads at run
// start and run end.
//
// Env:
//   - PORT                 listen port (default 11434, the gateway port the
//     scorer's sessionless path expects via HARNESS_GATEWAY_URL)
//   - OPENROUTER_API_KEY   upstream bearer key (required unless RELAY_STUB=1)
//   - OPENROUTER_BASE_URL  upstream base (default https://openrouter.ai/api/v1;
//     "/chat/completions" is appended)
//   - RELAY_STUB           "1" answers the model-relay probe with a canned
//     completion and never calls OpenRouter — a $0 mode for
//     the refharness plumbing smoke. Token counters still
//     move so /health accounting stays well-formed.
//
// The key is read from the environment only and is never logged.
package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
)

const (
	maxBody         = 4 << 20
	maxResponseBody = 16 << 20
)

type relay struct {
	upstream string
	apiKey   string
	model    string
	stub     bool
	client   *http.Client

	requests               atomic.Uint64
	successes              atomic.Uint64
	infrastructureFailures atomic.Uint64
	callerCancellations    atomic.Uint64
	upstreamAttempts       atomic.Uint64
	usageAvailable         atomic.Uint64
	usageUnavailable       atomic.Uint64
	promptTokens           atomic.Uint64
	promptBytes            atomic.Uint64
	completionTokens       atomic.Uint64
	providerLatencyMs      atomic.Uint64
}

// relayHealth mirrors the wire shape the scorer decodes in
// relay_preflight.go:readRelayHealth (relayHealthSnapshot). AccountingVersion 2,
// the immutable provider identity, and the locked model/profile are the fields
// requireTokenAccounting inspects for a bench v7+/v9+ run.
type relayHealth struct {
	AccountingVersion      int    `json:"accounting_version"`
	Status                 string `json:"status"`
	Requests               uint64 `json:"requests"`
	Successes              uint64 `json:"successes"`
	InfrastructureFailures uint64 `json:"infrastructure_failures"`
	CallerCancellations    uint64 `json:"caller_cancellations"`
	UpstreamAttempts       uint64 `json:"upstream_attempts"`
	Provider               string `json:"provider"`
	ProfileRevision        string `json:"profile_revision"`
	Model                  string `json:"model"`
	UsageAvailable         uint64 `json:"usage_available"`
	UsageUnavailable       uint64 `json:"usage_unavailable"`
	PromptTokens           uint64 `json:"prompt_tokens"`
	PromptBytes            uint64 `json:"prompt_bytes"`
	CompletionTokens       uint64 `json:"completion_tokens"`
	ProviderLatencyMs      uint64 `json:"provider_latency_ms"`
	TTFTStatus             string `json:"ttft_status"`
}

type providerUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
}

func main() {
	port := envOr("PORT", "11434")
	base := strings.TrimRight(envOr("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"), "/")
	r := &relay{
		upstream: base + "/chat/completions",
		apiKey:   strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY")),
		model:    llm.V7HarnessModel,
		stub:     strings.TrimSpace(os.Getenv("RELAY_STUB")) == "1",
		client:   &http.Client{Timeout: 300 * time.Second},
	}
	if !r.stub && r.apiKey == "" {
		log.Fatal("OPENROUTER_API_KEY is required (or set RELAY_STUB=1 for the free plumbing smoke)")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/chat/completions", r.handle)
	mux.HandleFunc("POST /chat/completions", r.handle)
	mux.HandleFunc("GET /health", r.health)
	addr := ":" + port
	mode := "openrouter"
	if r.stub {
		mode = "STUB (no upstream calls)"
	}
	log.Printf("localstack-relay on %s -> %s (model pinned to %s, profile %s, %s)",
		addr, r.upstream, r.model, llm.V9AggregateProfileRevision, mode)
	ln, err := net.Listen("tcp4", "0.0.0.0"+addr)
	if err != nil {
		log.Fatalf("listen %s: %v", addr, err)
	}
	log.Fatal(http.Serve(ln, mux))
}

func (r *relay) handle(w http.ResponseWriter, req *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(req.Body, maxBody))
	if err != nil {
		http.Error(w, "read body", http.StatusBadRequest)
		return
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		http.Error(w, "body must be a JSON object", http.StatusBadRequest)
		return
	}
	// The pin: the upstream always sees the locked model and a non-streaming
	// request, regardless of what the harness asked for.
	body["model"] = r.model
	body["stream"] = false
	out, err := json.Marshal(body)
	if err != nil {
		http.Error(w, "marshal body", http.StatusInternalServerError)
		return
	}
	r.promptBytes.Add(uint64(len(out)))
	r.requests.Add(1)

	if r.stub {
		r.upstreamAttempts.Add(1)
		r.successes.Add(1)
		r.usageAvailable.Add(1)
		r.promptTokens.Add(8)
		r.completionTokens.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"id":      "localstack-stub",
			"object":  "chat.completion",
			"model":   r.model,
			"choices": []any{map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": "OK"}, "finish_reason": "stop"}},
			"usage":   map[string]any{"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
		})
		return
	}

	r.upstreamAttempts.Add(1)
	up, err := http.NewRequestWithContext(req.Context(), http.MethodPost, r.upstream, bytes.NewReader(out))
	if err != nil {
		http.Error(w, "build upstream request", http.StatusInternalServerError)
		return
	}
	up.Header.Set("Content-Type", "application/json")
	up.Header.Set("Authorization", "Bearer "+r.apiKey) // never the sandbox's header
	started := time.Now()
	resp, err := r.client.Do(up)
	r.providerLatencyMs.Add(uint64(time.Since(started).Milliseconds()))
	if err != nil {
		r.infrastructureFailures.Add(1)
		http.Error(w, "upstream unreachable", http.StatusBadGateway)
		return
	}
	responseBody, rerr := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody))
	resp.Body.Close()
	if rerr != nil {
		r.infrastructureFailures.Add(1)
		http.Error(w, "read upstream response", http.StatusBadGateway)
		return
	}
	if resp.StatusCode >= http.StatusOK && resp.StatusCode < http.StatusMultipleChoices {
		var completion struct {
			Choices []json.RawMessage `json:"choices"`
			Usage   *providerUsage    `json:"usage"`
		}
		if err := json.Unmarshal(responseBody, &completion); err != nil || len(completion.Choices) == 0 {
			r.infrastructureFailures.Add(1)
		} else {
			r.successes.Add(1)
			if completion.Usage != nil && completion.Usage.PromptTokens >= 0 && completion.Usage.CompletionTokens >= 0 {
				r.usageAvailable.Add(1)
				r.promptTokens.Add(uint64(completion.Usage.PromptTokens))
				r.completionTokens.Add(uint64(completion.Usage.CompletionTokens))
			} else {
				r.usageUnavailable.Add(1)
			}
		}
	} else {
		r.infrastructureFailures.Add(1)
	}
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(responseBody)
}

func (r *relay) health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(relayHealth{
		AccountingVersion:      2,
		Status:                 "ok",
		Requests:               r.requests.Load(),
		Successes:              r.successes.Load(),
		InfrastructureFailures: r.infrastructureFailures.Load(),
		CallerCancellations:    r.callerCancellations.Load(),
		UpstreamAttempts:       r.upstreamAttempts.Load(),
		Provider:               "openrouter",
		ProfileRevision:        llm.V9AggregateProfileRevision,
		Model:                  r.model,
		UsageAvailable:         r.usageAvailable.Load(),
		UsageUnavailable:       r.usageUnavailable.Load(),
		PromptTokens:           r.promptTokens.Load(),
		PromptBytes:            r.promptBytes.Load(),
		CompletionTokens:       r.completionTokens.Load(),
		ProviderLatencyMs:      r.providerLatencyMs.Load(),
		TTFTStatus:             "unavailable_non_streaming",
	})
}

func envOr(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}
