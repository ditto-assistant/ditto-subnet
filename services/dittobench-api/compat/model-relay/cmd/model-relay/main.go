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
// Retry env (deployment-tunable, transport-layer only — never touches the pins):
// a transient upstream failure (dropped connection, read error, 408/429/5xx) is
// retried with capped exponential backoff+jitter before it is ever counted as an
// infrastructure_failure, so a flaky provider does not discard whole runs.
//   - RELAY_RETRY_MAX_ATTEMPTS  total upstream attempts incl. the first (default 6)
//   - RELAY_RETRY_BASE_MS       first backoff delay in ms (default 200)
//   - RELAY_RETRY_CAP_MS        ceiling on any single backoff in ms (default 2000)
//   - RELAY_RETRY_FACTOR        exponential growth per attempt (default 2)
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/pprofserver"
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

// retryConfig bounds how aggressively the relay re-sends a transiently-failed
// upstream request before it counts as an infrastructure_failure. The scorer is
// fail-closed on that counter, so turning a flaky upstream (dropped connections,
// 429s, 5xx) into a success is what keeps whole runs from being discarded. A
// zero-value config means maxAttempts==0, i.e. a single shot with no retry — the
// exact pre-retry behavior, which keeps the relay honest under tests that do not
// opt in.
type retryConfig struct {
	maxAttempts int           // total upstream attempts, including the first
	base        time.Duration // first backoff delay
	cap         time.Duration // ceiling on any single backoff delay
	factor      float64       // exponential growth per attempt
}

// backoff returns the delay before the next attempt after `attempt` (1-based)
// has failed: exponential base*factor^(attempt-1), capped, with full jitter in
// [d/2, d] so a saturated upstream is not hammered in lockstep. The result never
// exceeds cap.
func (c retryConfig) backoff(attempt int) time.Duration {
	d := float64(c.base) * math.Pow(c.factor, float64(attempt-1))
	if capF := float64(c.cap); c.cap > 0 && d > capF {
		d = capF
	}
	if d <= 0 {
		return 0
	}
	return time.Duration(d/2 + rand.Float64()*(d/2))
}

type relay struct {
	provider        string
	profileRevision string
	upstream        string
	apiKey          string
	model           string
	thinking        bool
	pinBody         func(body map[string]any)
	client          *http.Client
	retry           retryConfig
	// sleep waits for d or until ctx is done, returning ctx.Err() if the
	// context ends first. Injected so tests can assert backoff bounds without
	// real delay; nil falls back to the context-aware real sleep.
	sleep                  func(ctx context.Context, d time.Duration) error
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

// ctxSleep is the default relay.sleep: it honors the incoming request deadline
// so retries never outlive the sandbox's context.
func ctxSleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return ctx.Err()
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
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
	port, err := strconv.Atoi(envOr("PORT", "11434"))
	if err != nil || port < 1 || port > 65535 {
		log.Fatal("PORT must be a valid port")
	}
	addr := ":" + strconv.Itoa(port)
	pprofserver.Start(context.Background(), "dittobench-model-relay", port)
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
		retry: retryConfig{
			maxAttempts: envInt("RELAY_RETRY_MAX_ATTEMPTS", 6),
			base:        time.Duration(envInt("RELAY_RETRY_BASE_MS", 200)) * time.Millisecond,
			cap:         time.Duration(envInt("RELAY_RETRY_CAP_MS", 2000)) * time.Millisecond,
			factor:      envFloat("RELAY_RETRY_FACTOR", 2),
		},
		sleep: ctxSleep,
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

	// One relayed request may take several upstream attempts: a transient
	// upstream failure (dropped connection, read error, 429, or 5xx) is retried
	// with capped exponential backoff before it is ever counted as an
	// infrastructure_failure. `out` is already a buffered []byte, so each attempt
	// rebuilds a fresh single-use bytes.Reader over the same bytes. The counters
	// are authoritative-outcome counters: requests is incremented once above, and
	// infrastructure_failures / successes are incremented exactly once below, on
	// the final outcome — never per attempt.
	for attempt := 1; ; attempt++ {
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
			// is a real (retryable) infrastructure failure and flows through the
			// normal path below.
			if callerCanceled(req, err) {
				r.callerCancellations.Add(1)
				log.Print("model-relay: caller cancelled upstream call; not counted as an infrastructure failure")
				return
			}
			// Transport/connection failure — transient, retryable.
			if r.retryUpstream(req.Context(), attempt, fmt.Sprintf("transport error: %v", err)) {
				continue
			}
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
			// Truncated/failed body read — transient, retryable.
			if r.retryUpstream(req.Context(), attempt, "read upstream response error") {
				continue
			}
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
		if isRetryableStatus(resp.StatusCode) {
			// 408/429/5xx — transient upstream saturation/outage, retryable.
			if r.retryUpstream(req.Context(), attempt, fmt.Sprintf("upstream status %d", resp.StatusCode)) {
				continue
			}
			// Attempts exhausted (or deadline): the existing infra path, exactly
			// once, forwarding the real upstream response as before.
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

// retryUpstream reports whether the caller should re-attempt the upstream call.
// It returns false — meaning "take the terminal path now" — when attempts are
// exhausted or the request context ends during backoff. Backoff is gated on the
// context so retries never outlive the sandbox deadline. Nothing here touches the
// counters; the caller records the single authoritative outcome. Logs are
// low-level and never include the body, key, or Authorization header.
func (r *relay) retryUpstream(ctx context.Context, attempt int, reason string) bool {
	if attempt >= r.retry.maxAttempts {
		if r.retry.maxAttempts > 1 {
			log.Printf("model-relay: upstream retries exhausted after %d attempts (%s)", attempt, reason)
		}
		return false
	}
	d := r.retry.backoff(attempt)
	log.Printf("model-relay: retrying upstream attempt %d/%d in %s (%s)", attempt+1, r.retry.maxAttempts, d, reason)
	sleep := r.sleep
	if sleep == nil {
		sleep = ctxSleep
	}
	if err := sleep(ctx, d); err != nil {
		log.Printf("model-relay: upstream retry aborted, context ended (%s)", reason)
		return false
	}
	return true
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

// isRetryableStatus is the transient subset of the infrastructure statuses:
// server-side timeouts (408), rate limits (429), and any 5xx are worth another
// attempt. 401/403 are deterministic (bad key / forbidden) and stay terminal —
// they still count as infrastructure exactly once via isInfrastructureStatus.
func isRetryableStatus(status int) bool {
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

func envInt(name string, def int) int {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(name string, def float64) float64 {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
