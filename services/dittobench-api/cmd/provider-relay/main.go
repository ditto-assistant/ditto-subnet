// Command provider-relay is a local-only model-pinning relay for comparing one
// explicit OpenRouter provider with the production Chutes baseline. It keeps the
// OpenRouter key on the host, forces Qwen3-32B with thinking disabled, and
// disables provider fallbacks so every result is attributable.
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

const maxBody = 4 << 20

type relay struct {
	upstream string
	apiKey   string
	model    string
	provider string
	client   *http.Client
	mu       sync.Mutex
	stats    relayStats
}

type relayStats struct {
	Model              string         `json:"model"`
	Provider           string         `json:"provider"`
	Requests           int            `json:"requests"`
	HTTPErrorResponses int            `json:"http_error_responses"`
	APIErrorResponses  int            `json:"api_error_responses"`
	UpstreamErrors     int            `json:"upstream_errors"`
	PromptTokens       int            `json:"prompt_tokens"`
	CompletionTokens   int            `json:"completion_tokens"`
	TotalTokens        int            `json:"total_tokens"`
	CostUSD            float64        `json:"cost_usd"`
	P50LatencyMS       int64          `json:"p50_latency_ms"`
	P95LatencyMS       int64          `json:"p95_latency_ms"`
	FinishReasons      map[string]int `json:"finish_reasons"`
	ResponseProviders  map[string]int `json:"response_providers"`
	latencies          []int64
}

type relayEnvelope struct {
	Provider string `json:"provider"`
	Error    *struct {
		Code any `json:"code"`
	} `json:"error"`
	Choices []struct {
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int     `json:"prompt_tokens"`
		CompletionTokens int     `json:"completion_tokens"`
		TotalTokens      int     `json:"total_tokens"`
		Cost             float64 `json:"cost"`
	} `json:"usage"`
}

func main() {
	provider := flag.String("provider", "", "exact OpenRouter provider slug")
	model := flag.String("model", providercert.DefaultModel, "exact upstream model identifier")
	upstream := flag.String("upstream", providercert.DefaultAPIURL+"/chat/completions", "upstream chat completions endpoint")
	port := flag.String("port", "11435", "listen port")
	flag.Parse()
	apiKey := strings.TrimSpace(os.Getenv("PROVIDER_RELAY_API_KEY"))
	if apiKey == "" {
		apiKey = strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	}
	r := &relay{
		upstream: strings.TrimSpace(*upstream),
		apiKey:   apiKey,
		model:    strings.TrimSpace(*model),
		provider: strings.TrimSpace(*provider),
		client:   &http.Client{Timeout: 300 * time.Second},
	}
	if r.apiKey == "" || r.model == "" || r.upstream == "" {
		log.Fatal("-model, -upstream, and PROVIDER_RELAY_API_KEY (or OPENROUTER_API_KEY) are required")
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/chat/completions", r.handle)
	mux.HandleFunc("POST /chat/completions", r.handle)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("GET /stats", r.handleStats)
	mux.HandleFunc("POST /stats/reset", r.handleStatsReset)
	log.Printf("local provider relay on :%s (model=%s provider=%s)", *port, r.model, r.provider)
	log.Fatal(http.ListenAndServe(":"+*port, mux))
}

func (r *relay) handle(w http.ResponseWriter, req *http.Request) {
	started := time.Now()
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
	body["model"] = r.model
	body["stream"] = false
	body["reasoning"] = map[string]any{"enabled": false, "exclude": false}
	body["chat_template_kwargs"] = map[string]any{"enable_thinking": false}
	if r.provider != "" {
		body["provider"] = map[string]any{
			"only": []string{r.provider}, "order": []string{r.provider},
			"allow_fallbacks": false, "require_parameters": false,
		}
	} else {
		delete(body, "provider")
	}
	out, err := json.Marshal(body)
	if err != nil {
		http.Error(w, "marshal body", http.StatusInternalServerError)
		return
	}
	up, err := http.NewRequestWithContext(req.Context(), http.MethodPost, r.upstream, bytes.NewReader(out))
	if err != nil {
		http.Error(w, "build upstream request", http.StatusInternalServerError)
		return
	}
	up.Header.Set("Content-Type", "application/json")
	up.Header.Set("Authorization", "Bearer "+r.apiKey)
	if strings.Contains(strings.ToLower(r.upstream), "openrouter.ai") {
		providercert.SetAttributionHeaders(up.Header)
	}
	resp, err := r.client.Do(up)
	if err != nil {
		r.recordUpstreamError(time.Since(started))
		http.Error(w, "upstream unreachable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, maxBody))
	if err != nil {
		r.recordUpstreamError(time.Since(started))
		http.Error(w, "read upstream response", http.StatusBadGateway)
		return
	}
	r.recordResponse(resp.StatusCode, responseBody, time.Since(started))
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(responseBody)
}

func (r *relay) recordUpstreamError(elapsed time.Duration) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.initStatsLocked()
	r.stats.Requests++
	r.stats.UpstreamErrors++
	r.stats.latencies = append(r.stats.latencies, elapsed.Milliseconds())
}

func (r *relay) recordResponse(status int, body []byte, elapsed time.Duration) {
	var envelope relayEnvelope
	_ = json.Unmarshal(body, &envelope)
	r.mu.Lock()
	defer r.mu.Unlock()
	r.initStatsLocked()
	r.stats.Requests++
	r.stats.latencies = append(r.stats.latencies, elapsed.Milliseconds())
	if status < 200 || status >= 300 {
		r.stats.HTTPErrorResponses++
	}
	if envelope.Error != nil {
		r.stats.APIErrorResponses++
	}
	r.stats.PromptTokens += envelope.Usage.PromptTokens
	r.stats.CompletionTokens += envelope.Usage.CompletionTokens
	r.stats.TotalTokens += envelope.Usage.TotalTokens
	r.stats.CostUSD += envelope.Usage.Cost
	if envelope.Provider != "" {
		r.stats.ResponseProviders[envelope.Provider]++
	}
	for _, choice := range envelope.Choices {
		if choice.FinishReason != "" {
			r.stats.FinishReasons[choice.FinishReason]++
		}
	}
}

func (r *relay) initStatsLocked() {
	if r.stats.Model == "" {
		r.stats.Model = r.model
		r.stats.Provider = r.provider
		r.stats.FinishReasons = map[string]int{}
		r.stats.ResponseProviders = map[string]int{}
	}
}

func (r *relay) statsSnapshot() relayStats {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.initStatsLocked()
	snapshot := r.stats
	snapshot.latencies = nil
	latencies := append([]int64(nil), r.stats.latencies...)
	sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
	snapshot.P50LatencyMS = percentile(latencies, 0.50)
	snapshot.P95LatencyMS = percentile(latencies, 0.95)
	snapshot.FinishReasons = cloneCounts(r.stats.FinishReasons)
	snapshot.ResponseProviders = cloneCounts(r.stats.ResponseProviders)
	return snapshot
}

func (r *relay) handleStats(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(r.statsSnapshot())
}

func (r *relay) handleStatsReset(w http.ResponseWriter, _ *http.Request) {
	r.mu.Lock()
	r.stats = relayStats{}
	r.initStatsLocked()
	r.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func cloneCounts(in map[string]int) map[string]int {
	out := make(map[string]int, len(in))
	for key, value := range in {
		out[key] = value
	}
	return out
}

func percentile(sorted []int64, p float64) int64 {
	if len(sorted) == 0 {
		return 0
	}
	index := int(float64(len(sorted)-1)*p + 0.5)
	return sorted[index]
}
