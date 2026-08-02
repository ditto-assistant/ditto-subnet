package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const (
	profileRevision      = "longmemeval-openrouter-pplx-embed-v1-0.6b-768-v1"
	harnessModel         = "embeddinggemma"
	openRouterModel      = "perplexity/pplx-embed-v1-0.6b"
	openRouterProvider   = "Perplexity"
	openRouterURL        = "https://openrouter.ai/api/v1/embeddings"
	embeddingDimensions  = 768
	maximumInputs        = 256
	maximumBodyBytes     = 2 << 20
	maximumResponseBytes = 16 << 20
	promptUSDPerToken    = 0.000000004
)

type ollamaRequest struct {
	Model string          `json:"model"`
	Input json.RawMessage `json:"input"`
}

type ollamaResponse struct {
	Model           string      `json:"model"`
	Embeddings      [][]float64 `json:"embeddings"`
	PromptEvalCount int         `json:"prompt_eval_count,omitempty"`
}

type providerOptions struct {
	Order          []string `json:"order"`
	AllowFallbacks bool     `json:"allow_fallbacks"`
	DataCollection string   `json:"data_collection"`
}

type openRouterRequest struct {
	Model          string          `json:"model"`
	Input          []string        `json:"input"`
	Dimensions     int             `json:"dimensions"`
	EncodingFormat string          `json:"encoding_format"`
	Provider       providerOptions `json:"provider"`
}

type openRouterResponse struct {
	Data []struct {
		Embedding []float64 `json:"embedding"`
		Index     int       `json:"index"`
	} `json:"data"`
	Usage *struct {
		PromptTokens int `json:"prompt_tokens"`
		TotalTokens  int `json:"total_tokens"`
	} `json:"usage"`
}

type proxyStats struct {
	Requests           atomic.Uint64
	CacheHits          atomic.Uint64
	CacheMisses        atomic.Uint64
	CacheWriteFailures atomic.Uint64
	CoalescedWaits     atomic.Uint64
	UpstreamCalls      atomic.Uint64
	Failures           atomic.Uint64
	PromptTokens       atomic.Uint64
	TotalTokens        atomic.Uint64
	LatencyMS          atomic.Uint64
}

type inflightCall struct {
	done chan struct{}
}

type proxy struct {
	apiKey      string
	upstreamURL string
	cacheDir    string
	client      *http.Client
	stats       proxyStats
	mu          sync.Mutex
	inflight    map[string]*inflightCall
}

func newProxy(apiKey, upstreamURL, cacheDir string, client *http.Client) *proxy {
	if client == nil {
		client = &http.Client{
			Timeout: 100 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		}
	}
	return &proxy{
		apiKey: strings.TrimSpace(apiKey), upstreamURL: upstreamURL,
		cacheDir: cacheDir, client: client, inflight: make(map[string]*inflightCall),
	}
}

func canonicalRequest(inputs []string) openRouterRequest {
	return openRouterRequest{
		Model: openRouterModel, Input: inputs, Dimensions: embeddingDimensions,
		EncodingFormat: "float",
		Provider: providerOptions{
			Order: []string{openRouterProvider}, AllowFallbacks: false,
			DataCollection: "deny",
		},
	}
}

func decodeInputs(raw json.RawMessage) ([]string, error) {
	var batch []string
	if json.Unmarshal(raw, &batch) == nil {
		if len(batch) == 0 || len(batch) > maximumInputs {
			return nil, errors.New("invalid input count")
		}
		for _, input := range batch {
			if input == "" || len(input) > maximumBodyBytes {
				return nil, errors.New("invalid input")
			}
		}
		return batch, nil
	}
	var single string
	if json.Unmarshal(raw, &single) != nil || single == "" || len(single) > maximumBodyBytes {
		return nil, errors.New("input must be a string or array of strings")
	}
	return []string{single}, nil
}

func cacheKey(request openRouterRequest) (string, []byte, error) {
	body, err := json.Marshal(struct {
		Profile string            `json:"profile"`
		Request openRouterRequest `json:"request"`
	}{Profile: profileRevision, Request: request})
	if err != nil {
		return "", nil, err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), body, nil
}

func (p *proxy) cached(key string) ([]byte, bool) {
	if p.cacheDir == "" {
		return nil, false
	}
	body, err := os.ReadFile(filepath.Join(p.cacheDir, key+".json"))
	return body, err == nil
}

func (p *proxy) store(key string, body []byte) error {
	if p.cacheDir == "" {
		return nil
	}
	if err := os.MkdirAll(p.cacheDir, 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(p.cacheDir, ".embedding-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(body); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryName, filepath.Join(p.cacheDir, key+".json"))
}

func (p *proxy) resolve(ctx context.Context, request openRouterRequest) ([]byte, error) {
	key, _, err := cacheKey(request)
	if err != nil {
		return nil, err
	}
	lockedBody, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	if body, ok := p.cached(key); ok {
		p.stats.CacheHits.Add(1)
		return body, nil
	}

	p.mu.Lock()
	if call := p.inflight[key]; call != nil {
		p.stats.CoalescedWaits.Add(1)
		p.mu.Unlock()
		select {
		case <-call.done:
			if body, ok := p.cached(key); ok {
				p.stats.CacheHits.Add(1)
				return body, nil
			}
			return nil, errors.New("coalesced embedding request failed")
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	call := &inflightCall{done: make(chan struct{})}
	p.inflight[key] = call
	p.stats.CacheMisses.Add(1)
	p.mu.Unlock()
	defer func() {
		p.mu.Lock()
		delete(p.inflight, key)
		close(call.done)
		p.mu.Unlock()
	}()

	upstream, err := http.NewRequestWithContext(ctx, http.MethodPost, p.upstreamURL, bytes.NewReader(lockedBody))
	if err != nil {
		return nil, err
	}
	upstream.Header.Set("Authorization", "Bearer "+p.apiKey)
	upstream.Header.Set("Content-Type", "application/json")
	upstream.Header.Set("HTTP-Referer", "https://dittobench.com")
	upstream.Header.Set("X-Title", "DittoBench LongMemEval embedding calibration")
	p.stats.UpstreamCalls.Add(1)
	started := time.Now()
	response, err := p.client.Do(upstream)
	p.stats.LatencyMS.Add(uint64(time.Since(started).Milliseconds()))
	if err != nil {
		return nil, fmt.Errorf("embedding provider unavailable: %w", err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maximumResponseBytes+1))
	if err != nil || len(responseBody) > maximumResponseBytes {
		return nil, errors.New("embedding provider returned an unreadable response")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("embedding provider returned status %d", response.StatusCode)
	}
	var decoded openRouterResponse
	if json.Unmarshal(responseBody, &decoded) != nil || len(decoded.Data) != len(request.Input) {
		return nil, errors.New("embedding provider returned an invalid response")
	}
	sort.Slice(decoded.Data, func(i, j int) bool { return decoded.Data[i].Index < decoded.Data[j].Index })
	vectors := make([][]float64, len(decoded.Data))
	for index, item := range decoded.Data {
		if item.Index != index || len(item.Embedding) != embeddingDimensions {
			return nil, errors.New("embedding provider returned invalid vector dimensions or ordering")
		}
		for _, value := range item.Embedding {
			if math.IsNaN(value) || math.IsInf(value, 0) {
				return nil, errors.New("embedding provider returned a non-finite vector")
			}
		}
		vectors[index] = item.Embedding
	}
	promptTokens := 0
	if decoded.Usage != nil && decoded.Usage.PromptTokens >= 0 && decoded.Usage.TotalTokens >= decoded.Usage.PromptTokens {
		promptTokens = decoded.Usage.PromptTokens
		p.stats.PromptTokens.Add(uint64(decoded.Usage.PromptTokens))
		p.stats.TotalTokens.Add(uint64(decoded.Usage.TotalTokens))
	}
	ollamaBody, err := json.Marshal(ollamaResponse{
		Model: harnessModel, Embeddings: vectors, PromptEvalCount: promptTokens,
	})
	if err != nil {
		return nil, err
	}
	if err := p.store(key, ollamaBody); err != nil {
		// Cache durability improves restart cost but must never turn a valid,
		// dimension-checked provider response into a failed memory write.
		p.stats.CacheWriteFailures.Add(1)
	}
	return ollamaBody, nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet && r.URL.Path == "/health" {
		promptTokens := p.stats.PromptTokens.Load()
		writeJSON(w, http.StatusOK, map[string]any{
			"status": "ok", "profile_revision": profileRevision,
			"harness_model": harnessModel, "upstream_model": openRouterModel,
			"provider": openRouterProvider, "dimensions": embeddingDimensions,
			"requests": p.stats.Requests.Load(), "cache_hits": p.stats.CacheHits.Load(),
			"cache_misses": p.stats.CacheMisses.Load(), "coalesced_waits": p.stats.CoalescedWaits.Load(),
			"cache_write_failures": p.stats.CacheWriteFailures.Load(),
			"upstream_requests":    p.stats.UpstreamCalls.Load(), "failures": p.stats.Failures.Load(),
			"prompt_tokens": promptTokens, "total_tokens": p.stats.TotalTokens.Load(),
			"provider_latency_ms": p.stats.LatencyMS.Load(),
			"estimated_cost_usd":  float64(promptTokens) * promptUSDPerToken,
		})
		return
	}
	if r.Method != http.MethodPost || r.URL.Path != "/api/embed" {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	p.stats.Requests.Add(1)
	if p.apiKey == "" {
		p.stats.Failures.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "embedding service unavailable"})
		return
	}
	decoder := json.NewDecoder(io.LimitReader(r.Body, maximumBodyBytes+1))
	decoder.DisallowUnknownFields()
	var request ollamaRequest
	if decoder.Decode(&request) != nil || decoder.Decode(&struct{}{}) != io.EOF ||
		(request.Model != harnessModel && request.Model != harnessModel+":latest") {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid embedding request"})
		return
	}
	inputs, err := decodeInputs(request.Input)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid embedding request"})
		return
	}
	body, err := p.resolve(r.Context(), canonicalRequest(inputs))
	if err != nil {
		p.stats.Failures.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "embedding service unavailable"})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func main() {
	apiKey := strings.TrimSpace(os.Getenv("OPENROUTER_API_KEY"))
	if apiKey == "" {
		// The benchmark reader relay already uses this secret name. Supporting it
		// lets the embedding process share that trusted container without ever
		// copying the credential into a miner container or command line.
		apiKey = strings.TrimSpace(os.Getenv("RELAY_API_KEY"))
	}
	if apiKey == "" {
		log.Fatal("OPENROUTER_API_KEY is required")
	}
	port, err := strconv.Atoi(envOr("OPENROUTER_EMBED_PORT", "11434"))
	if err != nil || port < 1 || port > 65535 {
		log.Fatal("OPENROUTER_EMBED_PORT must be a valid port")
	}
	cacheDir := envOr("OPENROUTER_EMBED_CACHE_DIR", "/tmp/dittobench-openrouter-embeddings")
	handler := newProxy(apiKey, openRouterURL, cacheDir, nil)
	server := &http.Server{
		Addr:              fmt.Sprintf(":%d", port),
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      110 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	log.Printf("OpenRouter embedding proxy on :%d; profile=%s", port, profileRevision)
	log.Fatal(server.ListenAndServe())
}
