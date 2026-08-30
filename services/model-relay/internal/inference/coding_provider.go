package inference

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"mime"
)

type codingProviderOutcome struct {
	settlement        codingProviderSettlement
	normalized        []byte
	failureProjection []byte
}

func (d *Deps) completeCodingProvider(
	ctx context.Context,
	dispatch codingDispatchRequest,
	lockedBody []byte,
) (codingProviderOutcome, error) {
	headers := map[string]string{
		"Authorization":         "Bearer " + d.Cfg.Inference.OpenRouterAPIKey,
		"Content-Type":          "application/json",
		"HTTP-Referer":          "https://heyditto.ai/",
		"X-OpenRouter-Title":    "DittoBench Coding",
		"X-OpenRouter-Metadata": "enabled",
		"X-OpenRouter-Cache":    "false",
	}
	result, err := postOnce(
		ctx,
		d.Upstream,
		d.Cfg.Inference.UpstreamURL,
		lockedBody,
		headers,
		codingMaxResponseBytes,
		codingProviderTimeoutSeconds,
	)
	if err != nil || result == nil || result.bodyOverLimit {
		return codingProviderOutcome{}, errors.New("coding provider settlement unavailable")
	}
	defer zeroCodingBytes(result.body)
	if !codingJSONContentType(result.header.Get("Content-Type")) {
		return codingProviderOutcome{}, errors.New("coding provider content type is invalid")
	}
	payload, ok := decodeCodingJSON(result.body)
	if !ok {
		return codingProviderOutcome{}, errors.New("coding provider response is not trusted JSON")
	}
	selected, metadataOK := codingRouterSelection(payload)
	if !metadataOK {
		return codingProviderOutcome{}, errors.New("coding provider router metadata is invalid")
	}
	if codingReceiptFreeResponse(result.status, payload, selected) {
		return codingProviderOutcome{
			settlement: codingSettlementBase(dispatch, result.status, false),
		}, nil
	}
	if result.status >= 200 && result.status < 300 {
		normalized, normalizedSHA256, prompt, completion, cost, generationID, normalizeErr :=
			normalizeCodingProviderResponse(payload, selected)
		if normalizeErr == nil {
			settlement := codingSettlementBase(dispatch, result.status, true)
			settlement.Outcome = "complete"
			settlement.TerminalErrorCode = nil
			settlement.ResponseSHA256 = &normalizedSHA256
			settlement.ResponseDigestKind = "normalized_v1"
			settlement.ProviderGenerationID = &generationID
			settlement.ReceiptProvider = stringPointer(codingReceiptProvider)
			settlement.UsageAvailable = true
			settlement.PromptTokens = prompt
			settlement.CompletionTokens = completion
			settlement.TotalTokens = prompt + completion
			settlement.CostAvailable = true
			settlement.CostUSDMicros = cost
			return codingProviderOutcome{settlement: settlement, normalized: normalized}, nil
		}
		if !selected {
			return codingProviderOutcome{}, errors.New("coding successful response had no selected provider")
		}
		return codingProviderFailureOutcome(dispatch, result.status, payload, selected, "provider_response_invalid")
	}
	return codingProviderFailureOutcome(dispatch, result.status, payload, selected, "provider_http")
}

func codingJSONContentType(value string) bool {
	mediaType, _, err := mime.ParseMediaType(value)
	return err == nil && mediaType == "application/json"
}

func codingSettlementBase(
	dispatch codingDispatchRequest,
	httpStatus int,
	selected bool,
) codingProviderSettlement {
	outcome := "receipt_free_retry"
	failure := "pre_provider_unavailable"
	return codingProviderSettlement{
		Schema: codingSettlementSchema, CodingContractVersion: 1,
		TicketID: dispatch.TicketID, CaseID: dispatch.CaseID,
		ProfileCapabilityID:  dispatch.ProfileCapabilityID,
		InferenceGrantSHA256: dispatch.InferenceGrantSHA256,
		GrantID:              dispatch.GrantID, Generation: dispatch.Generation,
		RequestID: dispatch.RequestID, RequestSequence: dispatch.RequestSequence,
		Attempt: dispatch.Attempt, LockedRequestSHA256: dispatch.LockedRequestSHA256,
		Outcome: outcome, TerminalErrorCode: &failure, HTTPStatus: httpStatus,
		ResponseSHA256: nil, ResponseDigestKind: "none", ProviderGenerationID: nil,
		Model: codingModel, ProviderAPI: codingProviderAPI, ProviderRoute: codingProviderRoute,
		ReceiptProvider: nil, ProviderRouteProfile: codingProviderRouteProfile,
		ProviderAccountGuardrail: codingAccountGuardrail,
		ProviderPipelinePolicy:   codingPipelinePolicy, ProviderCachePolicy: codingCachePolicy,
		RouterMetadataVerified: true,
		RouterAttempts:         []codingRouterAttempt{{Provider: codingReceiptProvider, Selected: selected}},
		PipelineStages:         []string{}, FallbackUsed: false,
		UsageAvailable: false, PromptTokens: 0, CompletionTokens: 0, TotalTokens: 0,
		CostAvailable: false, CostUSDMicros: 0, TimedOut: false,
	}
}

func codingProviderFailureOutcome(
	dispatch codingDispatchRequest,
	status int,
	payload map[string]any,
	selected bool,
	code string,
) (codingProviderOutcome, error) {
	settlement := codingSettlementBase(dispatch, status, selected)
	settlement.Outcome = "provider_failure"
	settlement.TerminalErrorCode = &code
	failureProjection, err := codingCanonicalJSON(payload)
	if err != nil || len(failureProjection) == 0 || len(failureProjection) > codingMaxResponseBytes {
		return codingProviderOutcome{}, errors.New("coding provider failure projection is unavailable")
	}
	responseSHA256, err := codingCanonicalSHA256(payload)
	if err != nil {
		return codingProviderOutcome{}, errors.New("coding provider failure digest is unavailable")
	}
	settlement.ResponseSHA256 = &responseSHA256
	settlement.ResponseDigestKind = "canonical_json_v1"
	if selected {
		if provider, present := payload["provider"]; present && provider != codingReceiptProvider {
			return codingProviderOutcome{}, errors.New("coding selected provider identity disagrees")
		}
		if model, present := payload["model"]; present && model != codingModel {
			if code != "provider_response_invalid" {
				return codingProviderOutcome{}, errors.New("coding selected provider model disagrees")
			}
		}
		prompt, completion, cost, generationID, ok := codingProviderAccounting(payload)
		if !ok {
			return codingProviderOutcome{}, errors.New("coding selected provider accounting is unavailable")
		}
		settlement.ProviderGenerationID = generationID
		settlement.ReceiptProvider = stringPointer(codingReceiptProvider)
		settlement.UsageAvailable = true
		settlement.PromptTokens = prompt
		settlement.CompletionTokens = completion
		settlement.TotalTokens = prompt + completion
		settlement.CostAvailable = true
		settlement.CostUSDMicros = cost
	}
	return codingProviderOutcome{settlement: settlement, failureProjection: failureProjection}, nil
}

func codingRouterSelection(payload map[string]any) (bool, bool) {
	metadata, ok := payload["openrouter_metadata"].(map[string]any)
	if !ok || !onlyJSONKeys(metadata, "requested", "strategy", "attempt", "endpoints", "pipeline") ||
		len(metadata) != 5 || metadata["requested"] != codingModel || metadata["strategy"] != "direct" {
		return false, false
	}
	attempt, ok := codingInt64(metadata["attempt"])
	if !ok || attempt < 0 || attempt > 100 {
		return false, false
	}
	pipeline, ok := metadata["pipeline"].([]any)
	if !ok || len(pipeline) != 0 {
		return false, false
	}
	endpoints, ok := metadata["endpoints"].(map[string]any)
	if !ok || !onlyJSONKeys(endpoints, "total", "available") || len(endpoints) != 2 {
		return false, false
	}
	total, ok := codingInt64(endpoints["total"])
	available, availableOK := endpoints["available"].([]any)
	if !ok || !availableOK || total != 1 || len(available) != 1 {
		return false, false
	}
	endpoint, ok := available[0].(map[string]any)
	if !ok || !onlyJSONKeys(endpoint, "provider", "model", "selected") || len(endpoint) != 3 ||
		endpoint["provider"] != codingReceiptProvider || endpoint["model"] != codingModel {
		return false, false
	}
	selected, ok := endpoint["selected"].(bool)
	return selected, ok
}

func codingReceiptFreeResponse(status int, payload map[string]any, selected bool) bool {
	if selected {
		return false
	}
	switch status {
	case 404, 408, 429, 500, 502, 503, 504:
	default:
		return false
	}
	metadata := payload["openrouter_metadata"].(map[string]any)
	attempt, ok := codingInt64(metadata["attempt"])
	if !ok || attempt != 0 {
		return false
	}
	if _, ok := payload["error"].(map[string]any); !ok {
		return false
	}
	for _, forbidden := range []string{
		"id", "generation", "generation_id", "model", "provider", "choices", "usage", "cost",
	} {
		if _, present := payload[forbidden]; present {
			return false
		}
	}
	return true
}

func normalizeCodingProviderResponse(
	payload map[string]any,
	selected bool,
) ([]byte, string, int64, int64, int64, string, error) {
	if !selected {
		return nil, "", 0, 0, 0, "", errors.New("provider was not selected")
	}
	generationID, ok := payload["id"].(string)
	if !ok || !validCodingIdentifier(generationID, 256) || payload["model"] != codingModel {
		return nil, "", 0, 0, 0, "", errors.New("provider identity is invalid")
	}
	if provider, present := payload["provider"]; present && provider != codingReceiptProvider {
		return nil, "", 0, 0, 0, "", errors.New("provider receipt identity is invalid")
	}
	choices, ok := payload["choices"].([]any)
	if !ok || len(choices) != 1 {
		return nil, "", 0, 0, 0, "", errors.New("provider choices are invalid")
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return nil, "", 0, 0, 0, "", errors.New("provider choice is invalid")
	}
	if _, hasError := choice["error"]; hasError {
		return nil, "", 0, 0, 0, "", errors.New("provider choice contains an error")
	}
	if _, hasError := payload["error"]; hasError {
		return nil, "", 0, 0, 0, "", errors.New("provider response contains an error")
	}
	message, ok := choice["message"].(map[string]any)
	if !ok {
		return nil, "", 0, 0, 0, "", errors.New("provider message is invalid")
	}
	if role, present := message["role"]; present && role != "assistant" {
		return nil, "", 0, 0, 0, "", errors.New("provider message role is invalid")
	}
	var content *string
	if value, present := message["content"]; present && value != nil {
		text, ok := value.(string)
		if !ok {
			return nil, "", 0, 0, 0, "", errors.New("provider content is invalid")
		}
		content = &text
	}
	toolCalls := make([]codingToolCall, 0)
	if value, present := message["tool_calls"]; present && value != nil {
		calls, ok := value.([]any)
		if !ok || len(calls) > 1 {
			return nil, "", 0, 0, 0, "", errors.New("provider tool calls are invalid")
		}
		for _, raw := range calls {
			if validateCodingToolCallShape(raw) != nil {
				return nil, "", 0, 0, 0, "", errors.New("provider tool call is invalid")
			}
			encoded, _ := json.Marshal(raw)
			var call codingToolCall
			if json.Unmarshal(encoded, &call) != nil {
				return nil, "", 0, 0, 0, "", errors.New("provider tool call is malformed")
			}
			toolCalls = append(toolCalls, call)
		}
	}
	if content == nil && len(toolCalls) == 0 {
		return nil, "", 0, 0, 0, "", errors.New("provider response is empty")
	}
	prompt, completion, cost, _, ok := codingProviderAccounting(payload)
	if !ok || prompt > codingMaxPromptTokens || completion > codingMaxCompletionPerCall ||
		prompt > codingMaxTotalTokens-completion {
		return nil, "", 0, 0, 0, "", errors.New("provider accounting is invalid")
	}
	normalized := codingNormalizedResponse{
		Schema: codingNormalizedSchema, ID: generationID, Model: codingModel, Provider: codingReceiptProvider,
		Choices: []codingResponseChoice{{Message: codingResponseMessage{Content: content, ToolCalls: toolCalls}}},
		Usage: codingNormalizedUsage{
			PromptTokens: prompt, CompletionTokens: completion, TotalTokens: prompt + completion,
			CostUSDMicros: cost,
		},
	}
	normalizedBytes, err := codingCanonicalJSON(normalized)
	if err != nil || len(normalizedBytes) > codingMaxResponseBytes {
		return nil, "", 0, 0, 0, "", errors.New("normalized provider response is invalid")
	}
	normalizedSHA256, err := codingCanonicalSHA256(normalized)
	if err != nil {
		return nil, "", 0, 0, 0, "", err
	}
	return normalizedBytes, normalizedSHA256, prompt, completion, cost, generationID, nil
}

func codingProviderAccounting(payload map[string]any) (int64, int64, int64, *string, bool) {
	usage, ok := payload["usage"].(map[string]any)
	if !ok {
		return 0, 0, 0, nil, false
	}
	prompt, promptOK := codingInt64(usage["prompt_tokens"])
	completion, completionOK := codingInt64(usage["completion_tokens"])
	total, totalOK := codingInt64(usage["total_tokens"])
	cost, costOK := codingFloatToMicros(usage["cost"])
	if !promptOK || !completionOK || !totalOK || !costOK || prompt < 0 || completion < 0 ||
		prompt > math.MaxInt64-completion || total != prompt+completion ||
		prompt > codingMaxPromptTokens || completion > codingMaxCompletionPerCall ||
		total > codingMaxTotalTokens || cost > codingMaxCostUSDMicros {
		return 0, 0, 0, nil, false
	}
	var generationID *string
	if value, present := payload["id"]; present {
		id, ok := value.(string)
		if !ok || !validCodingIdentifier(id, 256) {
			return 0, 0, 0, nil, false
		}
		generationID = &id
	}
	return prompt, completion, cost, generationID, true
}

func stringPointer(value string) *string { return &value }

func codingSettlementDigest(settlement codingProviderSettlement) (string, []byte, error) {
	canonical, err := codingCanonicalJSON(settlement)
	if err != nil || len(canonical) == 0 || len(canonical) > codingMaxSettlementBytes {
		return "", nil, errors.New("coding settlement is outside its bound")
	}
	digest, err := codingCanonicalSHA256(settlement)
	if err != nil {
		return "", nil, err
	}
	compact, err := compactJSON(settlement)
	if err != nil || len(compact) == 0 || len(compact) > codingMaxSettlementBytes {
		return "", nil, errors.New("coding settlement storage projection is invalid")
	}
	return digest, compact, nil
}

func codingDispatchResultBody(
	dispatch codingDispatchRequest,
	outcome codingProviderOutcome,
) ([]byte, error) {
	var normalized *string
	if len(outcome.normalized) != 0 {
		value := base64.StdEncoding.EncodeToString(outcome.normalized)
		normalized = &value
	}
	var failure *string
	if len(outcome.failureProjection) != 0 {
		value := base64.StdEncoding.EncodeToString(outcome.failureProjection)
		failure = &value
	}
	return compactJSON(codingDispatchResult{
		Schema: codingDispatchResultSchema, CodingContractVersion: 1, WeightEligible: false,
		Sequence: dispatch.Sequence, Settlement: outcome.settlement,
		NormalizedResponseBase64: normalized, FailureResponseProjectionBase64: failure,
	})
}
