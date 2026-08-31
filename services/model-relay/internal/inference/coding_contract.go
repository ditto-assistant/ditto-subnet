package inference

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
)

var codingBearerPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{32,128}$`)
var codingCostNumber = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$`)

const (
	codingDispatchRequestSchema       = "dittobench-coding-inference-dispatch-v1"
	codingDispatchResultSchema        = "dittobench-coding-inference-dispatch-result-v1"
	codingSettlementSchema            = "dittobench-coding-provider-settlement-v1"
	codingNormalizedSchema            = "dittobench-coding-inference-response-v1"
	codingSystemPromptSchema          = "dittobench-coding-system-prompt-v1"
	codingToolSchema                  = "dittobench-coding-model-tools-v1"
	codingModel                       = "openai/gpt-5.6-luna"
	codingProviderAPI                 = "openrouter"
	codingProviderRoute               = "azure/eu"
	codingReceiptProvider             = "Azure"
	codingProviderRouteProfile        = "luna-azure-eu-zdr-v1"
	codingAccountGuardrail            = "openrouter_private_account_v1"
	codingPipelinePolicy              = "no_plugins_no_transforms_v1"
	codingCachePolicy                 = "disabled_v1"
	codingReasoningEffort             = "medium"
	codingInferenceGrantSHA256        = "b2f38d9f6b5484e9a056d74be4dc0250912f05c9e51512801b590dff934a41d6"
	codingPromptSHA256                = "08eba8649a547ec94dc99ccf7b91e2553e4e570281b95d20afc779c78d764eed"
	codingToolSchemaSHA256            = "ca3b07083a5247941523fa51cab316fb2d4141e8907f9d7ca3d3471ceb3421eb"
	codingMaxRequests                 = 256
	codingMaxRetries                  = 100
	codingMaxAttempts                 = 3
	codingMaxMessages                 = 512
	codingMaxTools                    = 64
	codingMaxRequestBytes             = 4 << 20
	codingMaxResponseBytes            = 8 << 20
	codingMaxDispatchBytes            = codingMaxRequestBytes + 64<<10
	codingMaxSettlementBytes          = 64 << 10
	codingMaxCompletionPerCall        = 32_768
	codingMaxPromptTokens             = 2_000_000
	codingMaxCompletionTokens         = 250_000
	codingMaxTotalTokens              = 2_250_000
	codingMaxCostUSDMicros            = 10_000_000
	codingProviderTimeoutSeconds      = 300
	codingCanaryCaseID                = "PRACTICE-LEDGER-001"
	codingCanaryProfileID             = "public-certification-v1"
	codingCanaryRequestBudget         = 32
	codingCanaryPromptTokenBudget     = 10_000
	codingCanaryCompletionTokenBudget = 2_000
)

type codingDispatchRequest struct {
	Schema                string              `json:"schema"`
	CodingContractVersion int                 `json:"coding_contract_version"`
	WeightEligible        bool                `json:"weight_eligible"`
	TicketID              string              `json:"ticket_id"`
	CaseID                string              `json:"case_id"`
	ProfileCapabilityID   string              `json:"profile_capability_id"`
	InferenceGrantSHA256  string              `json:"inference_grant_sha256"`
	GrantID               string              `json:"grant_id"`
	Generation            int32               `json:"generation"`
	Sequence              int32               `json:"sequence"`
	RequestSequence       int32               `json:"request_sequence"`
	Attempt               int32               `json:"attempt"`
	RequestID             string              `json:"request_id"`
	LockedRequestSHA256   string              `json:"locked_request_sha256"`
	LockedRequest         codingLockedRequest `json:"locked_request"`
	Deadline              string              `json:"deadline"`
}

type codingReasoning struct {
	Effort  string `json:"effort"`
	Exclude bool   `json:"exclude"`
}

type codingUsageRequest struct {
	Include bool `json:"include"`
}

type codingProviderSelection struct {
	Only              []string `json:"only"`
	Order             []string `json:"order"`
	AllowFallbacks    bool     `json:"allow_fallbacks"`
	RequireParameters bool     `json:"require_parameters"`
	DataCollection    string   `json:"data_collection"`
	ZDR               bool     `json:"zdr"`
}

type codingToolFunction struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Parameters  json.RawMessage `json:"parameters"`
}

type codingTool struct {
	Type     string             `json:"type"`
	Function codingToolFunction `json:"function"`
}

type codingLockedRequest struct {
	Model               string                  `json:"model"`
	Messages            []json.RawMessage       `json:"messages"`
	Tools               []codingTool            `json:"tools"`
	ToolChoice          string                  `json:"tool_choice"`
	Reasoning           codingReasoning         `json:"reasoning"`
	MaxCompletionTokens int64                   `json:"max_completion_tokens"`
	ParallelToolCalls   bool                    `json:"parallel_tool_calls"`
	N                   int32                   `json:"n"`
	Stream              bool                    `json:"stream"`
	Store               bool                    `json:"store"`
	Usage               codingUsageRequest      `json:"usage"`
	Provider            codingProviderSelection `json:"provider"`
}

type codingToolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type codingToolCall struct {
	ID       string                 `json:"id"`
	Type     string                 `json:"type"`
	Function codingToolCallFunction `json:"function"`
}

type codingResponseMessage struct {
	Content   *string          `json:"content"`
	ToolCalls []codingToolCall `json:"tool_calls"`
}

type codingResponseChoice struct {
	Message codingResponseMessage `json:"message"`
}

type codingNormalizedUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
	TotalTokens      int64 `json:"total_tokens"`
	CostUSDMicros    int64 `json:"cost_usd_micros"`
}

type codingNormalizedResponse struct {
	Schema   string                 `json:"schema"`
	ID       string                 `json:"id"`
	Model    string                 `json:"model"`
	Provider string                 `json:"provider"`
	Choices  []codingResponseChoice `json:"choices"`
	Usage    codingNormalizedUsage  `json:"usage"`
}

type codingRouterAttempt struct {
	Provider string `json:"provider"`
	Selected bool   `json:"selected"`
}

type codingProviderSettlement struct {
	Schema                   string                `json:"schema"`
	CodingContractVersion    int                   `json:"coding_contract_version"`
	TicketID                 string                `json:"ticket_id"`
	CaseID                   string                `json:"case_id"`
	ProfileCapabilityID      string                `json:"profile_capability_id"`
	InferenceGrantSHA256     string                `json:"inference_grant_sha256"`
	GrantID                  string                `json:"grant_id"`
	Generation               int32                 `json:"generation"`
	RequestID                string                `json:"request_id"`
	RequestSequence          int32                 `json:"request_sequence"`
	Attempt                  int32                 `json:"attempt"`
	LockedRequestSHA256      string                `json:"locked_request_sha256"`
	Outcome                  string                `json:"outcome"`
	TerminalErrorCode        *string               `json:"terminal_error_code"`
	HTTPStatus               int                   `json:"http_status"`
	ResponseSHA256           *string               `json:"response_sha256"`
	ResponseDigestKind       string                `json:"response_digest_kind"`
	ProviderGenerationID     *string               `json:"provider_generation_id"`
	Model                    string                `json:"model"`
	ProviderAPI              string                `json:"provider_api"`
	ProviderRoute            string                `json:"provider_route"`
	ReceiptProvider          *string               `json:"receipt_provider"`
	ProviderRouteProfile     string                `json:"provider_route_profile"`
	ProviderAccountGuardrail string                `json:"provider_account_guardrail"`
	ProviderPipelinePolicy   string                `json:"provider_pipeline_policy"`
	ProviderCachePolicy      string                `json:"provider_cache_policy"`
	RouterMetadataVerified   bool                  `json:"router_metadata_verified"`
	RouterAttempts           []codingRouterAttempt `json:"router_attempts"`
	PipelineStages           []string              `json:"pipeline_stages"`
	FallbackUsed             bool                  `json:"fallback_used"`
	UsageAvailable           bool                  `json:"usage_available"`
	PromptTokens             int64                 `json:"prompt_tokens"`
	CompletionTokens         int64                 `json:"completion_tokens"`
	TotalTokens              int64                 `json:"total_tokens"`
	CostAvailable            bool                  `json:"cost_available"`
	CostUSDMicros            int64                 `json:"cost_usd_micros"`
	TimedOut                 bool                  `json:"timed_out"`
}

type codingDispatchResult struct {
	Schema                          string                   `json:"schema"`
	CodingContractVersion           int                      `json:"coding_contract_version"`
	WeightEligible                  bool                     `json:"weight_eligible"`
	Sequence                        int32                    `json:"sequence"`
	Settlement                      codingProviderSettlement `json:"settlement"`
	NormalizedResponseBase64        *string                  `json:"normalized_response_base64"`
	FailureResponseProjectionBase64 *string                  `json:"failure_response_projection_base64"`
}

func parseCodingDispatch(body []byte) (codingDispatchRequest, []byte, error) {
	var zero codingDispatchRequest
	if len(body) == 0 || len(body) > codingMaxDispatchBytes || !utf8.Valid(body) {
		return zero, nil, errors.New("coding dispatch size or Unicode is invalid")
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(body)
	if !ok || !validCodingJSON(decoded, 0) {
		return zero, nil, errors.New("coding dispatch JSON is invalid")
	}
	object, ok := decoded.(map[string]any)
	if !ok || !codingRequiredFields(object,
		"schema", "coding_contract_version", "weight_eligible", "ticket_id", "case_id",
		"profile_capability_id", "inference_grant_sha256", "grant_id", "generation",
		"sequence", "request_sequence", "attempt", "request_id", "locked_request_sha256",
		"locked_request", "deadline") {
		return zero, nil, errors.New("coding dispatch fields are invalid")
	}
	lockedObject, ok := object["locked_request"].(map[string]any)
	if !ok || validateCodingLockedShape(lockedObject) != nil {
		return zero, nil, errors.New("coding locked request shape is invalid")
	}
	var request codingDispatchRequest
	if err := json.Unmarshal(body, &request); err != nil {
		return zero, nil, err
	}
	if request.Schema != codingDispatchRequestSchema || request.CodingContractVersion != 1 || request.WeightEligible ||
		!canonicalCodingUUID(request.TicketID) || !validCodingIdentifier(request.CaseID, 256) ||
		!validCodingIdentifier(request.ProfileCapabilityID, 256) || request.InferenceGrantSHA256 != codingInferenceGrantSHA256 ||
		!canonicalCodingUUID(request.GrantID) || request.Generation < 1 || request.Generation > math.MaxInt32 ||
		request.Sequence < 1 || request.Sequence > codingMaxRequests+codingMaxRetries ||
		request.RequestSequence < 1 || request.RequestSequence > codingMaxRequests ||
		request.Attempt < 1 || request.Attempt > codingMaxAttempts || !canonicalCodingUUID(request.RequestID) ||
		!validCodingSHA256(request.LockedRequestSHA256) {
		return zero, nil, errors.New("coding dispatch authority is invalid")
	}
	deadline, err := time.Parse(time.RFC3339Nano, request.Deadline)
	if err != nil || deadline.Location() == nil || !deadline.After(time.Unix(0, 0)) {
		return zero, nil, errors.New("coding dispatch deadline is invalid")
	}
	lockedBytes, err := compactJSON(request.LockedRequest)
	if err != nil || len(lockedBytes) > codingMaxRequestBytes || validateCodingLocked(request.LockedRequest) != nil {
		return zero, nil, errors.New("coding locked request is invalid")
	}
	lockedSHA256, err := codingCanonicalSHA256(request.LockedRequest)
	if err != nil || lockedSHA256 != request.LockedRequestSHA256 {
		return zero, nil, errors.New("coding locked request digest disagrees")
	}
	return request, lockedBytes, nil
}

func codingRequiredFields(object map[string]any, fields ...string) bool {
	for _, field := range fields {
		if _, present := object[field]; !present {
			return false
		}
	}
	return true
}

func validateCodingLocked(request codingLockedRequest) error {
	if request.Model != codingModel || len(request.Messages) == 0 || len(request.Messages) > codingMaxMessages ||
		len(request.Tools) == 0 || len(request.Tools) > codingMaxTools || request.ToolChoice != "auto" ||
		request.Reasoning.Effort != codingReasoningEffort || !request.Reasoning.Exclude ||
		request.MaxCompletionTokens < 1 || request.MaxCompletionTokens > codingMaxCompletionPerCall ||
		request.ParallelToolCalls || request.N != 1 || request.Stream || request.Store || !request.Usage.Include ||
		len(request.Provider.Only) != 1 || len(request.Provider.Order) != 1 ||
		request.Provider.Only[0] != codingProviderRoute || request.Provider.Order[0] != codingProviderRoute ||
		request.Provider.AllowFallbacks || !request.Provider.RequireParameters ||
		request.Provider.DataCollection != "deny" || !request.Provider.ZDR {
		return errors.New("coding locked request escaped policy")
	}
	seen := make(map[string]struct{}, len(request.Tools))
	for _, tool := range request.Tools {
		if tool.Type != "function" || !validCodingIdentifier(tool.Function.Name, 128) ||
			tool.Function.Description == "" || len([]byte(tool.Function.Description)) > 2_000 ||
			len(tool.Function.Parameters) == 0 || len(tool.Function.Parameters) > 64<<10 {
			return errors.New("coding tool is invalid")
		}
		if _, duplicate := seen[tool.Function.Name]; duplicate {
			return errors.New("coding tool name is duplicated")
		}
		seen[tool.Function.Name] = struct{}{}
		parameters, ok := decodeJSONNumbersRejectDuplicateKeys(tool.Function.Parameters)
		if !ok {
			return errors.New("coding tool parameters are invalid")
		}
		if _, object := parameters.(map[string]any); !object || !codingJSONUsesCanonicalIntegers(parameters) {
			return errors.New("coding tool parameters are not canonical")
		}
	}
	toolDigest, err := codingCanonicalSHA256(struct {
		Schema string       `json:"schema"`
		Tools  []codingTool `json:"tools"`
	}{Schema: codingToolSchema, Tools: request.Tools})
	if err != nil || toolDigest != codingToolSchemaSHA256 {
		return errors.New("coding tool schema disagrees with policy")
	}
	for index, message := range request.Messages {
		decoded, ok := decodeJSONNumbersRejectDuplicateKeys(message)
		object, objectOK := decoded.(map[string]any)
		if !ok || !objectOK || validateCodingMessageShape(object) != nil {
			return fmt.Errorf("coding message %d is invalid", index)
		}
	}
	var first struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	if json.Unmarshal(request.Messages[0], &first) != nil || first.Role != "system" || first.Content == "" {
		return errors.New("coding fixed system prompt is missing")
	}
	promptDigest, err := codingCanonicalSHA256(struct {
		Schema  string `json:"schema"`
		Content string `json:"content"`
	}{Schema: codingSystemPromptSchema, Content: first.Content})
	if err != nil || promptDigest != codingPromptSHA256 {
		return errors.New("coding system prompt disagrees with policy")
	}
	return nil
}

func validateCodingLockedShape(object map[string]any) error {
	if !onlyJSONKeys(object, "model", "messages", "tools", "tool_choice", "reasoning",
		"max_completion_tokens", "parallel_tool_calls", "n", "stream", "store", "usage", "provider") || len(object) != 12 {
		return errors.New("locked request fields are invalid")
	}
	reasoning, ok := object["reasoning"].(map[string]any)
	if !ok || !onlyJSONKeys(reasoning, "effort", "exclude") || len(reasoning) != 2 {
		return errors.New("reasoning fields are invalid")
	}
	usage, ok := object["usage"].(map[string]any)
	if !ok || !onlyJSONKeys(usage, "include") || len(usage) != 1 {
		return errors.New("usage fields are invalid")
	}
	provider, ok := object["provider"].(map[string]any)
	if !ok || !onlyJSONKeys(provider, "only", "order", "allow_fallbacks", "require_parameters", "data_collection", "zdr") || len(provider) != 6 {
		return errors.New("provider fields are invalid")
	}
	messages, ok := object["messages"].([]any)
	if !ok || len(messages) == 0 || len(messages) > codingMaxMessages {
		return errors.New("messages are invalid")
	}
	for _, raw := range messages {
		message, ok := raw.(map[string]any)
		if !ok || validateCodingMessageShape(message) != nil {
			return errors.New("message shape is invalid")
		}
	}
	tools, ok := object["tools"].([]any)
	if !ok || len(tools) == 0 || len(tools) > codingMaxTools {
		return errors.New("tools are invalid")
	}
	for _, raw := range tools {
		tool, ok := raw.(map[string]any)
		if !ok || !onlyJSONKeys(tool, "type", "function") || len(tool) != 2 {
			return errors.New("tool shape is invalid")
		}
		function, ok := tool["function"].(map[string]any)
		if !ok || !onlyJSONKeys(function, "name", "description", "parameters") || len(function) != 3 {
			return errors.New("tool function shape is invalid")
		}
	}
	return nil
}

func validateCodingMessageShape(message map[string]any) error {
	role, ok := message["role"].(string)
	if !ok {
		return errors.New("message role is invalid")
	}
	switch role {
	case "system", "user":
		if !onlyJSONKeys(message, "role", "content") || len(message) != 2 {
			return errors.New("message fields are invalid")
		}
		if _, ok := message["content"].(string); !ok {
			return errors.New("message content is invalid")
		}
	case "assistant":
		if !onlyJSONKeys(message, "role", "content", "tool_calls") || len(message) != 3 {
			return errors.New("assistant fields are invalid")
		}
		if content := message["content"]; content != nil {
			if _, ok := content.(string); !ok {
				return errors.New("assistant content is invalid")
			}
		}
		calls, ok := message["tool_calls"].([]any)
		if !ok || len(calls) > 1 {
			return errors.New("assistant tool calls are invalid")
		}
		for _, raw := range calls {
			if validateCodingToolCallShape(raw) != nil {
				return errors.New("assistant tool call is invalid")
			}
		}
	case "tool":
		if !onlyJSONKeys(message, "role", "tool_call_id", "content") || len(message) != 3 ||
			!validCodingIdentifier(asString(message["tool_call_id"]), 256) {
			return errors.New("tool message is invalid")
		}
		if _, ok := message["content"].(string); !ok {
			return errors.New("tool message content is invalid")
		}
	default:
		return errors.New("message role is unsupported")
	}
	return nil
}

func validateCodingToolCallShape(raw any) error {
	call, ok := raw.(map[string]any)
	if !ok || !onlyJSONKeys(call, "id", "type", "function") || len(call) != 3 ||
		!validCodingIdentifier(asString(call["id"]), 256) || call["type"] != "function" {
		return errors.New("tool call identity is invalid")
	}
	function, ok := call["function"].(map[string]any)
	if !ok || !onlyJSONKeys(function, "name", "arguments") || len(function) != 2 ||
		!validCodingIdentifier(asString(function["name"]), 128) {
		return errors.New("tool call function is invalid")
	}
	arguments, ok := function["arguments"].(string)
	if !ok || len([]byte(arguments)) == 0 || len([]byte(arguments)) > 64<<10 {
		return errors.New("tool call arguments are invalid")
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys([]byte(arguments))
	if !ok || !validCodingJSON(decoded, 0) {
		return errors.New("tool call arguments JSON is invalid")
	}
	if _, object := decoded.(map[string]any); !object {
		return errors.New("tool call arguments are not an object")
	}
	return nil
}

func validCodingJSON(value any, depth int) bool {
	if depth > 64 {
		return false
	}
	switch typed := value.(type) {
	case nil, bool:
		return true
	case string:
		return utf8.ValidString(typed)
	case json.Number:
		raw := typed.String()
		return validCodingNumberLexeme(raw)
	case []any:
		for _, item := range typed {
			if !validCodingJSON(item, depth+1) {
				return false
			}
		}
		return true
	case map[string]any:
		for key, item := range typed {
			if !utf8.ValidString(key) || !validCodingJSON(item, depth+1) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func validCodingNumberLexeme(raw string) bool {
	if raw == "" || len(raw) > 100 || raw == "-0" {
		return false
	}
	if exponentAt := strings.IndexAny(raw, "eE"); exponentAt >= 0 {
		exponent, err := strconv.Atoi(raw[exponentAt+1:])
		if err != nil || exponent < -100 || exponent > 100 {
			return false
		}
	}
	return true
}

func codingJSONUsesCanonicalIntegers(value any) bool {
	switch typed := value.(type) {
	case nil, bool, string:
		return true
	case json.Number:
		raw := typed.String()
		if raw == "-0" || strings.ContainsAny(raw, ".eE") {
			return false
		}
		_, err := strconv.ParseInt(raw, 10, 64)
		return err == nil
	case []any:
		for _, item := range typed {
			if !codingJSONUsesCanonicalIntegers(item) {
				return false
			}
		}
		return true
	case map[string]any:
		for _, item := range typed {
			if !codingJSONUsesCanonicalIntegers(item) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func codingCanonicalSHA256(value any) (string, error) {
	body, err := codingCanonicalJSON(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	return hex.EncodeToString(digest[:]), nil
}

func codingCanonicalJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projected any
	if err := decoder.Decode(&projected); err != nil {
		return nil, err
	}
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(projected); err != nil {
		return nil, err
	}
	return output.Bytes(), nil
}

func canonicalCodingUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func validCodingSHA256(value string) bool {
	if len(value) != sha256.Size*2 || strings.ToLower(value) != value {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validCodingIdentifier(value string, maximum int) bool {
	if value == "" || !utf8.ValidString(value) || len([]byte(value)) > maximum {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func asString(value any) string {
	stringValue, _ := value.(string)
	return stringValue
}

func codingFloatToMicros(value any) (int64, bool) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, false
	}
	raw := number.String()
	if len(raw) == 0 || len(raw) > 64 || !codingCostNumber.MatchString(raw) || !validCodingNumberLexeme(raw) {
		return 0, false
	}
	cost, ok := new(big.Rat).SetString(raw)
	if !ok || cost.Sign() < 0 || cost.Cmp(big.NewRat(100, 1)) > 0 {
		return 0, false
	}
	scaled := new(big.Rat).Mul(cost, big.NewRat(1_000_000, 1))
	quotient := new(big.Int).Quo(scaled.Num(), scaled.Denom())
	remainder := new(big.Int).Rem(scaled.Num(), scaled.Denom())
	comparison := new(big.Int).Lsh(remainder, 1).Cmp(scaled.Denom())
	if comparison > 0 || (comparison == 0 && quotient.Bit(0) == 1) {
		quotient.Add(quotient, big.NewInt(1))
	}
	if !quotient.IsInt64() {
		return 0, false
	}
	return quotient.Int64(), true
}

func codingInt64(value any) (int64, bool) {
	number, ok := isIntLiteral(value)
	if !ok {
		return 0, false
	}
	parsed, err := number.Int64()
	return parsed, err == nil
}

func decodeCodingJSON(body []byte) (map[string]any, bool) {
	if len(body) == 0 || len(body) > codingMaxResponseBytes || !utf8.Valid(body) {
		return nil, false
	}
	decoded, ok := decodeJSONNumbersRejectDuplicateKeys(body)
	if !ok || !validCodingJSON(decoded, 0) {
		return nil, false
	}
	object, ok := decoded.(map[string]any)
	return object, ok
}
