package codingcontract

import (
	"encoding/json"
)

const (
	InferenceSolverModel              = "openai/gpt-5.6-luna"
	InferenceReasoningEffort          = "medium"
	InferenceMaxRequests              = 256
	InferenceFinalizationTurnSlack    = 16
	InferencePolicySchema             = "dittobench-coding-inference-policy-v1"
	InferenceSystemPromptSchema       = "dittobench-coding-system-prompt-v1"
	InferenceToolSchemaSchema         = "dittobench-coding-model-tools-v1"
	InferenceResponseSchema           = "dittobench-coding-inference-response-v1"
	InferenceReceiptSchema            = "dittobench-coding-inference-receipt-v1"
	InferenceReceiptSetSchema         = "dittobench-coding-inference-receipt-set-v1"
	InferenceProviderSettlementSchema = "dittobench-coding-provider-settlement-v1"
	MaxInferencePolicyBytes           = 64 << 10
	MaxInferenceRequestBytes          = 4 << 20
	MaxInferenceResponseBytes         = 8 << 20
	MaxInferenceReceiptSetBytes       = 4 << 20
	MaxInferenceToolParameterBytes    = 64 << 10
	MaxInferenceToolArgumentBytes     = 64 << 10
	MaxInferenceMessages              = 512
	MaxInferenceTools                 = 64
	MaxInferenceToolCallsPerResponse  = 1
)

type InferencePolicy struct {
	Schema                        string `json:"schema"`
	CodingContractVersion         int    `json:"coding_contract_version"`
	BenchFamily                   string `json:"bench_family"`
	WeightEligible                bool   `json:"weight_eligible"`
	API                           string `json:"api"`
	Model                         string `json:"model"`
	ProviderAPI                   string `json:"provider_api"`
	ProviderRoute                 string `json:"provider_route"`
	ReceiptProvider               string `json:"receipt_provider"`
	ProviderReceiptSource         string `json:"provider_receipt_source"`
	ProviderAccountGuardrail      string `json:"provider_account_guardrail"`
	ProviderPipelinePolicy        string `json:"provider_pipeline_policy"`
	ProviderCachePolicy           string `json:"provider_cache_policy"`
	RouterMetadataRequired        bool   `json:"router_metadata_required"`
	ProviderRouteProfile          string `json:"provider_route_profile"`
	PromptSHA256                  string `json:"prompt_sha256"`
	ToolSchemaSHA256              string `json:"tool_schema_sha256"`
	ReasoningEffort               string `json:"reasoning_effort"`
	ReasoningExcluded             bool   `json:"reasoning_excluded"`
	Stream                        bool   `json:"stream"`
	Store                         bool   `json:"store"`
	N                             uint32 `json:"n"`
	ParallelToolCalls             bool   `json:"parallel_tool_calls"`
	MaxToolCallsPerResponse       uint32 `json:"max_tool_calls_per_response"`
	UsageIncluded                 bool   `json:"usage_included"`
	AllowFallbacks                bool   `json:"allow_fallbacks"`
	RequireParameters             bool   `json:"require_parameters"`
	DataCollection                string `json:"data_collection"`
	ZDR                           bool   `json:"zdr"`
	MaxRequests                   uint32 `json:"max_requests"`
	MaxPromptTokens               uint64 `json:"max_prompt_tokens"`
	MaxCompletionTokens           uint64 `json:"max_completion_tokens"`
	MaxTotalTokens                uint64 `json:"max_total_tokens"`
	MaxCompletionTokensPerRequest uint64 `json:"max_completion_tokens_per_request"`
	MaxCostUSDMicros              uint64 `json:"max_cost_usd_micros"`
	MaxRequestBytes               uint64 `json:"max_request_bytes"`
	MaxResponseBytes              uint64 `json:"max_response_bytes"`
	RequestTimeoutMilliseconds    uint64 `json:"request_timeout_milliseconds"`
	RetryPolicy                   string `json:"retry_policy"`
	MaxAttemptsPerRequest         uint32 `json:"max_attempts_per_request"`
	MaxRetries                    uint32 `json:"max_retries"`
	CostSource                    string `json:"cost_source"`
	Currency                      string `json:"currency"`
}

type InferenceSystemPrompt struct {
	Schema  string `json:"schema"`
	Content string `json:"content"`
}

type InferenceToolFunction struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

type InferenceTool struct {
	Type     string                `json:"type"`
	Function InferenceToolFunction `json:"function"`
}

type InferenceToolSchema struct {
	Schema string          `json:"schema"`
	Tools  []InferenceTool `json:"tools"`
}

type InferenceMinerReasoning struct {
	Effort string `json:"effort"`
}

type InferenceLockedReasoning struct {
	Effort  string `json:"effort"`
	Exclude bool   `json:"exclude"`
}

type InferenceUsageRequest struct {
	Include bool `json:"include"`
}

type InferenceProviderSelection struct {
	Only              []string `json:"only"`
	Order             []string `json:"order"`
	AllowFallbacks    bool     `json:"allow_fallbacks"`
	RequireParameters bool     `json:"require_parameters"`
	DataCollection    string   `json:"data_collection"`
	ZDR               bool     `json:"zdr"`
}

type InferenceMinerRequest struct {
	Model               string                  `json:"model"`
	Messages            []json.RawMessage       `json:"messages"`
	Tools               []InferenceTool         `json:"tools"`
	ToolChoice          string                  `json:"tool_choice"`
	Reasoning           InferenceMinerReasoning `json:"reasoning"`
	MaxCompletionTokens uint64                  `json:"max_completion_tokens"`
	ParallelToolCalls   bool                    `json:"parallel_tool_calls"`
}

type InferenceLockedRequest struct {
	Model               string                     `json:"model"`
	Messages            []json.RawMessage          `json:"messages"`
	Tools               []InferenceTool            `json:"tools"`
	ToolChoice          string                     `json:"tool_choice"`
	Reasoning           InferenceLockedReasoning   `json:"reasoning"`
	MaxCompletionTokens uint64                     `json:"max_completion_tokens"`
	ParallelToolCalls   bool                       `json:"parallel_tool_calls"`
	N                   uint32                     `json:"n"`
	Stream              bool                       `json:"stream"`
	Store               bool                       `json:"store"`
	Usage               InferenceUsageRequest      `json:"usage"`
	Provider            InferenceProviderSelection `json:"provider"`
}

type InferenceToolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type InferenceToolCall struct {
	ID       string                    `json:"id"`
	Type     string                    `json:"type"`
	Function InferenceToolCallFunction `json:"function"`
}

type InferenceResponseMessage struct {
	Content   *string             `json:"content"`
	ToolCalls []InferenceToolCall `json:"tool_calls"`
}

type InferenceResponseChoice struct {
	Message InferenceResponseMessage `json:"message"`
}

type InferenceMinerUsage struct {
	PromptTokens     uint64 `json:"prompt_tokens"`
	CompletionTokens uint64 `json:"completion_tokens"`
	TotalTokens      uint64 `json:"total_tokens"`
}

type InferenceMinerResponse struct {
	ID      string                    `json:"id"`
	Model   string                    `json:"model"`
	Choices []InferenceResponseChoice `json:"choices"`
	Usage   InferenceMinerUsage       `json:"usage"`
}

type InferenceProviderUsage struct {
	PromptTokens     uint64      `json:"prompt_tokens"`
	CompletionTokens uint64      `json:"completion_tokens"`
	TotalTokens      uint64      `json:"total_tokens"`
	Cost             json.Number `json:"cost"`
}

type InferenceProviderResponse struct {
	ID       string                    `json:"id"`
	Model    string                    `json:"model"`
	Provider string                    `json:"provider"`
	Choices  []InferenceResponseChoice `json:"choices"`
	Usage    InferenceProviderUsage    `json:"usage"`
}

type InferenceNormalizedUsage struct {
	PromptTokens     uint64 `json:"prompt_tokens"`
	CompletionTokens uint64 `json:"completion_tokens"`
	TotalTokens      uint64 `json:"total_tokens"`
	CostUSDMicros    uint64 `json:"cost_usd_micros"`
}

type InferenceNormalizedResponse struct {
	Schema   string                    `json:"schema"`
	ID       string                    `json:"id"`
	Model    string                    `json:"model"`
	Provider string                    `json:"provider"`
	Choices  []InferenceResponseChoice `json:"choices"`
	Usage    InferenceNormalizedUsage  `json:"usage"`
}

type InferenceReceiptOutcome string

const (
	InferenceReceiptFreeRetry      InferenceReceiptOutcome = "receipt_free_retry"
	InferenceReceiptComplete       InferenceReceiptOutcome = "complete"
	InferenceReceiptProviderFailed InferenceReceiptOutcome = "provider_failure"
)

type InferenceReceipt struct {
	Schema                   string                  `json:"schema"`
	Sequence                 uint32                  `json:"sequence"`
	RequestSequence          uint32                  `json:"request_sequence"`
	Attempt                  uint32                  `json:"attempt"`
	RequestID                string                  `json:"request_id"`
	LockedRequestSHA256      string                  `json:"locked_request_sha256"`
	PromptSHA256             string                  `json:"prompt_sha256"`
	ToolSchemaSHA256         string                  `json:"tool_schema_sha256"`
	Outcome                  InferenceReceiptOutcome `json:"outcome"`
	FailureCode              *string                 `json:"failure_code"`
	HTTPStatus               int                     `json:"http_status"`
	ResponseSHA256           *string                 `json:"response_sha256"`
	ResponseDigestKind       string                  `json:"response_digest_kind"`
	ProviderGenerationID     *string                 `json:"provider_generation_id"`
	ProviderSettlementSHA256 string                  `json:"provider_settlement_sha256"`
	Model                    string                  `json:"model"`
	ProviderRoute            string                  `json:"provider_route"`
	ProviderRouteProfile     string                  `json:"provider_route_profile"`
	ProviderSelected         bool                    `json:"provider_selected"`
	ReceiptProvider          *string                 `json:"receipt_provider"`
	FallbackUsed             bool                    `json:"fallback_used"`
	PromptTokens             uint64                  `json:"prompt_tokens"`
	CompletionTokens         uint64                  `json:"completion_tokens"`
	TotalTokens              uint64                  `json:"total_tokens"`
	CostUSDMicros            uint64                  `json:"cost_usd_micros"`
	TimedOut                 bool                    `json:"timed_out"`
}

type InferenceReceiptSet struct {
	Schema                string             `json:"schema"`
	CodingContractVersion int                `json:"coding_contract_version"`
	TicketID              string             `json:"ticket_id"`
	CaseID                string             `json:"case_id"`
	ProfileCapabilityID   string             `json:"profile_capability_id"`
	GrantID               string             `json:"grant_id"`
	Generation            uint32             `json:"generation"`
	InferenceGrantSHA256  string             `json:"inference_grant_sha256"`
	RequestBudget         uint32             `json:"request_budget"`
	PromptTokenBudget     uint64             `json:"prompt_token_budget"`
	CompletionTokenBudget uint64             `json:"completion_token_budget"`
	Receipts              []InferenceReceipt `json:"receipts"`
}

// InferenceReceiptBinding is trusted lease/live-grant authority supplied by
// the future gateway. Receipt-set bytes cannot choose these values.
type InferenceReceiptBinding struct {
	TicketID              string
	CaseID                string
	ProfileCapabilityID   string
	GrantID               string
	Generation            uint32
	InferenceGrantSHA256  string
	RequestBudget         uint32
	PromptTokenBudget     uint64
	CompletionTokenBudget uint64
}

type InferenceRouterAttempt struct {
	Provider string `json:"provider"`
	Selected bool   `json:"selected"`
}

// InferenceProviderSettlement is the replayable trusted Platform projection
// whose digest a provider receipt commits.
type InferenceProviderSettlement struct {
	Schema                   string                   `json:"schema"`
	CodingContractVersion    int                      `json:"coding_contract_version"`
	TicketID                 string                   `json:"ticket_id"`
	CaseID                   string                   `json:"case_id"`
	ProfileCapabilityID      string                   `json:"profile_capability_id"`
	InferenceGrantSHA256     string                   `json:"inference_grant_sha256"`
	GrantID                  string                   `json:"grant_id"`
	Generation               uint32                   `json:"generation"`
	RequestID                string                   `json:"request_id"`
	RequestSequence          uint32                   `json:"request_sequence"`
	Attempt                  uint32                   `json:"attempt"`
	LockedRequestSHA256      string                   `json:"locked_request_sha256"`
	Outcome                  InferenceReceiptOutcome  `json:"outcome"`
	TerminalErrorCode        *string                  `json:"terminal_error_code"`
	HTTPStatus               int                      `json:"http_status"`
	ResponseSHA256           *string                  `json:"response_sha256"`
	ResponseDigestKind       string                   `json:"response_digest_kind"`
	ProviderGenerationID     *string                  `json:"provider_generation_id"`
	Model                    string                   `json:"model"`
	ProviderAPI              string                   `json:"provider_api"`
	ProviderRoute            string                   `json:"provider_route"`
	ReceiptProvider          *string                  `json:"receipt_provider"`
	ProviderRouteProfile     string                   `json:"provider_route_profile"`
	ProviderAccountGuardrail string                   `json:"provider_account_guardrail"`
	ProviderPipelinePolicy   string                   `json:"provider_pipeline_policy"`
	ProviderCachePolicy      string                   `json:"provider_cache_policy"`
	RouterMetadataVerified   bool                     `json:"router_metadata_verified"`
	RouterAttempts           []InferenceRouterAttempt `json:"router_attempts"`
	PipelineStages           []string                 `json:"pipeline_stages"`
	FallbackUsed             bool                     `json:"fallback_used"`
	UsageAvailable           bool                     `json:"usage_available"`
	PromptTokens             uint64                   `json:"prompt_tokens"`
	CompletionTokens         uint64                   `json:"completion_tokens"`
	TotalTokens              uint64                   `json:"total_tokens"`
	CostAvailable            bool                     `json:"cost_available"`
	CostUSDMicros            uint64                   `json:"cost_usd_micros"`
	TimedOut                 bool                     `json:"timed_out"`
}
