// Package providercert probes and summarizes explicitly pinned OpenRouter
// providers for the model contract used by DittoBench harnesses.
package providercert

import (
	"encoding/json"
	"time"
)

const (
	SchemaVersion = 1
	DefaultModel  = "qwen/qwen3-32b"
	DefaultAPIURL = "https://openrouter.ai/api/v1"
	AppReferer    = "https://heyditto.ai"
	AppTitle      = "Ditto"
)

type Endpoint struct {
	Name                string         `json:"name"`
	ProviderName        string         `json:"provider_name"`
	ProviderSlug        string         `json:"provider_slug"`
	Tag                 string         `json:"tag"`
	Status              int            `json:"status"`
	UptimeLast30m       float64        `json:"uptime_last_30m"`
	ContextLength       int            `json:"context_length"`
	MaxCompletionTokens *int           `json:"max_completion_tokens,omitempty"`
	Quantization        string         `json:"quantization"`
	Pricing             map[string]any `json:"pricing"`
	SupportedParameters []string       `json:"supported_parameters"`
}

type Inventory struct {
	CapturedAt   time.Time  `json:"captured_at"`
	Model        string     `json:"model"`
	ModelName    string     `json:"model_name"`
	Architecture any        `json:"architecture,omitempty"`
	Endpoints    []Endpoint `json:"endpoints"`
}

type ToolCall struct {
	ID        string          `json:"id"`
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments,omitempty"`
}

type Usage struct {
	PromptTokens     int64   `json:"prompt_tokens"`
	CompletionTokens int64   `json:"completion_tokens"`
	TotalTokens      int64   `json:"total_tokens"`
	ReasoningTokens  int64   `json:"reasoning_tokens,omitempty"`
	Cost             float64 `json:"cost_usd,omitempty"`
}

type Result struct {
	Provider           string     `json:"provider"`
	ProviderSlug       string     `json:"provider_slug"`
	EndpointTag        string     `json:"endpoint_tag"`
	Scenario           string     `json:"scenario"`
	Run                int        `json:"run"`
	Streaming          bool       `json:"streaming"`
	Success            bool       `json:"success"`
	HTTPStatus         int        `json:"http_status"`
	Attempts           int        `json:"attempts"`
	LatencyMS          int64      `json:"latency_ms"`
	TTFTMS             int64      `json:"ttft_ms,omitempty"`
	Model              string     `json:"model,omitempty"`
	ResponseProvider   string     `json:"response_provider,omitempty"`
	FinishReason       string     `json:"finish_reason,omitempty"`
	NativeFinishReason string     `json:"native_finish_reason,omitempty"`
	ContentLength      int        `json:"content_length"`
	ToolCalls          []ToolCall `json:"tool_calls,omitempty"`
	ReasoningPresent   bool       `json:"reasoning_present"`
	ReasoningDetails   int        `json:"reasoning_details"`
	Usage              Usage      `json:"usage"`
	Errors             []string   `json:"errors,omitempty"`
	content            string
}

type ProviderSummary struct {
	Provider                       string  `json:"provider"`
	ProviderSlug                   string  `json:"provider_slug"`
	Runs                           int     `json:"runs"`
	Successes                      int     `json:"successes"`
	SuccessRate                    float64 `json:"success_rate"`
	ToolRuns                       int     `json:"tool_runs"`
	ToolSuccesses                  int     `json:"tool_successes"`
	ToolSuccessRate                float64 `json:"tool_success_rate"`
	P50LatencyMS                   int64   `json:"p50_latency_ms"`
	P95LatencyMS                   int64   `json:"p95_latency_ms"`
	OutputTokens                   int64   `json:"output_tokens"`
	EffectiveOutputTokensPerSecond float64 `json:"effective_output_tokens_per_second"`
	TotalCostUSD                   float64 `json:"total_cost_usd"`
}

type Report struct {
	SchemaVersion   int               `json:"schema_version"`
	StartedAt       time.Time         `json:"started_at"`
	FinishedAt      time.Time         `json:"finished_at"`
	Model           string            `json:"model"`
	RunsPerScenario int               `json:"runs_per_scenario"`
	RunOffset       int               `json:"run_offset,omitempty"`
	Routing         map[string]any    `json:"routing"`
	Inventory       Inventory         `json:"inventory"`
	Results         []Result          `json:"results"`
	Summary         []ProviderSummary `json:"summary"`
}
