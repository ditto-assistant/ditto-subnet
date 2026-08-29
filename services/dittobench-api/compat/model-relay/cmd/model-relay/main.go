// model-relay is the model-pinning gateway for validators that back the model
// lock with a hosted provider instead of local GPUs. It terminates the
// sandbox's OpenAI-compatible chat requests locally, FORCES the model field to
// the locked id, injects the upstream API key, and forwards to the upstream.
// The sandbox never holds the key and cannot choose the model, so the lock's
// semantics are identical to a local Ollama/vLLM gateway. The egress side stays
// fail-closed: the sandbox reaches only this relay (host.docker.internal), and
// the relay is the only process that reaches the upstream.
//
// This binary is rollback-only after platform ticket inference is enabled.
// OpenRouter/Nebius is the default compatibility profile. The historical
// Chutes profile remains code-frozen but disabled unless an operator explicitly
// enables the bounded transition escape hatch.
//
// Each profile is CODE-FROZEN: RELAY_PROVIDER only chooses which certified
// profile runs, never what it pins (upstream, exact model id, serving-provider
// routing, thinking mode). All of those are consensus-critical constants a
// hybrid-reasoning model must share fleet-wide; change them in code (a
// network-wide change), then redeploy. There is deliberately no upstream-URL
// override: the pin is enforced in code, not left to a validator's env.
//
// Env (deployment only):
//   - RELAY_DISABLED  "1" starts a no-provider compatibility stub
//   - RELAY_PROVIDER  "openrouter" (default); "chutes" is soft-deprecated
//   - RELAY_ENABLE_DEPRECATED_CHUTES  explicit "1" needed for the old profile
//   - RELAY_API_KEY   upstream bearer key for the selected provider (required)
//   - PORT            listen port (default 11434, the gateway port the sandbox
//     already expects)
//
// Provider calls are single-shot. A failed benchmark must be manually retried.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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

// providerProfile is one code-frozen upstream the relay may pin to. Every
// field is consensus-critical and therefore a constant: RELAY_PROVIDER only
// chooses WHICH profile runs, never what a profile pins.
type providerProfile struct {
	name     string
	revision string
	upstream string
	model    string
	// pinBody applies provider-specific consensus pins beyond the model id.
	pinBody func(body map[string]any)
}

// providers are the certified profiles for the locked Qwen3-32B. Chutes serves
// the hardware-attested TEE deployment; OpenRouter serves the same weights with
// routing locked to the certified Nebius deployment (the throughput evidence
// behind the certification measured that exact provider, and free routing would
// un-pin the scored backend).
var providers = map[string]providerProfile{
	"chutes": {
		name:     "chutes",
		revision: llm.ChutesRelayProfileRevision,
		upstream: "https://llm.chutes.ai/v1/chat/completions",
		model:    llm.LockedUpstreamModel,
	},
	"openrouter": {
		name:     "openrouter",
		revision: llm.OpenRouterRelayProfileRevision,
		upstream: "https://openrouter.ai/api/v1/chat/completions",
		model:    llm.LockedHarnessModel,
		pinBody: func(body map[string]any) {
			body["provider"] = map[string]any{
				"only":            []string{"nebius"},
				"allow_fallbacks": false,
				"data_collection": "deny",
				"zdr":             true,
			}
			appendNoThink(body)
		},
	},
}

// appendNoThink applies Qwen3's soft thinking switch on the openrouter
// profile. Nebius via OpenRouter ignores chat_template_kwargs.enable_thinking
// and OpenRouter's reasoning parameter (verified live 2026-07-17: reasoning
// tokens still arrive with both set), so the hard switch the Chutes profile
// relies on does not exist here; the documented Qwen3 soft switch does. The
// LATEST /think|/no_think directive in the conversation wins, so the relay
// appends /no_think after everything the sandbox wrote — a sandbox-supplied
// "/think" can never come later. On Chutes the hard template switch makes
// soft directives inert, so this pin is openrouter-only.
func appendNoThink(body map[string]any) {
	msgs, _ := body["messages"].([]any)
	if len(msgs) == 0 {
		body["messages"] = []any{map[string]any{"role": "system", "content": "/no_think"}}
		return
	}
	last, _ := msgs[len(msgs)-1].(map[string]any)
	if last == nil {
		return
	}
	switch c := last["content"].(type) {
	case string:
		last["content"] = c + "\n/no_think"
	case []any:
		last["content"] = append(c, map[string]any{"type": "text", "text": "/no_think"})
	default:
		body["messages"] = append(msgs, map[string]any{"role": "user", "content": "/no_think"})
	}
}

// lockedThinking is the frozen fleet-wide thinking mode. Off: a hybrid-reasoning
// model (Qwen3) must not pick per request, and off keeps replies inside per-case
// budgets. Consensus-critical, so it is a code constant, not env-tunable.
const lockedThinking = false

// maxBody bounds a relayed request body; chat requests are prompts, not blobs.
const maxBody = 4 << 20

// maxResponseBody bounds the upstream completion before it is inspected and
// forwarded. The locked model is non-streaming and benchmark replies are small;
// a response above this limit is an upstream protocol failure, not a usable
// completion.
const maxResponseBody = 16 << 20

// maxUsageTokensPerCompletion rejects telemetry that is clearly corrupt before
// it can poison a run total. It is deliberately far above every case budget.
const maxUsageTokensPerCompletion = 10_000_000

type relay struct {
	provider               string
	profileRevision        string
	upstream               string
	apiKey                 string
	model                  string
	thinking               bool
	pinBody                func(body map[string]any)
	client                 *http.Client
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

func main() {
	addr := ":" + envOr("PORT", "11434")
	if strings.TrimSpace(os.Getenv("RELAY_DISABLED")) == "1" {
		mux := http.NewServeMux()
		mux.HandleFunc("/", disabledHandler)
		serveRelay(addr, mux, "model-relay compatibility stub disabled")
		return
	}
	providerName := envOr("RELAY_PROVIDER", "openrouter")
	if providerName == "chutes" && strings.TrimSpace(os.Getenv("RELAY_ENABLE_DEPRECATED_CHUTES")) != "1" {
		log.Fatal("the Chutes relay profile is disabled; use platform ticket inference")
	}
	profile, ok := providers[providerName]
	if !ok {
		log.Fatalf("RELAY_PROVIDER %q is not a certified profile (chutes|openrouter)", providerName)
	}
	r := &relay{
		provider:        profile.name,
		profileRevision: profile.revision,
		upstream:        profile.upstream,
		apiKey:          strings.TrimSpace(os.Getenv("RELAY_API_KEY")),
		model:           profile.model,
		thinking:        lockedThinking,
		pinBody:         profile.pinBody,
		client:          &http.Client{Timeout: 300 * time.Second},
	}
	if r.apiKey == "" {
		log.Fatal("RELAY_API_KEY is required")
	}
	mux := http.NewServeMux()
	// Both the bare and /v1 chat-completions paths, so OLLAMA_BASE_URL-style
	// and OPENAI_BASE_URL-style clients work unchanged.
	mux.HandleFunc("POST /v1/chat/completions", r.handle)
	mux.HandleFunc("POST /chat/completions", r.handle)
	mux.HandleFunc("GET /health", r.health)
	log.Printf("model-relay on %s -> %s (model pinned to %s)", addr, r.upstream, r.model)
	serveRelay(addr, mux, "")
}

func disabledHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusGone)
	_, _ = w.Write([]byte(`{"status":"disabled"}`))
}

func serveRelay(addr string, handler http.Handler, message string) {
	if message != "" {
		log.Printf("%s on %s", message, addr)
	}
	// Bind IPv4 explicitly. The relay is the gateway a sandboxed harness reaches via
	// host.docker.internal (Docker Desktop's IPv4 host-gateway); a Go dual-stack
	// "[::]" listener is not reachable that way on Docker Desktop/WSL2, so the
	// harness's chat calls fail before reaching the relay. Docker networking is
	// IPv4, so tcp4 loses nothing.
	ln, err := net.Listen("tcp4", "0.0.0.0"+addr)
	if err != nil {
		log.Fatalf("listen %s: %v", addr, err)
	}
	log.Fatal(http.Serve(ln, handler))
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
	// The pin: whatever the sandbox asked for, the upstream sees the locked
	// model, a non-streaming request (one JSON body back), and one locked
	// thinking mode (a hybrid-reasoning model must not pick per request).
	body["model"] = r.model
	body["stream"] = false
	ctk, _ := body["chat_template_kwargs"].(map[string]any)
	if ctk == nil {
		ctk = map[string]any{}
	}
	ctk["enable_thinking"] = r.thinking
	body["chat_template_kwargs"] = ctk
	if r.pinBody != nil {
		r.pinBody(body)
	}
	out, err := json.Marshal(body)
	if err != nil {
		http.Error(w, "marshal body", http.StatusInternalServerError)
		return
	}
	r.promptBytes.Add(uint64(len(out)))
	r.requests.Add(1)

	// Every provider outcome is terminal. Only an operator may issue another
	// scored attempt after this request returns.
	{
		r.upstreamAttempts.Add(1)
		up, err := http.NewRequestWithContext(req.Context(), http.MethodPost, r.upstream, bytes.NewReader(out))
		if err != nil {
			http.Error(w, "build upstream request", http.StatusInternalServerError)
			return
		}
		up.Header.Set("Content-Type", "application/json")
		up.Header.Set("Authorization", "Bearer "+r.apiKey) // never the sandbox's header

		providerStarted := time.Now()
		resp, err := r.client.Do(up)
		r.providerLatencyMs.Add(uint64(time.Since(providerStarted).Milliseconds()))
		if err != nil {
			// The caller (sandbox/harness) abandoned this call — an early exit
			// or run teardown cancelled the request context. That is the
			// harness's own decision, NOT a provider failure: there is nobody
			// left to answer and nothing to retry, so return without touching
			// infrastructure_failures. Counting it would let a benign
			// cancellation trip the fail-closed relay-health check and kill an
			// otherwise-healthy run. A context that expired because the upstream
			// was genuinely too slow surfaces as context.DeadlineExceeded, which
			// is a real infrastructure failure and flows through the
			// normal path below.
			if callerCanceled(req, err) {
				r.callerCancellations.Add(1)
				log.Print("model-relay: caller cancelled upstream call; not counted as an infrastructure failure")
				return
			}
			// Transport/connection failures park the scored attempt.
			r.infrastructureFailures.Add(1)
			http.Error(w, "upstream unreachable", http.StatusBadGateway)
			return
		}
		responseBody, rerr := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody+1))
		resp.Body.Close()
		if rerr != nil {
			// The caller can also disappear after headers arrive but while the
			// response body is being read. Treat that exactly like a transport-time
			// caller cancellation: no provider outcome or usage was received, and
			// the abandoned call must not taint an otherwise complete run.
			if callerCanceled(req, rerr) {
				r.callerCancellations.Add(1)
				log.Print("model-relay: caller cancelled response body read; not counted as an infrastructure failure")
				return
			}
			// A truncated body is ambiguous and therefore terminal.
			r.infrastructureFailures.Add(1)
			http.Error(w, "read upstream response", http.StatusBadGateway)
			return
		}
		if len(responseBody) > maxResponseBody {
			// Oversized reply is a deterministic protocol failure, not transient.
			r.infrastructureFailures.Add(1)
			http.Error(w, "upstream response too large", http.StatusBadGateway)
			return
		}
		if isTransientInfrastructureStatus(resp.StatusCode) {
			// 408/429/5xx park immediately after this one provider dispatch.
			r.infrastructureFailures.Add(1)
			r.forward(w, resp, responseBody)
			return
		}
		// Terminal outcomes (never retried): a deterministic client error
		// (401/403 count as infra as before, other 4xx pass straight through),
		// or a 2xx that is a real completion (success) or a malformed one (infra).
		if isInfrastructureStatus(resp.StatusCode) {
			r.infrastructureFailures.Add(1)
		} else if resp.StatusCode >= http.StatusOK && resp.StatusCode < http.StatusMultipleChoices {
			var completion struct {
				Choices []json.RawMessage `json:"choices"`
				Usage   *providerUsage    `json:"usage"`
			}
			if err := json.Unmarshal(responseBody, &completion); err != nil || len(completion.Choices) == 0 {
				r.infrastructureFailures.Add(1)
			} else {
				r.successes.Add(1)
				if validProviderUsage(completion.Usage) {
					r.usageAvailable.Add(1)
					r.promptTokens.Add(uint64(completion.Usage.PromptTokens))
					r.completionTokens.Add(uint64(completion.Usage.CompletionTokens))
				} else {
					r.usageUnavailable.Add(1)
				}
			}
		}
		r.forward(w, resp, responseBody)
		return
	}
}

func callerCanceled(req *http.Request, err error) bool {
	return errors.Is(err, context.Canceled) && errors.Is(req.Context().Err(), context.Canceled)
}

// forward relays the upstream status, Content-Type, and body to the sandbox.
func (r *relay) forward(w http.ResponseWriter, resp *http.Response, body []byte) {
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(body)
}

type providerUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
}

func validProviderUsage(usage *providerUsage) bool {
	if usage == nil || usage.PromptTokens < 0 || usage.CompletionTokens < 0 ||
		usage.PromptTokens > maxUsageTokensPerCompletion || usage.CompletionTokens > maxUsageTokensPerCompletion {
		return false
	}
	sum := usage.PromptTokens + usage.CompletionTokens
	if sum == 0 || sum > maxUsageTokensPerCompletion {
		return false
	}
	return usage.TotalTokens == 0 || usage.TotalTokens == sum
}

func isInfrastructureStatus(status int) bool {
	switch status {
	case http.StatusUnauthorized, http.StatusForbidden, http.StatusRequestTimeout, http.StatusTooManyRequests:
		return true
	default:
		return status >= http.StatusInternalServerError
	}
}

// isTransientInfrastructureStatus identifies the statuses that used to be
// retried. They now remain useful only for failure accounting; all are terminal.
func isTransientInfrastructureStatus(status int) bool {
	switch status {
	case http.StatusRequestTimeout, http.StatusTooManyRequests:
		return true
	default:
		return status >= http.StatusInternalServerError
	}
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
		Provider:               r.provider,
		ProfileRevision:        r.profileRevision,
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
