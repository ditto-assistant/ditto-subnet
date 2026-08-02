// Command provider-load runs a bounded, no-retry concurrency ramp against an
// OpenAI-compatible chat endpoint. It is intended for controlled provider
// certification, not production load generation.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/providercert"
)

type config struct {
	Endpoint              string
	Model                 string
	Provider              string
	APIKey                string
	Concurrency           []int
	Waves                 int
	RequestsPerLevel      int
	MaxTokens             int
	RequestTimeout        time.Duration
	Stream                bool
	Fixture               string
	InputPricePerMillion  float64
	OutputPricePerMillion float64
}

type report struct {
	SchemaVersion         int           `json:"schema_version"`
	StartedAt             time.Time     `json:"started_at"`
	FinishedAt            time.Time     `json:"finished_at"`
	Endpoint              string        `json:"endpoint"`
	Model                 string        `json:"model"`
	Provider              string        `json:"provider,omitempty"`
	Waves                 int           `json:"waves"`
	RequestsPerLevel      int           `json:"requests_per_level,omitempty"`
	MaxTokens             int           `json:"max_tokens"`
	Stream                bool          `json:"stream"`
	Fixture               string        `json:"fixture"`
	RetryAttempts         int           `json:"retry_attempts"`
	Interrupted           bool          `json:"interrupted"`
	InputPricePerMillion  float64       `json:"input_price_per_million,omitempty"`
	OutputPricePerMillion float64       `json:"output_price_per_million,omitempty"`
	Levels                []levelReport `json:"levels"`
}

type levelReport struct {
	Concurrency                 int            `json:"concurrency"`
	Requests                    int            `json:"requests"`
	Successful                  int            `json:"successful"`
	SuccessRate                 float64        `json:"success_rate"`
	RateLimited                 int            `json:"rate_limited"`
	ServerErrors                int            `json:"server_errors"`
	SchemaErrors                int            `json:"schema_errors"`
	Timeouts                    int            `json:"timeouts"`
	OtherErrors                 int            `json:"other_errors"`
	Conformant                  int            `json:"conformant"`
	ConformanceRate             float64        `json:"conformance_rate"`
	ReasoningLeaks              int            `json:"reasoning_leaks"`
	WallMS                      int64          `json:"wall_ms"`
	SuccessfulRequestsPerSecond float64        `json:"successful_requests_per_second"`
	CompletionTokensPerSecond   float64        `json:"completion_tokens_per_second"`
	P50LatencyMS                int64          `json:"p50_latency_ms"`
	P95LatencyMS                int64          `json:"p95_latency_ms"`
	P99LatencyMS                int64          `json:"p99_latency_ms"`
	P50TTFTMS                   int64          `json:"p50_ttft_ms"`
	P95TTFTMS                   int64          `json:"p95_ttft_ms"`
	P99TTFTMS                   int64          `json:"p99_ttft_ms"`
	PromptTokens                int            `json:"prompt_tokens"`
	CompletionTokens            int            `json:"completion_tokens"`
	TotalTokens                 int            `json:"total_tokens"`
	CostUSD                     float64        `json:"cost_usd"`
	CostEstimated               bool           `json:"cost_estimated"`
	ResponseProviders           map[string]int `json:"response_providers"`
	Observations                []observation  `json:"observations"`
}

type observation struct {
	Index              int     `json:"index"`
	HTTPStatus         int     `json:"http_status"`
	LatencyMS          int64   `json:"latency_ms"`
	TTFTMS             int64   `json:"ttft_ms,omitempty"`
	Success            bool    `json:"success"`
	Conformant         bool    `json:"conformant"`
	ReasoningLeak      bool    `json:"reasoning_leak"`
	ErrorKind          string  `json:"error_kind,omitempty"`
	FinishReason       string  `json:"finish_reason,omitempty"`
	NativeFinishReason string  `json:"native_finish_reason,omitempty"`
	ResponseProvider   string  `json:"response_provider,omitempty"`
	PromptTokens       int     `json:"prompt_tokens"`
	CompletionTokens   int     `json:"completion_tokens"`
	TotalTokens        int     `json:"total_tokens"`
	CostUSD            float64 `json:"cost_usd"`
}

type chatResponse struct {
	Provider string `json:"provider"`
	Choices  []struct {
		FinishReason       string `json:"finish_reason"`
		NativeFinishReason string `json:"native_finish_reason"`
		Message            struct {
			Content          string     `json:"content"`
			Reasoning        any        `json:"reasoning"`
			ReasoningContent any        `json:"reasoning_content"`
			ReasoningDetails any        `json:"reasoning_details"`
			ToolCalls        []toolCall `json:"tool_calls"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int     `json:"prompt_tokens"`
		CompletionTokens int     `json:"completion_tokens"`
		TotalTokens      int     `json:"total_tokens"`
		Cost             float64 `json:"cost"`
	} `json:"usage"`
	Error *struct {
		Code int `json:"code"`
	} `json:"error"`
}

type toolCall struct {
	Index    int    `json:"index"`
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type streamChunk struct {
	Provider string `json:"provider"`
	Choices  []struct {
		FinishReason       string `json:"finish_reason"`
		NativeFinishReason string `json:"native_finish_reason"`
		Delta              struct {
			Content          string     `json:"content"`
			Reasoning        any        `json:"reasoning"`
			ReasoningContent any        `json:"reasoning_content"`
			ReasoningDetails any        `json:"reasoning_details"`
			ToolCalls        []toolCall `json:"tool_calls"`
		} `json:"delta"`
	} `json:"choices"`
	Usage chatUsage `json:"usage"`
	Error *struct {
		Code int `json:"code"`
	} `json:"error"`
}

type chatUsage struct {
	PromptTokens     int     `json:"prompt_tokens"`
	CompletionTokens int     `json:"completion_tokens"`
	TotalTokens      int     `json:"total_tokens"`
	Cost             float64 `json:"cost"`
}

func main() {
	endpoint := flag.String("endpoint", "", "chat completions endpoint")
	model := flag.String("model", "", "exact upstream model identifier")
	provider := flag.String("provider", "", "OpenRouter provider slug; pins routing and disables fallbacks")
	concurrency := flag.String("concurrency", "1,2,4,8,16", "comma-separated bounded concurrency levels")
	waves := flag.Int("waves", 3, "requests per worker at each level")
	requestsPerLevel := flag.Int("requests-per-level", 0, "fixed requests at each level; overrides -waves")
	maxTokens := flag.Int("max-tokens", 128, "maximum completion tokens per request")
	timeout := flag.Duration("timeout", 120*time.Second, "per-request timeout")
	stream := flag.Bool("stream", false, "stream responses and record time to first event")
	fixture := flag.String("fixture", "text", "deterministic fixture: text or tool")
	inputPrice := flag.Float64("input-price-per-million", 0, "fallback input price in USD per million tokens")
	outputPrice := flag.Float64("output-price-per-million", 0, "fallback output price in USD per million tokens")
	output := flag.String("output", "", "write JSON report here")
	flag.Parse()
	levels, err := parseLevels(*concurrency)
	if err != nil {
		fatal(err)
	}
	apiKey := strings.TrimSpace(os.Getenv("PROVIDER_LOAD_API_KEY"))
	if *endpoint == "" || *model == "" || apiKey == "" || *waves < 1 || *maxTokens < 1 || *requestsPerLevel < 0 || *requestsPerLevel > 100 {
		fatal(errors.New("-endpoint, -model, positive -waves/-max-tokens, and PROVIDER_LOAD_API_KEY are required"))
	}
	if *fixture != "text" && *fixture != "tool" {
		fatal(errors.New("-fixture must be text or tool"))
	}
	cfg := config{Endpoint: *endpoint, Model: *model, Provider: *provider, APIKey: apiKey, Concurrency: levels, Waves: *waves, RequestsPerLevel: *requestsPerLevel, MaxTokens: *maxTokens, RequestTimeout: *timeout, Stream: *stream, Fixture: *fixture, InputPricePerMillion: *inputPrice, OutputPricePerMillion: *outputPrice}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	result := run(ctx, cfg)
	raw, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		fatal(err)
	}
	raw = append(raw, '\n')
	if *output == "" {
		_, _ = os.Stdout.Write(raw)
		return
	}
	if err := os.WriteFile(*output, raw, 0o644); err != nil {
		fatal(err)
	}
}

func run(ctx context.Context, cfg config) report {
	result := report{SchemaVersion: 2, StartedAt: time.Now().UTC(), Endpoint: cfg.Endpoint, Model: cfg.Model, Provider: cfg.Provider, Waves: cfg.Waves, RequestsPerLevel: cfg.RequestsPerLevel, MaxTokens: cfg.MaxTokens, Stream: cfg.Stream, Fixture: cfg.Fixture, RetryAttempts: 0, InputPricePerMillion: cfg.InputPricePerMillion, OutputPricePerMillion: cfg.OutputPricePerMillion}
	client := &http.Client{}
	for _, concurrency := range cfg.Concurrency {
		result.Levels = append(result.Levels, runLevel(ctx, client, cfg, concurrency))
		if ctx.Err() != nil {
			result.Interrupted = true
			break
		}
	}
	result.FinishedAt = time.Now().UTC()
	return result
}

func runLevel(ctx context.Context, client *http.Client, cfg config, concurrency int) levelReport {
	count := concurrency * cfg.Waves
	if cfg.RequestsPerLevel > 0 {
		count = cfg.RequestsPerLevel
	}
	observations := make([]observation, count)
	jobs := make(chan int, count)
	started := time.Now()
	var wg sync.WaitGroup
	for index := range count {
		jobs <- index
	}
	close(jobs)
	for range concurrency {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range jobs {
				item := request(ctx, client, cfg, concurrency*100000+index)
				item.Index = index
				observations[index] = item
			}
		}()
	}
	wg.Wait()
	return summarizeLevel(cfg, concurrency, time.Since(started), observations)
}

func request(parent context.Context, client *http.Client, cfg config, index int) observation {
	body := map[string]any{
		"model":       cfg.Model,
		"messages":    []map[string]string{{"role": "user", "content": fmt.Sprintf("Load certification request %d. Write exactly 100 short English words about reliable distributed systems. Do not use tools.", index)}},
		"temperature": 0, "max_tokens": cfg.MaxTokens, "stream": cfg.Stream,
		"chat_template_kwargs": map[string]any{"enable_thinking": false},
	}
	if cfg.Stream {
		body["stream_options"] = map[string]any{"include_usage": true}
	}
	if cfg.Fixture == "tool" {
		body["messages"] = []map[string]string{{"role": "user", "content": fmt.Sprintf("Load certification request %d. Use lookup_weather for Paris in celsius. Do not answer directly.", index)}}
		body["tools"] = []map[string]any{{
			"type": "function",
			"function": map[string]any{
				"name": "lookup_weather", "description": "Look up current weather.",
				"parameters": map[string]any{
					"type": "object", "additionalProperties": false,
					"properties": map[string]any{
						"city": map[string]any{"type": "string"},
						"unit": map[string]any{"type": "string", "enum": []string{"celsius"}},
					},
					"required": []string{"city", "unit"},
				},
			},
		}}
		body["tool_choice"] = map[string]any{"type": "function", "function": map[string]string{"name": "lookup_weather"}}
	}
	if cfg.Provider != "" {
		body["reasoning"] = map[string]any{"enabled": false, "exclude": false}
		body["provider"] = map[string]any{"only": []string{cfg.Provider}, "order": []string{cfg.Provider}, "allow_fallbacks": false, "require_parameters": false}
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return observation{Index: index, ErrorKind: "marshal"}
	}
	ctx, cancel := context.WithTimeout(parent, cfg.RequestTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, cfg.Endpoint, bytes.NewReader(raw))
	if err != nil {
		return observation{Index: index, ErrorKind: "request"}
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	req.Header.Set("Content-Type", "application/json")
	if strings.Contains(strings.ToLower(cfg.Endpoint), "openrouter.ai") {
		providercert.SetAttributionHeaders(req.Header)
	}
	started := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		kind := "transport"
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			kind = "timeout"
		} else if errors.Is(err, context.Canceled) || errors.Is(ctx.Err(), context.Canceled) {
			kind = "canceled"
		}
		return observation{Index: index, LatencyMS: time.Since(started).Milliseconds(), ErrorKind: kind}
	}
	defer resp.Body.Close()
	out := observation{Index: index, HTTPStatus: resp.StatusCode}
	if resp.StatusCode == http.StatusTooManyRequests {
		out.ErrorKind = "rate_limited"
	} else if resp.StatusCode >= 500 {
		out.ErrorKind = "server_error"
	} else if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		out.ErrorKind = "api_error"
	}
	if out.ErrorKind != "" {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
		out.LatencyMS = time.Since(started).Milliseconds()
		var decoded chatResponse
		if json.Unmarshal(data, &decoded) == nil {
			out.ResponseProvider = decoded.Provider
			out.PromptTokens, out.CompletionTokens, out.TotalTokens, out.CostUSD = decoded.Usage.PromptTokens, decoded.Usage.CompletionTokens, decoded.Usage.TotalTokens, decoded.Usage.Cost
		}
		return out
	}
	if cfg.Stream {
		return readStream(resp.Body, out, cfg.Fixture, started)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	out.LatencyMS = time.Since(started).Milliseconds()
	if err != nil {
		out.ErrorKind = "read"
		return out
	}
	var decoded chatResponse
	if err := json.Unmarshal(data, &decoded); err != nil {
		out.ErrorKind = "malformed_json"
		return out
	}
	out.ResponseProvider = decoded.Provider
	out.PromptTokens, out.CompletionTokens, out.TotalTokens, out.CostUSD = decoded.Usage.PromptTokens, decoded.Usage.CompletionTokens, decoded.Usage.TotalTokens, decoded.Usage.Cost
	if decoded.Error != nil {
		out.ErrorKind = "api_error"
	} else if len(decoded.Choices) != 1 || decoded.Usage.TotalTokens == 0 {
		out.ErrorKind = "schema"
	} else {
		out.FinishReason = decoded.Choices[0].FinishReason
		out.NativeFinishReason = decoded.Choices[0].NativeFinishReason
		out.ReasoningLeak = hasValue(decoded.Choices[0].Message.Reasoning) || hasValue(decoded.Choices[0].Message.ReasoningContent) || hasValue(decoded.Choices[0].Message.ReasoningDetails)
		out.Conformant = responseConforms(cfg.Fixture, decoded.Choices[0].Message.Content, decoded.Choices[0].Message.ToolCalls)
		out.Success = out.Conformant
		if !out.Conformant {
			out.ErrorKind = "schema"
		}
	}
	return out
}

func readStream(body io.Reader, out observation, fixture string, started time.Time) observation {
	scanner := bufio.NewScanner(io.LimitReader(body, 4<<20))
	scanner.Buffer(make([]byte, 64<<10), 1<<20)
	var content strings.Builder
	toolCalls := map[int]*toolCall{}
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		payload := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if payload == "[DONE]" {
			break
		}
		var chunk streamChunk
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			out.ErrorKind = "malformed_stream"
			continue
		}
		if out.TTFTMS == 0 {
			out.TTFTMS = time.Since(started).Milliseconds()
		}
		if chunk.Provider != "" {
			out.ResponseProvider = chunk.Provider
		}
		if chunk.Error != nil {
			out.ErrorKind = "api_error"
		}
		if chunk.Usage.TotalTokens > 0 {
			out.PromptTokens, out.CompletionTokens, out.TotalTokens, out.CostUSD = chunk.Usage.PromptTokens, chunk.Usage.CompletionTokens, chunk.Usage.TotalTokens, chunk.Usage.Cost
		}
		for _, choice := range chunk.Choices {
			if choice.FinishReason != "" {
				out.FinishReason = choice.FinishReason
			}
			if choice.NativeFinishReason != "" {
				out.NativeFinishReason = choice.NativeFinishReason
			}
			content.WriteString(choice.Delta.Content)
			out.ReasoningLeak = out.ReasoningLeak || hasValue(choice.Delta.Reasoning) || hasValue(choice.Delta.ReasoningContent) || hasValue(choice.Delta.ReasoningDetails)
			for _, delta := range choice.Delta.ToolCalls {
				call := toolCalls[delta.Index]
				if call == nil {
					call = &toolCall{Index: delta.Index}
					toolCalls[delta.Index] = call
				}
				call.ID += delta.ID
				call.Type += delta.Type
				call.Function.Name += delta.Function.Name
				call.Function.Arguments += delta.Function.Arguments
			}
		}
	}
	out.LatencyMS = time.Since(started).Milliseconds()
	if err := scanner.Err(); err != nil && out.ErrorKind == "" {
		out.ErrorKind = "read"
	}
	calls := make([]toolCall, 0, len(toolCalls))
	for index := 0; index < len(toolCalls); index++ {
		if call := toolCalls[index]; call != nil {
			calls = append(calls, *call)
		}
	}
	if out.ErrorKind == "" && out.TotalTokens == 0 {
		out.ErrorKind = "schema"
	}
	if out.ErrorKind == "" {
		out.Conformant = responseConforms(fixture, content.String(), calls)
		out.Success = out.Conformant
		if !out.Conformant {
			out.ErrorKind = "schema"
		}
	}
	return out
}

func responseConforms(fixture, content string, calls []toolCall) bool {
	if fixture == "" || fixture == "text" {
		return strings.TrimSpace(content) != "" && len(calls) == 0
	}
	if len(calls) != 1 || calls[0].Function.Name != "lookup_weather" || strings.TrimSpace(calls[0].ID) == "" {
		return false
	}
	var args struct {
		City string `json:"city"`
		Unit string `json:"unit"`
	}
	return json.Unmarshal([]byte(calls[0].Function.Arguments), &args) == nil && strings.EqualFold(args.City, "Paris") && args.Unit == "celsius"
}

func hasValue(value any) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case string:
		return strings.TrimSpace(typed) != ""
	default:
		return true
	}
}

func summarizeLevel(cfg config, concurrency int, wall time.Duration, observations []observation) levelReport {
	result := levelReport{Concurrency: concurrency, Requests: len(observations), WallMS: wall.Milliseconds(), ResponseProviders: map[string]int{}, Observations: observations}
	var latencies []int64
	var ttfts []int64
	for _, item := range observations {
		if item.Success {
			result.Successful++
			latencies = append(latencies, item.LatencyMS)
			if item.TTFTMS > 0 {
				ttfts = append(ttfts, item.TTFTMS)
			}
		}
		if item.Conformant {
			result.Conformant++
		}
		if item.ReasoningLeak {
			result.ReasoningLeaks++
		}
		if item.ErrorKind == "rate_limited" {
			result.RateLimited++
		}
		if item.ErrorKind == "server_error" {
			result.ServerErrors++
		}
		if item.ErrorKind == "schema" || item.ErrorKind == "malformed_json" || item.ErrorKind == "malformed_stream" {
			result.SchemaErrors++
		}
		if item.ErrorKind == "timeout" {
			result.Timeouts++
		}
		if item.ErrorKind != "" && item.ErrorKind != "rate_limited" && item.ErrorKind != "server_error" && item.ErrorKind != "schema" && item.ErrorKind != "malformed_json" && item.ErrorKind != "malformed_stream" && item.ErrorKind != "timeout" {
			result.OtherErrors++
		}
		result.PromptTokens += item.PromptTokens
		result.CompletionTokens += item.CompletionTokens
		result.TotalTokens += item.TotalTokens
		result.CostUSD += item.CostUSD
		if item.ResponseProvider != "" {
			result.ResponseProviders[item.ResponseProvider]++
		}
	}
	if result.CostUSD == 0 && (cfg.InputPricePerMillion > 0 || cfg.OutputPricePerMillion > 0) {
		result.CostUSD = (float64(result.PromptTokens)*cfg.InputPricePerMillion + float64(result.CompletionTokens)*cfg.OutputPricePerMillion) / 1_000_000
		result.CostEstimated = true
	}
	if result.Requests > 0 {
		result.SuccessRate = float64(result.Successful) / float64(result.Requests)
		result.ConformanceRate = float64(result.Conformant) / float64(result.Requests)
	}
	if seconds := wall.Seconds(); seconds > 0 {
		result.SuccessfulRequestsPerSecond = float64(result.Successful) / seconds
		result.CompletionTokensPerSecond = float64(result.CompletionTokens) / seconds
	}
	result.P50LatencyMS, result.P95LatencyMS, result.P99LatencyMS = percentile(latencies, .50), percentile(latencies, .95), percentile(latencies, .99)
	result.P50TTFTMS, result.P95TTFTMS, result.P99TTFTMS = percentile(ttfts, .50), percentile(ttfts, .95), percentile(ttfts, .99)
	return result
}

func percentile(values []int64, quantile float64) int64 {
	if len(values) == 0 {
		return 0
	}
	copyValues := append([]int64(nil), values...)
	sort.Slice(copyValues, func(i, j int) bool { return copyValues[i] < copyValues[j] })
	index := int(float64(len(copyValues)-1) * quantile)
	return copyValues[index]
}

func parseLevels(raw string) ([]int, error) {
	parts := strings.Split(raw, ",")
	levels := make([]int, 0, len(parts))
	seen := map[int]bool{}
	for _, part := range parts {
		value, err := strconv.Atoi(strings.TrimSpace(part))
		if err != nil || value < 1 || value > 64 {
			return nil, fmt.Errorf("invalid concurrency %q; values must be in [1,64]", part)
		}
		if !seen[value] {
			seen[value] = true
			levels = append(levels, value)
		}
	}
	if len(levels) == 0 {
		return nil, errors.New("at least one concurrency level is required")
	}
	return levels, nil
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "provider-load:", err)
	os.Exit(1)
}
