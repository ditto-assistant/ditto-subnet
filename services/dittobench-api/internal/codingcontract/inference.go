package codingcontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
)

const maxInferenceCostUSDMicros uint64 = 100_000_000

var inferenceFailureCodes = []string{
	"pre_provider_unavailable",
	"provider_http",
	"provider_response_invalid",
	"provider_timeout",
	"provider_transport",
}

var inferenceCostNumber = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$`)

func ParseInferencePolicy(body []byte) (InferencePolicy, error) {
	return parseInferenceDocument(body, MaxInferencePolicyBytes, validateInferencePolicyShape, func(value InferencePolicy) error {
		return value.Validate()
	})
}

func ParseInferenceSystemPrompt(body []byte) (InferenceSystemPrompt, error) {
	return parseInferenceDocument(body, MaxInferencePolicyBytes, func(shape map[string]any) error {
		return requireFields(shape, "$", "schema", "content")
	}, func(value InferenceSystemPrompt) error { return value.Validate() })
}

func ParseInferenceToolSchema(body []byte) (InferenceToolSchema, error) {
	return parseInferenceDocument(body, MaxInferenceRequestBytes, validateInferenceToolSchemaShape, func(value InferenceToolSchema) error {
		return value.Validate()
	})
}

func ParseInferenceMinerRequest(body []byte, policy InferencePolicy) (InferenceMinerRequest, error) {
	maximum, err := inferenceRequestMaximum(policy)
	if err != nil {
		return InferenceMinerRequest{}, err
	}
	return parseInferenceDocument(body, maximum, validateInferenceMinerRequestShape, func(value InferenceMinerRequest) error {
		return value.ValidateAgainst(policy)
	})
}

func ParseInferenceLockedRequest(body []byte, policy InferencePolicy) (InferenceLockedRequest, error) {
	maximum, err := inferenceRequestMaximum(policy)
	if err != nil {
		return InferenceLockedRequest{}, err
	}
	return parseInferenceDocument(body, maximum, validateInferenceLockedRequestShape, func(value InferenceLockedRequest) error {
		return value.ValidateAgainst(policy)
	})
}

func ParseInferenceMinerResponse(body []byte, policy InferencePolicy) (InferenceMinerResponse, error) {
	maximum, err := inferenceResponseMaximum(policy)
	if err != nil {
		return InferenceMinerResponse{}, err
	}
	return parseInferenceDocument(body, maximum, validateInferenceMinerResponseShape, func(value InferenceMinerResponse) error {
		return value.ValidateAgainst(policy)
	})
}

func ParseInferenceProviderResponse(body []byte, policy InferencePolicy) (InferenceProviderResponse, error) {
	maximum, err := inferenceResponseMaximum(policy)
	if err != nil {
		return InferenceProviderResponse{}, err
	}
	return parseInferenceDocument(body, maximum, validateInferenceProviderResponseShape, func(value InferenceProviderResponse) error {
		return value.ValidateAgainst(policy)
	})
}

func ParseInferenceNormalizedResponse(body []byte, policy InferencePolicy) (InferenceNormalizedResponse, error) {
	maximum, err := inferenceResponseMaximum(policy)
	if err != nil {
		return InferenceNormalizedResponse{}, err
	}
	return parseInferenceDocument(body, maximum, validateInferenceNormalizedResponseShape, func(value InferenceNormalizedResponse) error {
		return value.ValidateAgainst(policy)
	})
}

func ParseInferenceReceiptSet(body []byte, policy InferencePolicy) (InferenceReceiptSet, error) {
	return parseInferenceDocument(body, MaxInferenceReceiptSetBytes, validateInferenceReceiptSetShape, func(value InferenceReceiptSet) error {
		_, err := validateInferenceReceiptSet(policy, value)
		return err
	})
}

func ParseInferenceProviderSettlement(
	body []byte,
	policy InferencePolicy,
) (InferenceProviderSettlement, error) {
	return parseInferenceDocument(
		body,
		MaxInferenceReceiptSetBytes,
		validateInferenceProviderSettlementShape,
		func(value InferenceProviderSettlement) error { return value.Validate(policy) },
	)
}

func parseInferenceDocument[T any](
	body []byte,
	maximum int,
	shape func(map[string]any) error,
	validate func(T) error,
) (T, error) {
	var zero T
	if err := ValidateJSONDocument(body, maximum); err != nil {
		return zero, err
	}
	if err := validateInferenceNumberLexemes(body); err != nil {
		return zero, err
	}
	shapeDecoder := json.NewDecoder(bytes.NewReader(body))
	shapeDecoder.UseNumber()
	var object map[string]any
	if err := shapeDecoder.Decode(&object); err != nil {
		return zero, err
	}
	if err := requireEOF(shapeDecoder); err != nil {
		return zero, err
	}
	if err := shape(object); err != nil {
		return zero, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var value T
	if err := decoder.Decode(&value); err != nil {
		return zero, err
	}
	if err := requireEOF(decoder); err != nil {
		return zero, err
	}
	if err := validate(value); err != nil {
		return zero, err
	}
	return value, nil
}

func validateInferenceNumberLexemes(body []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		number, ok := token.(json.Number)
		if !ok {
			continue
		}
		raw := number.String()
		if strings.ContainsAny(raw, ".eE") {
			if len(raw) > 64 {
				return errors.New("coding inference JSON decimal spelling is outside bounds")
			}
			if exponentAt := strings.IndexAny(raw, "eE"); exponentAt >= 0 {
				exponent, parseErr := strconv.Atoi(raw[exponentAt+1:])
				if parseErr != nil || exponent < -100 || exponent > 100 {
					return errors.New("coding inference JSON decimal exponent is outside bounds")
				}
			}
			continue
		}
		digits := strings.TrimPrefix(raw, "-")
		if raw == "-0" || len(digits) > 100 {
			return errors.New("coding inference JSON integer spelling is outside bounds")
		}
	}
}

func InferencePolicySHA256(policy InferencePolicy) (string, error) {
	if err := policy.Validate(); err != nil {
		return "", err
	}
	return inferenceDigest(policy, MaxInferencePolicyBytes)
}

func InferenceSystemPromptSHA256(prompt InferenceSystemPrompt) (string, error) {
	if err := prompt.Validate(); err != nil {
		return "", err
	}
	return inferenceDigest(prompt, MaxInferencePolicyBytes)
}

func InferenceToolSchemaSHA256(schema InferenceToolSchema) (string, error) {
	if err := schema.Validate(); err != nil {
		return "", err
	}
	return inferenceDigest(schema, MaxInferenceRequestBytes)
}

func InferenceMinerRequestSHA256(policy InferencePolicy, request InferenceMinerRequest) (string, error) {
	if err := request.ValidateAgainst(policy); err != nil {
		return "", err
	}
	maximum, _ := inferenceRequestMaximum(policy)
	return inferenceDigest(request, maximum)
}

func InferenceLockedRequestSHA256(policy InferencePolicy, request InferenceLockedRequest) (string, error) {
	if err := request.ValidateAgainst(policy); err != nil {
		return "", err
	}
	maximum, _ := inferenceRequestMaximum(policy)
	return inferenceDigest(request, maximum)
}

func InferenceMinerResponseSHA256(policy InferencePolicy, response InferenceMinerResponse) (string, error) {
	response.Choices = normalizeInferenceChoices(response.Choices)
	if err := response.ValidateAgainst(policy); err != nil {
		return "", err
	}
	maximum, _ := inferenceResponseMaximum(policy)
	return inferenceDigest(response, maximum)
}

func InferenceNormalizedResponseSHA256(policy InferencePolicy, response InferenceNormalizedResponse) (string, error) {
	response.Choices = normalizeInferenceChoices(response.Choices)
	if err := response.ValidateAgainst(policy); err != nil {
		return "", err
	}
	maximum, _ := inferenceResponseMaximum(policy)
	return inferenceDigest(response, maximum)
}

// InferenceModelEvidenceSHA256 freezes the standalone relay vector projection.
// Task evidence remains signable only through TaskEvidenceDigest.
func InferenceModelEvidenceSHA256(policy InferencePolicy, evidence ModelEvidence) (string, error) {
	grantDigest, err := InferencePolicySHA256(policy)
	if err != nil {
		return "", err
	}
	if err := evidence.Validate(); err != nil {
		return "", err
	}
	if evidence.Model != policy.Model ||
		evidence.Provider != policy.ProviderRoute || evidence.ProviderRouteProfile != policy.ProviderRouteProfile ||
		evidence.ReasoningEffort != policy.ReasoningEffort || evidence.InferenceGrantSHA256 != grantDigest ||
		evidence.PromptSHA256 != policy.PromptSHA256 || evidence.ToolSchemaSHA256 != policy.ToolSchemaSHA256 ||
		evidence.FallbackUsed != policy.AllowFallbacks || evidence.CostSource != policy.CostSource ||
		evidence.Currency != policy.Currency {
		return "", errors.New("coding inference model evidence disagrees with policy")
	}
	if evidence.Requests > uint64(policy.MaxRequests) || evidence.PromptTokens > policy.MaxPromptTokens ||
		evidence.CompletionTokens > policy.MaxCompletionTokens || evidence.TotalTokens > policy.MaxTotalTokens ||
		evidence.CostUSDMicros > policy.MaxCostUSDMicros || evidence.RetryCount > policy.MaxRetries {
		return "", errors.New("coding inference model evidence exceeds policy")
	}
	return inferenceDigest(evidence, MaxCanonicalJSONBytes)
}

func inferenceDigest(value any, maximum int) (string, error) {
	body, err := inferenceCanonicalJSON(value, maximum)
	if err != nil {
		return "", err
	}
	return digestBytes(body), nil
}

func inferenceCanonicalJSON(value any, maximum int) ([]byte, error) {
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
	if output.Len() == 0 || output.Len() > maximum {
		return nil, errors.New("canonical coding inference JSON exceeds its bound")
	}
	return output.Bytes(), nil
}

func (policy InferencePolicy) Validate() error {
	if policy.Schema != InferencePolicySchema || policy.CodingContractVersion != ContractVersion ||
		policy.BenchFamily != "coding" || policy.WeightEligible ||
		policy.API != "openai-compatible-chat-completions" || policy.Model != InferenceSolverModel ||
		policy.ProviderAPI != "openrouter" || !validIdentifier(policy.ProviderRoute, 128) ||
		!validIdentifier(policy.ReceiptProvider, 128) || !validIdentifier(policy.ProviderRouteProfile, 128) ||
		policy.ProviderReceiptSource != "platform_settlement_v1" ||
		policy.ProviderAccountGuardrail != "openrouter_private_account_v1" ||
		policy.ProviderPipelinePolicy != "no_plugins_no_transforms_v1" ||
		policy.ProviderCachePolicy != "disabled_v1" || !policy.RouterMetadataRequired ||
		!validSHA256(policy.PromptSHA256) || !validSHA256(policy.ToolSchemaSHA256) ||
		policy.ReasoningEffort != InferenceReasoningEffort || !policy.ReasoningExcluded || policy.Stream || policy.Store ||
		policy.N != 1 || policy.ParallelToolCalls || policy.MaxToolCallsPerResponse != MaxInferenceToolCallsPerResponse ||
		!policy.UsageIncluded || policy.AllowFallbacks ||
		!policy.RequireParameters || policy.DataCollection != "deny" || !policy.ZDR ||
		policy.RetryPolicy != "receipt_free_pre_provider_v1" ||
		policy.CostSource != "provider_receipt_v1" || policy.Currency != "USD" {
		return errors.New("coding inference policy identity or lock is invalid")
	}
	if policy.MaxRequests != InferenceMaxRequests ||
		policy.MaxPromptTokens == 0 || policy.MaxPromptTokens > 2_000_000 ||
		policy.MaxCompletionTokens == 0 || policy.MaxCompletionTokens > 250_000 ||
		policy.MaxTotalTokens == 0 || policy.MaxTotalTokens > 2_250_000 ||
		policy.MaxPromptTokens > ^uint64(0)-policy.MaxCompletionTokens ||
		policy.MaxTotalTokens != policy.MaxPromptTokens+policy.MaxCompletionTokens ||
		policy.MaxCompletionTokensPerRequest == 0 || policy.MaxCompletionTokensPerRequest > 32_768 ||
		policy.MaxCompletionTokensPerRequest > policy.MaxCompletionTokens ||
		policy.MaxCostUSDMicros == 0 || policy.MaxCostUSDMicros > maxInferenceCostUSDMicros ||
		policy.MaxRequestBytes != MaxInferenceRequestBytes || policy.MaxResponseBytes != MaxInferenceResponseBytes ||
		policy.RequestTimeoutMilliseconds < 1_000 || policy.RequestTimeoutMilliseconds > 300_000 ||
		policy.RequestTimeoutMilliseconds%1_000 != 0 || policy.MaxAttemptsPerRequest == 0 ||
		policy.MaxAttemptsPerRequest > 3 || policy.MaxRetries > 100 ||
		uint64(policy.MaxRequests)+uint64(policy.MaxRetries) > 1_100 {
		return errors.New("coding inference policy budget is invalid")
	}
	return nil
}

// EffectiveInferenceRequestBudget mirrors the reference agent's bounded turn
// loop: one model request per workspace-tool turn plus a fixed finalization
// allowance, capped by the public relay policy.
func EffectiveInferenceRequestBudget(workspaceToolCalls uint32) uint32 {
	if workspaceToolCalls >= InferenceMaxRequests-InferenceFinalizationTurnSlack {
		return InferenceMaxRequests
	}
	return workspaceToolCalls + InferenceFinalizationTurnSlack
}

func (prompt InferenceSystemPrompt) Validate() error {
	if prompt.Schema != InferenceSystemPromptSchema || prompt.Content == "" ||
		len(prompt.Content) > 64<<10 || !utf8.ValidString(prompt.Content) {
		return errors.New("coding inference system prompt is invalid")
	}
	return nil
}

func (schema InferenceToolSchema) Validate() error {
	if schema.Schema != InferenceToolSchemaSchema || schema.Tools == nil ||
		len(schema.Tools) == 0 || len(schema.Tools) > MaxInferenceTools {
		return errors.New("coding inference tool schema is invalid")
	}
	seen := make(map[string]struct{}, len(schema.Tools))
	for _, tool := range schema.Tools {
		if err := tool.Validate(); err != nil {
			return err
		}
		if _, duplicate := seen[tool.Function.Name]; duplicate {
			return errors.New("coding inference tool names are not unique")
		}
		seen[tool.Function.Name] = struct{}{}
	}
	return nil
}

func (tool InferenceTool) Validate() error {
	if tool.Type != "function" || !validIdentifier(tool.Function.Name, 128) ||
		tool.Function.Description == "" || len(tool.Function.Description) > 2_000 ||
		!utf8.ValidString(tool.Function.Description) || len(tool.Function.Parameters) == 0 ||
		len(tool.Function.Parameters) > MaxInferenceToolParameterBytes {
		return errors.New("coding inference tool definition is invalid")
	}
	if err := ValidateJSONDocument(tool.Function.Parameters, MaxInferenceToolParameterBytes); err != nil {
		return errors.New("coding inference tool parameters are invalid")
	}
	var object map[string]any
	decoder := json.NewDecoder(bytes.NewReader(tool.Function.Parameters))
	decoder.UseNumber()
	if err := decoder.Decode(&object); err != nil || object == nil || !inferenceJSONUsesCanonicalIntegers(object) {
		return errors.New("coding inference tool parameters must be an object")
	}
	return nil
}

func inferenceJSONUsesCanonicalIntegers(value any) bool {
	switch typed := value.(type) {
	case nil, bool, string:
		return true
	case json.Number:
		raw := typed.String()
		if raw == "-0" {
			return false
		}
		_, err := strconv.ParseInt(raw, 10, 64)
		return err == nil
	case []any:
		for _, item := range typed {
			if !inferenceJSONUsesCanonicalIntegers(item) {
				return false
			}
		}
		return true
	case map[string]any:
		for _, item := range typed {
			if !inferenceJSONUsesCanonicalIntegers(item) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func (request InferenceMinerRequest) ValidateAgainst(policy InferencePolicy) error {
	if err := policy.Validate(); err != nil {
		return err
	}
	if request.Model != policy.Model || request.Messages == nil || len(request.Messages) == 0 ||
		len(request.Messages) > MaxInferenceMessages || request.Tools == nil ||
		request.ToolChoice != "auto" || request.Reasoning.Effort != policy.ReasoningEffort ||
		request.MaxCompletionTokens == 0 || request.MaxCompletionTokens > policy.MaxCompletionTokensPerRequest ||
		request.ParallelToolCalls != policy.ParallelToolCalls {
		return errors.New("coding inference miner request violates the policy")
	}
	if err := validateInferenceMessages(request.Messages); err != nil {
		return err
	}
	schema := InferenceToolSchema{Schema: InferenceToolSchemaSchema, Tools: request.Tools}
	toolDigest, err := InferenceToolSchemaSHA256(schema)
	if err != nil || toolDigest != policy.ToolSchemaSHA256 {
		return errors.New("coding inference request tool schema disagrees with policy")
	}
	prompt, err := systemPromptFromMessages(request.Messages)
	if err != nil {
		return err
	}
	promptDigest, err := InferenceSystemPromptSHA256(prompt)
	if err != nil || promptDigest != policy.PromptSHA256 {
		return errors.New("coding inference request system prompt disagrees with policy")
	}
	return nil
}

func (request InferenceLockedRequest) ValidateAgainst(policy InferencePolicy) error {
	miner := InferenceMinerRequest{
		Model: request.Model, Messages: request.Messages, Tools: request.Tools,
		ToolChoice: request.ToolChoice, Reasoning: InferenceMinerReasoning{Effort: request.Reasoning.Effort},
		MaxCompletionTokens: request.MaxCompletionTokens, ParallelToolCalls: request.ParallelToolCalls,
	}
	if err := miner.ValidateAgainst(policy); err != nil {
		return err
	}
	selection := request.Provider
	if request.Reasoning.Exclude != policy.ReasoningExcluded || request.N != policy.N ||
		request.Stream != policy.Stream || request.Store != policy.Store || request.Usage.Include != policy.UsageIncluded ||
		!slices.Equal(selection.Only, []string{policy.ProviderRoute}) ||
		!slices.Equal(selection.Order, []string{policy.ProviderRoute}) ||
		selection.AllowFallbacks != policy.AllowFallbacks || selection.RequireParameters != policy.RequireParameters ||
		selection.DataCollection != policy.DataCollection || selection.ZDR != policy.ZDR {
		return errors.New("coding inference locked request escaped the policy")
	}
	return nil
}

func LockInferenceRequest(policy InferencePolicy, request InferenceMinerRequest) (InferenceLockedRequest, error) {
	if err := request.ValidateAgainst(policy); err != nil {
		return InferenceLockedRequest{}, err
	}
	return InferenceLockedRequest{
		Model: request.Model, Messages: cloneRawMessages(request.Messages), Tools: cloneInferenceTools(request.Tools),
		ToolChoice:          request.ToolChoice,
		Reasoning:           InferenceLockedReasoning{Effort: policy.ReasoningEffort, Exclude: policy.ReasoningExcluded},
		MaxCompletionTokens: request.MaxCompletionTokens, ParallelToolCalls: policy.ParallelToolCalls,
		N: policy.N, Stream: policy.Stream, Store: policy.Store, Usage: InferenceUsageRequest{Include: policy.UsageIncluded},
		Provider: InferenceProviderSelection{
			Only: []string{policy.ProviderRoute}, Order: []string{policy.ProviderRoute},
			AllowFallbacks: policy.AllowFallbacks, RequireParameters: policy.RequireParameters,
			DataCollection: policy.DataCollection, ZDR: policy.ZDR,
		},
	}, nil
}

func (response InferenceMinerResponse) ValidateAgainst(policy InferencePolicy) error {
	if err := policy.Validate(); err != nil {
		return err
	}
	if !validIdentifier(response.ID, 256) ||
		response.Model != policy.Model || len(response.Choices) != 1 ||
		response.Usage.PromptTokens > ^uint64(0)-response.Usage.CompletionTokens ||
		response.Usage.TotalTokens != response.Usage.PromptTokens+response.Usage.CompletionTokens ||
		response.Usage.PromptTokens > policy.MaxPromptTokens ||
		response.Usage.CompletionTokens > policy.MaxCompletionTokensPerRequest ||
		response.Usage.TotalTokens > policy.MaxTotalTokens {
		return errors.New("coding inference miner response is invalid")
	}
	return response.Choices[0].Validate()
}

func (response InferenceProviderResponse) ValidateAgainst(policy InferencePolicy) error {
	miner := InferenceMinerResponse{
		ID: response.ID, Model: response.Model, Choices: response.Choices,
		Usage: InferenceMinerUsage{
			PromptTokens: response.Usage.PromptTokens, CompletionTokens: response.Usage.CompletionTokens,
			TotalTokens: response.Usage.TotalTokens,
		},
	}
	if err := miner.ValidateAgainst(policy); err != nil {
		return err
	}
	if response.Provider != policy.ReceiptProvider || response.Usage.Cost == "" {
		return errors.New("coding inference provider response identity is invalid")
	}
	costMicros, ok := inferenceCostMicros(response.Usage.Cost)
	if !ok || costMicros > policy.MaxCostUSDMicros {
		return errors.New("coding inference provider cost is invalid")
	}
	return nil
}

func (response InferenceNormalizedResponse) ValidateAgainst(policy InferencePolicy) error {
	miner := InferenceMinerResponse{
		ID: response.ID, Model: response.Model, Choices: response.Choices,
		Usage: InferenceMinerUsage{
			PromptTokens: response.Usage.PromptTokens, CompletionTokens: response.Usage.CompletionTokens,
			TotalTokens: response.Usage.TotalTokens,
		},
	}
	if response.Schema != InferenceResponseSchema || response.Provider != policy.ReceiptProvider ||
		response.Usage.CostUSDMicros > policy.MaxCostUSDMicros {
		return errors.New("coding inference normalized response is invalid")
	}
	return miner.ValidateAgainst(policy)
}

func NormalizeInferenceResponse(
	policy InferencePolicy,
	response InferenceProviderResponse,
) (InferenceNormalizedResponse, error) {
	if err := response.ValidateAgainst(policy); err != nil {
		return InferenceNormalizedResponse{}, err
	}
	costUSDMicros, ok := inferenceCostMicros(response.Usage.Cost)
	if !ok || costUSDMicros > policy.MaxCostUSDMicros {
		return InferenceNormalizedResponse{}, errors.New("coding inference normalized cost exceeds policy")
	}
	return InferenceNormalizedResponse{
		Schema: InferenceResponseSchema, ID: response.ID, Model: response.Model, Provider: response.Provider,
		Choices: cloneInferenceChoices(response.Choices),
		Usage: InferenceNormalizedUsage{
			PromptTokens: response.Usage.PromptTokens, CompletionTokens: response.Usage.CompletionTokens,
			TotalTokens: response.Usage.TotalTokens, CostUSDMicros: costUSDMicros,
		},
	}, nil
}

func (choice InferenceResponseChoice) Validate() error {
	message := choice.Message
	if message.Content == nil && len(message.ToolCalls) == 0 {
		return errors.New("coding inference response has neither content nor a tool call")
	}
	if message.Content != nil && (!utf8.ValidString(*message.Content) || len(*message.Content) > MaxInferenceResponseBytes) {
		return errors.New("coding inference response content is invalid")
	}
	if len(message.ToolCalls) > MaxInferenceToolCallsPerResponse {
		return errors.New("coding inference response tool calls are invalid")
	}
	seen := make(map[string]struct{}, len(message.ToolCalls))
	for _, call := range message.ToolCalls {
		if !validIdentifier(call.ID, 256) || call.Type != "function" ||
			!validIdentifier(call.Function.Name, 128) || call.Function.Arguments == "" ||
			len(call.Function.Arguments) > MaxInferenceToolArgumentBytes ||
			!utf8.ValidString(call.Function.Arguments) {
			return errors.New("coding inference response tool call is invalid")
		}
		if _, duplicate := seen[call.ID]; duplicate {
			return errors.New("coding inference response tool call IDs are not unique")
		}
		seen[call.ID] = struct{}{}
		if err := ValidateJSONDocument([]byte(call.Function.Arguments), MaxInferenceToolArgumentBytes); err != nil {
			return errors.New("coding inference response tool arguments are invalid")
		}
		if err := validateInferenceNumberLexemes([]byte(call.Function.Arguments)); err != nil {
			return errors.New("coding inference response tool argument numbers are invalid")
		}
		var arguments map[string]any
		decoder := json.NewDecoder(strings.NewReader(call.Function.Arguments))
		decoder.UseNumber()
		if err := decoder.Decode(&arguments); err != nil || arguments == nil {
			return errors.New("coding inference response tool arguments must be an object")
		}
	}
	return nil
}

func InferenceReceiptSetSHA256(policy InferencePolicy, set InferenceReceiptSet) (string, error) {
	set = set.Clone()
	if _, err := validateInferenceReceiptSet(policy, set); err != nil {
		return "", err
	}
	return inferenceDigest(set, MaxInferenceReceiptSetBytes)
}

func InferenceProviderSettlementSHA256(
	policy InferencePolicy,
	settlement InferenceProviderSettlement,
) (string, error) {
	if err := settlement.Validate(policy); err != nil {
		return "", err
	}
	return inferenceDigest(settlement, MaxInferenceReceiptSetBytes)
}

func (settlement InferenceProviderSettlement) Validate(policy InferencePolicy) error {
	if err := policy.Validate(); err != nil {
		return err
	}
	grantDigest, err := InferencePolicySHA256(policy)
	if err != nil {
		return err
	}
	if settlement.Schema != InferenceProviderSettlementSchema ||
		settlement.CodingContractVersion != ContractVersion || !canonicalUUID(settlement.TicketID) ||
		!validIdentifier(settlement.CaseID, 256) || !validIdentifier(settlement.ProfileCapabilityID, 256) ||
		settlement.InferenceGrantSHA256 != grantDigest || !canonicalUUID(settlement.GrantID) ||
		settlement.Generation == 0 || settlement.Generation > 1<<31-1 || !canonicalUUID(settlement.RequestID) ||
		settlement.RequestSequence == 0 || settlement.RequestSequence > policy.MaxRequests ||
		settlement.Attempt == 0 || settlement.Attempt > policy.MaxAttemptsPerRequest ||
		!validSHA256(settlement.LockedRequestSHA256) || settlement.Model != policy.Model ||
		settlement.ProviderAPI != policy.ProviderAPI || settlement.ProviderRoute != policy.ProviderRoute ||
		settlement.ProviderRouteProfile != policy.ProviderRouteProfile ||
		settlement.ProviderAccountGuardrail != policy.ProviderAccountGuardrail ||
		settlement.ProviderPipelinePolicy != policy.ProviderPipelinePolicy ||
		settlement.ProviderCachePolicy != policy.ProviderCachePolicy ||
		settlement.RouterMetadataVerified != policy.RouterMetadataRequired || settlement.FallbackUsed ||
		settlement.PipelineStages == nil || len(settlement.PipelineStages) != 0 ||
		len(settlement.RouterAttempts) != 1 || settlement.RouterAttempts[0].Provider != policy.ReceiptProvider ||
		(!settlement.UsageAvailable && (settlement.PromptTokens != 0 ||
			settlement.CompletionTokens != 0 || settlement.TotalTokens != 0)) ||
		(!settlement.CostAvailable && settlement.CostUSDMicros != 0) ||
		settlement.PromptTokens > ^uint64(0)-settlement.CompletionTokens ||
		settlement.TotalTokens != settlement.PromptTokens+settlement.CompletionTokens ||
		settlement.CompletionTokens > policy.MaxCompletionTokensPerRequest ||
		settlement.PromptTokens > policy.MaxPromptTokens || settlement.TotalTokens > policy.MaxTotalTokens ||
		settlement.CostUSDMicros > policy.MaxCostUSDMicros || settlement.HTTPStatus < 0 || settlement.HTTPStatus > 599 ||
		(settlement.ResponseSHA256 != nil && !validSHA256(*settlement.ResponseSHA256)) ||
		(settlement.ProviderGenerationID != nil && !validIdentifier(*settlement.ProviderGenerationID, 256)) ||
		!validInferenceResponseDigestKind(settlement.ResponseSHA256, settlement.ResponseDigestKind, settlement.Outcome) {
		return errors.New("coding inference provider settlement is invalid")
	}
	selected := settlement.RouterAttempts[0].Selected
	switch settlement.Outcome {
	case InferenceReceiptFreeRetry:
		if settlement.TerminalErrorCode == nil || *settlement.TerminalErrorCode != "pre_provider_unavailable" ||
			!retryablePreProviderStatus(settlement.HTTPStatus) || settlement.ResponseSHA256 != nil ||
			settlement.ProviderGenerationID != nil || settlement.ReceiptProvider != nil || selected ||
			settlement.UsageAvailable || settlement.CostAvailable || settlement.TimedOut {
			return errors.New("coding inference retry settlement is invalid")
		}
	case InferenceReceiptComplete:
		if settlement.TerminalErrorCode != nil || settlement.HTTPStatus < 200 || settlement.HTTPStatus >= 300 ||
			settlement.ResponseSHA256 == nil || !validSHA256(*settlement.ResponseSHA256) ||
			settlement.ProviderGenerationID == nil || !validIdentifier(*settlement.ProviderGenerationID, 256) ||
			settlement.ReceiptProvider == nil || *settlement.ReceiptProvider != policy.ReceiptProvider ||
			!selected || !settlement.UsageAvailable || !settlement.CostAvailable || settlement.TimedOut {
			return errors.New("coding inference completed settlement is invalid")
		}
	case InferenceReceiptProviderFailed:
		failureReceipt := InferenceReceipt{
			FailureCode: settlement.TerminalErrorCode, HTTPStatus: settlement.HTTPStatus,
			ResponseSHA256: settlement.ResponseSHA256, ProviderSelected: selected,
			ReceiptProvider: settlement.ReceiptProvider, ProviderGenerationID: settlement.ProviderGenerationID,
			PromptTokens: settlement.PromptTokens, CompletionTokens: settlement.CompletionTokens,
			CostUSDMicros: settlement.CostUSDMicros, TimedOut: settlement.TimedOut,
		}
		if settlement.TerminalErrorCode == nil || !slices.Contains(inferenceFailureCodes, *settlement.TerminalErrorCode) ||
			!validProviderFailureShape(failureReceipt) ||
			(selected && (settlement.ReceiptProvider == nil || *settlement.ReceiptProvider != policy.ReceiptProvider ||
				!settlement.UsageAvailable || !settlement.CostAvailable)) ||
			(!selected && (settlement.ReceiptProvider != nil || settlement.ProviderGenerationID != nil ||
				settlement.UsageAvailable || settlement.CostAvailable)) {
			return errors.New("coding inference failed settlement is invalid")
		}
	default:
		return errors.New("coding inference settlement outcome is invalid")
	}
	return nil
}

func (settlement InferenceProviderSettlement) ValidateAgainstReceipt(
	policy InferencePolicy,
	receipt InferenceReceipt,
) error {
	if err := settlement.Validate(policy); err != nil {
		return err
	}
	if settlement.RequestID != receipt.RequestID || settlement.RequestSequence != receipt.RequestSequence ||
		settlement.Attempt != receipt.Attempt || settlement.LockedRequestSHA256 != receipt.LockedRequestSHA256 ||
		settlement.Outcome != receipt.Outcome || !equalOptionalString(settlement.TerminalErrorCode, receipt.FailureCode) ||
		settlement.HTTPStatus != receipt.HTTPStatus ||
		!equalOptionalString(settlement.ResponseSHA256, receipt.ResponseSHA256) ||
		settlement.ResponseDigestKind != receipt.ResponseDigestKind ||
		!equalOptionalString(settlement.ProviderGenerationID, receipt.ProviderGenerationID) ||
		settlement.RouterAttempts[0].Selected != receipt.ProviderSelected ||
		!equalOptionalString(settlement.ReceiptProvider, receipt.ReceiptProvider) ||
		settlement.FallbackUsed != receipt.FallbackUsed || settlement.PromptTokens != receipt.PromptTokens ||
		settlement.CompletionTokens != receipt.CompletionTokens || settlement.TotalTokens != receipt.TotalTokens ||
		settlement.CostUSDMicros != receipt.CostUSDMicros || settlement.TimedOut != receipt.TimedOut {
		return errors.New("coding inference receipt disagrees with provider settlement")
	}
	digest, err := InferenceProviderSettlementSHA256(policy, settlement)
	if err != nil || digest != receipt.ProviderSettlementSHA256 {
		return errors.New("coding inference provider settlement digest disagrees")
	}
	return nil
}

func equalOptionalString(left, right *string) bool {
	if left == nil || right == nil {
		return left == nil && right == nil
	}
	return *left == *right
}

func DeriveInferenceModelEvidence(
	policy InferencePolicy,
	binding InferenceReceiptBinding,
	set InferenceReceiptSet,
	settlements []InferenceProviderSettlement,
) (ModelEvidence, error) {
	set = set.Clone()
	settlements = cloneInferenceSettlements(settlements)
	if err := validateInferenceReceiptBinding(policy, binding, set); err != nil {
		return ModelEvidence{}, err
	}
	aggregate, err := validateInferenceReceiptSet(policy, set)
	if err != nil {
		return ModelEvidence{}, err
	}
	if len(settlements) != len(set.Receipts) {
		return ModelEvidence{}, errors.New("coding inference settlement coverage is incomplete")
	}
	for index, settlement := range settlements {
		if settlement.TicketID != binding.TicketID || settlement.CaseID != binding.CaseID ||
			settlement.ProfileCapabilityID != binding.ProfileCapabilityID ||
			settlement.InferenceGrantSHA256 != binding.InferenceGrantSHA256 ||
			settlement.GrantID != binding.GrantID || settlement.Generation != binding.Generation {
			return ModelEvidence{}, errors.New("coding inference settlement binding disagrees")
		}
		if err := settlement.ValidateAgainstReceipt(policy, set.Receipts[index]); err != nil {
			return ModelEvidence{}, err
		}
	}
	root, err := inferenceDigest(set, MaxInferenceReceiptSetBytes)
	if err != nil {
		return ModelEvidence{}, err
	}
	evidence := baseInferenceModelEvidence(policy)
	evidence.Requests = aggregate.requests
	evidence.PromptTokens = aggregate.promptTokens
	evidence.CompletionTokens = aggregate.completionTokens
	evidence.TotalTokens = aggregate.promptTokens + aggregate.completionTokens
	evidence.CostUSDMicros = aggregate.costUSDMicros
	evidence.RetryCount = aggregate.retries
	evidence.ProviderReceiptSetSHA256 = &root
	if aggregate.providerFailure {
		evidence.UsageStatus = ModelUsageProviderFailure
	} else {
		evidence.UsageStatus = ModelUsageComplete
	}
	if err := evidence.Validate(); err != nil {
		return ModelEvidence{}, err
	}
	return evidence, nil
}

func validateInferenceReceiptBinding(
	policy InferencePolicy,
	binding InferenceReceiptBinding,
	set InferenceReceiptSet,
) error {
	if err := validateInferenceReceiptAuthority(policy, binding); err != nil {
		return err
	}
	if binding.TicketID != set.TicketID || binding.CaseID != set.CaseID ||
		binding.ProfileCapabilityID != set.ProfileCapabilityID || binding.GrantID != set.GrantID ||
		binding.Generation != set.Generation || binding.InferenceGrantSHA256 != set.InferenceGrantSHA256 {
		return errors.New("coding inference receipt set disagrees with trusted binding")
	}
	if binding.RequestBudget != set.RequestBudget || binding.PromptTokenBudget != set.PromptTokenBudget ||
		binding.CompletionTokenBudget != set.CompletionTokenBudget {
		return errors.New("coding inference receipt set disagrees with trusted binding")
	}
	return nil
}

func validateInferenceReceiptAuthority(
	policy InferencePolicy,
	binding InferenceReceiptBinding,
) error {
	grantDigest, err := InferencePolicySHA256(policy)
	if err != nil {
		return err
	}
	if !canonicalUUID(binding.TicketID) || !validIdentifier(binding.CaseID, 256) ||
		!validIdentifier(binding.ProfileCapabilityID, 256) || !canonicalUUID(binding.GrantID) ||
		binding.Generation == 0 || binding.Generation > 1<<31-1 ||
		binding.InferenceGrantSHA256 != grantDigest || binding.RequestBudget == 0 ||
		binding.RequestBudget > policy.MaxRequests || binding.PromptTokenBudget == 0 ||
		binding.PromptTokenBudget > policy.MaxPromptTokens || binding.CompletionTokenBudget == 0 ||
		binding.CompletionTokenBudget > policy.MaxCompletionTokens {
		return errors.New("coding inference receipt authority is invalid")
	}
	return nil
}

func NotInvokedInferenceModelEvidence(
	policy InferencePolicy,
	binding InferenceReceiptBinding,
) (ModelEvidence, error) {
	if err := validateInferenceReceiptAuthority(policy, binding); err != nil {
		return ModelEvidence{}, err
	}
	evidence := baseInferenceModelEvidence(policy)
	evidence.UsageStatus = ModelUsageNotInvoked
	if err := evidence.Validate(); err != nil {
		return ModelEvidence{}, err
	}
	return evidence, nil
}

func baseInferenceModelEvidence(policy InferencePolicy) ModelEvidence {
	grantDigest, _ := InferencePolicySHA256(policy)
	return ModelEvidence{
		Model: policy.Model, Provider: policy.ProviderRoute, ProviderRouteProfile: policy.ProviderRouteProfile,
		ReasoningEffort: policy.ReasoningEffort, InferenceGrantSHA256: grantDigest,
		PromptSHA256: policy.PromptSHA256, ToolSchemaSHA256: policy.ToolSchemaSHA256,
		FallbackUsed: false, CostSource: policy.CostSource, Currency: policy.Currency,
	}
}

type inferenceReceiptAggregate struct {
	requests         uint64
	retries          uint32
	promptTokens     uint64
	completionTokens uint64
	costUSDMicros    uint64
	providerFailure  bool
}

func validateInferenceReceiptSet(policy InferencePolicy, set InferenceReceiptSet) (inferenceReceiptAggregate, error) {
	var aggregate inferenceReceiptAggregate
	if err := policy.Validate(); err != nil {
		return aggregate, err
	}
	grantDigest, err := InferencePolicySHA256(policy)
	if err != nil {
		return aggregate, err
	}
	if set.Schema != InferenceReceiptSetSchema || set.CodingContractVersion != ContractVersion ||
		!canonicalUUID(set.TicketID) || !validIdentifier(set.CaseID, 256) ||
		!validIdentifier(set.ProfileCapabilityID, 256) || !canonicalUUID(set.GrantID) ||
		set.Generation == 0 || set.Generation > 1<<31-1 || set.InferenceGrantSHA256 != grantDigest ||
		set.RequestBudget == 0 || set.RequestBudget > policy.MaxRequests ||
		set.PromptTokenBudget == 0 || set.PromptTokenBudget > policy.MaxPromptTokens ||
		set.CompletionTokenBudget == 0 || set.CompletionTokenBudget > policy.MaxCompletionTokens ||
		set.Receipts == nil || len(set.Receipts) == 0 ||
		len(set.Receipts) > int(policy.MaxRequests)+int(policy.MaxRetries) {
		return aggregate, errors.New("coding inference receipt-set identity is invalid")
	}
	seenRequests := make(map[string]struct{})
	seenSettlements := make(map[string]struct{})
	seenGenerations := make(map[string]struct{})
	var current *InferenceReceipt
	terminal := true
	for index := range set.Receipts {
		receipt := set.Receipts[index]
		if receipt.Schema != InferenceReceiptSchema || receipt.Sequence != uint32(index+1) {
			return aggregate, errors.New("coding inference receipt sequence is invalid")
		}
		newRequest := current == nil || receipt.RequestSequence != current.RequestSequence
		if newRequest {
			if !terminal || (current != nil && current.Outcome != InferenceReceiptComplete) ||
				receipt.RequestSequence != uint32(aggregate.requests+1) || receipt.Attempt != 1 {
				return aggregate, errors.New("coding inference request sequence is invalid")
			}
			if _, duplicate := seenRequests[receipt.RequestID]; duplicate {
				return aggregate, errors.New("coding inference request ID was reused")
			}
			seenRequests[receipt.RequestID] = struct{}{}
			aggregate.requests++
			terminal = false
		} else if terminal || receipt.Attempt != current.Attempt+1 ||
			receipt.RequestID != current.RequestID || receipt.LockedRequestSHA256 != current.LockedRequestSHA256 ||
			receipt.PromptSHA256 != current.PromptSHA256 || receipt.ToolSchemaSHA256 != current.ToolSchemaSHA256 {
			return aggregate, errors.New("coding inference retry identity is invalid")
		}
		if receipt.Attempt > policy.MaxAttemptsPerRequest || receipt.PromptSHA256 != policy.PromptSHA256 ||
			receipt.ToolSchemaSHA256 != policy.ToolSchemaSHA256 || receipt.Model != policy.Model ||
			receipt.ProviderRoute != policy.ProviderRoute || receipt.ProviderRouteProfile != policy.ProviderRouteProfile ||
			receipt.FallbackUsed || !canonicalUUID(receipt.RequestID) || !validSHA256(receipt.LockedRequestSHA256) ||
			receipt.PromptTokens > ^uint64(0)-receipt.CompletionTokens ||
			receipt.TotalTokens != receipt.PromptTokens+receipt.CompletionTokens ||
			receipt.CompletionTokens > policy.MaxCompletionTokensPerRequest ||
			receipt.HTTPStatus < 0 || receipt.HTTPStatus > 599 ||
			!validInferenceResponseDigestKind(receipt.ResponseSHA256, receipt.ResponseDigestKind, receipt.Outcome) ||
			!validSHA256(receipt.ProviderSettlementSHA256) {
			return aggregate, errors.New("coding inference receipt identity is invalid")
		}
		if _, duplicate := seenSettlements[receipt.ProviderSettlementSHA256]; duplicate {
			return aggregate, errors.New("coding inference provider settlement was reused")
		}
		seenSettlements[receipt.ProviderSettlementSHA256] = struct{}{}
		if receipt.ProviderGenerationID != nil {
			if !validIdentifier(*receipt.ProviderGenerationID, 256) {
				return aggregate, errors.New("coding inference provider generation is invalid")
			}
			if _, duplicate := seenGenerations[*receipt.ProviderGenerationID]; duplicate {
				return aggregate, errors.New("coding inference provider generation was reused")
			}
			seenGenerations[*receipt.ProviderGenerationID] = struct{}{}
		}
		switch receipt.Outcome {
		case InferenceReceiptFreeRetry:
			if receipt.FailureCode == nil || *receipt.FailureCode != "pre_provider_unavailable" ||
				!retryablePreProviderStatus(receipt.HTTPStatus) || receipt.ResponseSHA256 != nil || receipt.ProviderSelected ||
				receipt.ReceiptProvider != nil || receipt.PromptTokens != 0 || receipt.CompletionTokens != 0 ||
				receipt.CostUSDMicros != 0 || receipt.TimedOut || receipt.ProviderGenerationID != nil {
				return aggregate, errors.New("coding inference receipt-free retry is invalid")
			}
			if aggregate.retries == ^uint32(0) {
				return aggregate, errors.New("coding inference retry count overflowed")
			}
			aggregate.retries++
			terminal = false
		case InferenceReceiptComplete:
			if receipt.FailureCode != nil || receipt.HTTPStatus < 200 || receipt.HTTPStatus >= 300 ||
				receipt.ResponseSHA256 == nil || !validSHA256(*receipt.ResponseSHA256) || !receipt.ProviderSelected ||
				receipt.ReceiptProvider == nil || *receipt.ReceiptProvider != policy.ReceiptProvider ||
				receipt.ProviderGenerationID == nil || receipt.TimedOut {
				return aggregate, errors.New("coding inference completed receipt is invalid")
			}
			terminal = true
		case InferenceReceiptProviderFailed:
			if receipt.FailureCode == nil || !slices.Contains(inferenceFailureCodes, *receipt.FailureCode) ||
				!validProviderFailureShape(receipt) ||
				(receipt.ResponseSHA256 != nil && !validSHA256(*receipt.ResponseSHA256)) ||
				(receipt.ProviderSelected && (receipt.ReceiptProvider == nil || *receipt.ReceiptProvider != policy.ReceiptProvider)) ||
				(!receipt.ProviderSelected && (receipt.ReceiptProvider != nil || receipt.ProviderGenerationID != nil ||
					receipt.PromptTokens != 0 ||
					receipt.CompletionTokens != 0 || receipt.CostUSDMicros != 0)) {
				return aggregate, errors.New("coding inference provider-failure receipt is invalid")
			}
			aggregate.providerFailure = true
			terminal = true
		default:
			return aggregate, errors.New("coding inference receipt outcome is invalid")
		}
		if aggregate.retries > policy.MaxRetries || aggregate.requests > uint64(set.RequestBudget) ||
			!addInferenceTotal(&aggregate.promptTokens, receipt.PromptTokens) ||
			!addInferenceTotal(&aggregate.completionTokens, receipt.CompletionTokens) ||
			!addInferenceTotal(&aggregate.costUSDMicros, receipt.CostUSDMicros) ||
			aggregate.promptTokens > set.PromptTokenBudget || aggregate.completionTokens > set.CompletionTokenBudget ||
			aggregate.promptTokens > ^uint64(0)-aggregate.completionTokens ||
			aggregate.promptTokens+aggregate.completionTokens > policy.MaxTotalTokens ||
			aggregate.costUSDMicros > policy.MaxCostUSDMicros {
			return aggregate, errors.New("coding inference receipt-set budget is invalid")
		}
		current = &set.Receipts[index]
	}
	if !terminal || aggregate.requests == 0 {
		return aggregate, errors.New("coding inference receipt set has an unterminated request")
	}
	return aggregate, nil
}

func addInferenceTotal(total *uint64, value uint64) bool {
	if total == nil || *total > ^uint64(0)-value {
		return false
	}
	*total += value
	return true
}

func retryablePreProviderStatus(status int) bool {
	return slices.Contains([]int{404, 408, 429, 500, 502, 503, 504}, status)
}

func validProviderFailureShape(receipt InferenceReceipt) bool {
	if receipt.FailureCode == nil {
		return false
	}
	switch *receipt.FailureCode {
	case "pre_provider_unavailable":
		return !receipt.ProviderSelected && retryablePreProviderStatus(receipt.HTTPStatus) &&
			receipt.ResponseSHA256 == nil && !receipt.TimedOut
	case "provider_timeout":
		return receipt.TimedOut && slices.Contains([]int{0, 408, 504}, receipt.HTTPStatus)
	case "provider_transport":
		return !receipt.TimedOut && receipt.HTTPStatus == 0 && receipt.ResponseSHA256 == nil
	case "provider_http":
		return !receipt.TimedOut && receipt.HTTPStatus >= 400
	case "provider_response_invalid":
		return !receipt.TimedOut && receipt.HTTPStatus >= 200 && receipt.HTTPStatus < 300 &&
			receipt.ResponseSHA256 != nil
	default:
		return false
	}
}

func validInferenceResponseDigestKind(
	responseSHA256 *string,
	kind string,
	outcome InferenceReceiptOutcome,
) bool {
	if responseSHA256 == nil {
		return kind == "none"
	}
	if !validSHA256(*responseSHA256) {
		return false
	}
	if outcome == InferenceReceiptComplete {
		return kind == "normalized_v1"
	}
	return kind == "canonical_json_v1"
}

func inferenceCostMicros(value json.Number) (uint64, bool) {
	raw := value.String()
	if len(raw) == 0 || len(raw) > 64 || !inferenceCostNumber.MatchString(raw) {
		return 0, false
	}
	if exponentAt := strings.IndexAny(raw, "eE"); exponentAt >= 0 {
		exponent, err := strconv.Atoi(raw[exponentAt+1:])
		if err != nil || exponent < -100 || exponent > 100 {
			return 0, false
		}
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
	if !quotient.IsUint64() {
		return 0, false
	}
	return quotient.Uint64(), true
}

func (set InferenceReceiptSet) Clone() InferenceReceiptSet {
	receipts := set.Receipts
	set.Receipts = make([]InferenceReceipt, len(receipts))
	for index, receipt := range receipts {
		set.Receipts[index] = cloneInferenceReceipt(receipt)
	}
	return set
}

func cloneInferenceSettlements(
	values []InferenceProviderSettlement,
) []InferenceProviderSettlement {
	if values == nil {
		return nil
	}
	result := make([]InferenceProviderSettlement, len(values))
	for index, value := range values {
		result[index] = value.Clone()
	}
	return result
}

func (settlement InferenceProviderSettlement) Clone() InferenceProviderSettlement {
	settlement.TerminalErrorCode = cloneOptionalString(settlement.TerminalErrorCode)
	settlement.ResponseSHA256 = cloneOptionalString(settlement.ResponseSHA256)
	settlement.ProviderGenerationID = cloneOptionalString(settlement.ProviderGenerationID)
	settlement.ReceiptProvider = cloneOptionalString(settlement.ReceiptProvider)
	if settlement.RouterAttempts != nil {
		settlement.RouterAttempts = append([]InferenceRouterAttempt{}, settlement.RouterAttempts...)
	}
	if settlement.PipelineStages != nil {
		settlement.PipelineStages = append([]string{}, settlement.PipelineStages...)
	}
	return settlement
}

func cloneOptionalString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

func cloneInferenceReceipt(receipt InferenceReceipt) InferenceReceipt {
	receipt.FailureCode = cloneOptionalString(receipt.FailureCode)
	receipt.ResponseSHA256 = cloneOptionalString(receipt.ResponseSHA256)
	receipt.ProviderGenerationID = cloneOptionalString(receipt.ProviderGenerationID)
	receipt.ReceiptProvider = cloneOptionalString(receipt.ReceiptProvider)
	return receipt
}

func cloneInferenceTools(tools []InferenceTool) []InferenceTool {
	if tools == nil {
		return nil
	}
	result := make([]InferenceTool, len(tools))
	for index, tool := range tools {
		result[index] = tool
		result[index].Function.Parameters = append(json.RawMessage(nil), tool.Function.Parameters...)
	}
	return result
}

func cloneRawMessages(messages []json.RawMessage) []json.RawMessage {
	if messages == nil {
		return nil
	}
	result := make([]json.RawMessage, len(messages))
	for index, message := range messages {
		result[index] = append(json.RawMessage(nil), message...)
	}
	return result
}

func cloneInferenceChoices(choices []InferenceResponseChoice) []InferenceResponseChoice {
	if choices == nil {
		return nil
	}
	result := make([]InferenceResponseChoice, len(choices))
	for index, choice := range choices {
		result[index] = choice
		if choice.Message.Content != nil {
			value := *choice.Message.Content
			result[index].Message.Content = &value
		}
		result[index].Message.ToolCalls = make([]InferenceToolCall, len(choice.Message.ToolCalls))
		copy(result[index].Message.ToolCalls, choice.Message.ToolCalls)
	}
	return result
}

func normalizeInferenceChoices(choices []InferenceResponseChoice) []InferenceResponseChoice {
	return cloneInferenceChoices(choices)
}

func validateInferenceMessages(messages []json.RawMessage) error {
	for index, message := range messages {
		if len(message) == 0 || len(message) > MaxInferenceRequestBytes ||
			ValidateJSONDocument(message, MaxInferenceRequestBytes) != nil {
			return fmt.Errorf("coding inference message %d is invalid", index)
		}
		var object map[string]any
		decoder := json.NewDecoder(bytes.NewReader(message))
		decoder.UseNumber()
		if err := decoder.Decode(&object); err != nil || object == nil {
			return fmt.Errorf("coding inference message %d is not an object", index)
		}
		if err := validateInferenceNumberLexemes(message); err != nil {
			return fmt.Errorf("coding inference message %d numbers are invalid", index)
		}
		if err := validateInferenceMessageShapes(object, fmt.Sprintf("$.messages[%d]", index)); err != nil {
			return err
		}
	}
	return nil
}

func systemPromptFromMessages(messages []json.RawMessage) (InferenceSystemPrompt, error) {
	if len(messages) == 0 {
		return InferenceSystemPrompt{}, errors.New("coding inference request has no system prompt")
	}
	var message struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	if err := json.Unmarshal(messages[0], &message); err != nil || message.Role != "system" || message.Content == "" {
		return InferenceSystemPrompt{}, errors.New("coding inference first message is not the fixed system prompt")
	}
	return InferenceSystemPrompt{Schema: InferenceSystemPromptSchema, Content: message.Content}, nil
}

func canonicalUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil && parsed.String() == value
}

func inferenceRequestMaximum(policy InferencePolicy) (int, error) {
	if err := policy.Validate(); err != nil {
		return 0, err
	}
	return int(policy.MaxRequestBytes), nil
}

func inferenceResponseMaximum(policy InferencePolicy) (int, error) {
	if err := policy.Validate(); err != nil {
		return 0, err
	}
	return int(policy.MaxResponseBytes), nil
}

func validateInferencePolicyShape(object map[string]any) error {
	return requireFields(object, "$",
		"schema", "coding_contract_version", "bench_family", "weight_eligible", "api", "model",
		"provider_api", "provider_route", "receipt_provider", "provider_receipt_source", "provider_account_guardrail",
		"provider_pipeline_policy", "provider_cache_policy", "router_metadata_required",
		"provider_route_profile", "prompt_sha256",
		"tool_schema_sha256", "reasoning_effort", "reasoning_excluded", "stream", "store", "n",
		"parallel_tool_calls", "max_tool_calls_per_response", "usage_included", "allow_fallbacks", "require_parameters",
		"data_collection", "zdr", "max_requests", "max_prompt_tokens", "max_completion_tokens",
		"max_total_tokens", "max_completion_tokens_per_request", "max_cost_usd_micros", "max_request_bytes",
		"max_response_bytes", "request_timeout_milliseconds", "retry_policy", "max_attempts_per_request",
		"max_retries", "cost_source", "currency")
}

func validateInferenceToolShape(object map[string]any, path string) error {
	if err := requireExactInferenceFields(object, path, "type", "function"); err != nil {
		return err
	}
	function, err := objectField(object, path, "function")
	if err != nil {
		return err
	}
	if err := requireExactInferenceFields(function, path+".function", "name", "description", "parameters"); err != nil {
		return err
	}
	if _, ok := function["parameters"].(map[string]any); !ok {
		return fmt.Errorf("%s.function.parameters must be an object", path)
	}
	return nil
}

func validateInferenceToolSchemaShape(object map[string]any) error {
	if err := requireFields(object, "$", "schema", "tools"); err != nil {
		return err
	}
	return arrayObjects(object, "$", "tools", validateInferenceToolShape)
}

func validateInferenceMessageShapes(object map[string]any, path string) error {
	role, ok := object["role"].(string)
	if !ok {
		return fmt.Errorf("%s.role must be a string", path)
	}
	switch role {
	case "system", "user":
		if err := requireExactInferenceFields(object, path, "role", "content"); err != nil {
			return err
		}
		if _, ok := object["content"].(string); !ok {
			return fmt.Errorf("%s.content must be a string", path)
		}
	case "assistant":
		if err := requireExactInferenceFields(object, path, "role", "content", "tool_calls"); err != nil {
			return err
		}
		if content := object["content"]; content != nil {
			if _, ok := content.(string); !ok {
				return fmt.Errorf("%s.content must be a string or null", path)
			}
		}
		calls, err := arrayField(object, path, "tool_calls")
		if err != nil || len(calls) > MaxInferenceToolCallsPerResponse {
			return fmt.Errorf("%s.tool_calls is invalid", path)
		}
		seen := make(map[string]struct{}, len(calls))
		for index, raw := range calls {
			call, ok := raw.(map[string]any)
			if !ok {
				return fmt.Errorf("%s.tool_calls[%d] must be an object", path, index)
			}
			if err := validateInferenceToolCallShape(call, fmt.Sprintf("%s.tool_calls[%d]", path, index)); err != nil {
				return err
			}
			id := call["id"].(string)
			if _, duplicate := seen[id]; duplicate {
				return fmt.Errorf("%s.tool_calls contains duplicate IDs", path)
			}
			seen[id] = struct{}{}
		}
	case "tool":
		if err := requireExactInferenceFields(object, path, "role", "tool_call_id", "content"); err != nil {
			return err
		}
		if id, ok := object["tool_call_id"].(string); !ok || !validIdentifier(id, 256) {
			return fmt.Errorf("%s.tool_call_id must be a string", path)
		}
		if _, ok := object["content"].(string); !ok {
			return fmt.Errorf("%s.content must be a string", path)
		}
	default:
		return fmt.Errorf("%s.role is invalid", path)
	}
	return nil
}

func validateInferenceRequestCoreShape(object map[string]any) error {
	if err := requireFields(object, "$", "model", "messages", "tools", "tool_choice", "reasoning",
		"max_completion_tokens", "parallel_tool_calls"); err != nil {
		return err
	}
	if err := arrayObjects(object, "$", "messages", validateInferenceMessageShapes); err != nil {
		return err
	}
	if err := arrayObjects(object, "$", "tools", validateInferenceToolShape); err != nil {
		return err
	}
	reasoning, err := objectField(object, "$", "reasoning")
	if err != nil {
		return err
	}
	return requireFields(reasoning, "$.reasoning", "effort")
}

func validateInferenceMinerRequestShape(object map[string]any) error {
	if err := requireExactInferenceFields(object, "$", "model", "messages", "tools", "tool_choice", "reasoning",
		"max_completion_tokens", "parallel_tool_calls"); err != nil {
		return err
	}
	return validateInferenceRequestCoreShape(object)
}

func validateInferenceLockedRequestShape(object map[string]any) error {
	if err := validateInferenceRequestCoreShape(object); err != nil {
		return err
	}
	if err := requireExactInferenceFields(object, "$", "model", "messages", "tools", "tool_choice", "reasoning",
		"max_completion_tokens", "parallel_tool_calls", "n", "stream", "store", "usage", "provider"); err != nil {
		return err
	}
	if err := requireFields(object, "$", "n", "stream", "store", "usage", "provider"); err != nil {
		return err
	}
	reasoning, _ := objectField(object, "$", "reasoning")
	if err := requireExactInferenceFields(reasoning, "$.reasoning", "effort", "exclude"); err != nil {
		return err
	}
	usage, err := objectField(object, "$", "usage")
	if err != nil {
		return err
	}
	if err := requireExactInferenceFields(usage, "$.usage", "include"); err != nil {
		return err
	}
	provider, err := objectField(object, "$", "provider")
	if err != nil {
		return err
	}
	return requireExactInferenceFields(provider, "$.provider", "only", "order", "allow_fallbacks", "require_parameters", "data_collection", "zdr")
}

func validateInferenceToolCallShape(call map[string]any, path string) error {
	if err := requireExactInferenceFields(call, path, "id", "type", "function"); err != nil {
		return err
	}
	id, ok := call["id"].(string)
	if !ok || !validIdentifier(id, 256) || call["type"] != "function" {
		return fmt.Errorf("%s identity is invalid", path)
	}
	function, err := objectField(call, path, "function")
	if err != nil {
		return err
	}
	if err := requireExactInferenceFields(function, path+".function", "name", "arguments"); err != nil {
		return err
	}
	if name, ok := function["name"].(string); !ok || !validIdentifier(name, 128) {
		return fmt.Errorf("%s.function.name must be a string", path)
	}
	arguments, ok := function["arguments"].(string)
	if !ok || len(arguments) > MaxInferenceToolArgumentBytes ||
		ValidateJSONDocument([]byte(arguments), MaxInferenceToolArgumentBytes) != nil ||
		validateInferenceNumberLexemes([]byte(arguments)) != nil {
		return fmt.Errorf("%s.function.arguments is invalid", path)
	}
	var decoded map[string]any
	if err := json.Unmarshal([]byte(arguments), &decoded); err != nil || decoded == nil {
		return fmt.Errorf("%s.function.arguments must be an object", path)
	}
	return nil
}

func validateInferenceChoiceShape(object map[string]any, path string, requireToolCalls bool) error {
	if err := requireFields(object, path, "message"); err != nil {
		return err
	}
	message, err := objectField(object, path, "message")
	if err != nil {
		return err
	}
	if err := requireFields(message, path+".message", "content"); err != nil {
		return err
	}
	if _, present := message["tool_calls"]; !present {
		if requireToolCalls {
			return fmt.Errorf("%s.message is missing required field %q", path, "tool_calls")
		}
		return nil
	}
	return arrayObjects(message, path+".message", "tool_calls", validateInferenceToolCallShape)
}

func validateInferenceUsageShape(object map[string]any, path string, costField string) error {
	fields := []string{"prompt_tokens", "completion_tokens", "total_tokens"}
	if costField != "" {
		fields = append(fields, costField)
	}
	return requireFields(object, path, fields...)
}

func validateInferenceResponseCoreShape(object map[string]any, provider bool, schema bool, costField string) error {
	fields := []string{"id", "model", "choices", "usage"}
	if provider {
		fields = append(fields, "provider")
	}
	if schema {
		fields = append(fields, "schema")
	}
	if err := requireFields(object, "$", fields...); err != nil {
		return err
	}
	if err := arrayObjects(object, "$", "choices", func(choice map[string]any, path string) error {
		return validateInferenceChoiceShape(choice, path, schema)
	}); err != nil {
		return err
	}
	usage, err := objectField(object, "$", "usage")
	if err != nil {
		return err
	}
	return validateInferenceUsageShape(usage, "$.usage", costField)
}

func validateInferenceMinerResponseShape(object map[string]any) error {
	return validateInferenceResponseCoreShape(object, false, false, "")
}

func validateInferenceProviderResponseShape(object map[string]any) error {
	return validateInferenceResponseCoreShape(object, true, false, "cost")
}

func validateInferenceNormalizedResponseShape(object map[string]any) error {
	return validateInferenceResponseCoreShape(object, true, true, "cost_usd_micros")
}

func validateInferenceProviderSettlementShape(object map[string]any) error {
	if err := requireFields(object, "$",
		"schema", "coding_contract_version", "ticket_id", "case_id", "profile_capability_id",
		"inference_grant_sha256", "grant_id", "generation", "request_id", "request_sequence", "attempt",
		"locked_request_sha256", "outcome", "terminal_error_code", "http_status", "response_sha256",
		"response_digest_kind", "provider_generation_id", "model", "provider_api", "provider_route",
		"receipt_provider", "provider_route_profile", "provider_account_guardrail", "provider_pipeline_policy",
		"provider_cache_policy", "router_metadata_verified", "router_attempts", "pipeline_stages", "fallback_used",
		"usage_available", "prompt_tokens", "completion_tokens", "total_tokens", "cost_available",
		"cost_usd_micros", "timed_out"); err != nil {
		return err
	}
	if err := arrayObjects(object, "$", "router_attempts", func(attempt map[string]any, path string) error {
		return requireFields(attempt, path, "provider", "selected")
	}); err != nil {
		return err
	}
	_, err := arrayField(object, "$", "pipeline_stages")
	return err
}

func validateInferenceReceiptShape(object map[string]any, path string) error {
	return requireFields(object, path,
		"schema", "sequence", "request_sequence", "attempt", "request_id", "locked_request_sha256",
		"prompt_sha256", "tool_schema_sha256", "outcome", "failure_code", "http_status",
		"response_sha256", "response_digest_kind", "provider_generation_id", "provider_settlement_sha256",
		"model", "provider_route",
		"provider_route_profile", "provider_selected",
		"receipt_provider", "fallback_used", "prompt_tokens", "completion_tokens", "total_tokens",
		"cost_usd_micros", "timed_out")
}

func validateInferenceReceiptSetShape(object map[string]any) error {
	if err := requireFields(object, "$", "schema", "coding_contract_version", "ticket_id", "case_id",
		"profile_capability_id", "grant_id", "generation", "inference_grant_sha256", "request_budget",
		"prompt_token_budget", "completion_token_budget", "receipts"); err != nil {
		return err
	}
	return arrayObjects(object, "$", "receipts", validateInferenceReceiptShape)
}

func requireExactInferenceFields(object map[string]any, path string, names ...string) error {
	if err := requireFields(object, path, names...); err != nil {
		return err
	}
	if len(object) != len(names) {
		return fmt.Errorf("%s contains unsupported fields", path)
	}
	return nil
}
