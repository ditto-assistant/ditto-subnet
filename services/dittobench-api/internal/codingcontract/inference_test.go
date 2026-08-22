package codingcontract

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type inferenceExpected struct {
	PromptSHA256                  string              `json:"prompt_sha256"`
	ToolSchemaSHA256              string              `json:"tool_schema_sha256"`
	InferenceGrantSHA256          string              `json:"inference_grant_sha256"`
	RequestSHA256                 []string            `json:"request_sha256"`
	ResponseSHA256                []string            `json:"response_sha256"`
	LockedRequestSHA256           []string            `json:"locked_request_sha256"`
	NormalizedResponseSHA256      []string            `json:"normalized_response_sha256"`
	InvalidProviderResponseSHA256 map[string]string   `json:"invalid_provider_response_sha256"`
	ProviderReceiptSetSHA256      map[string]string   `json:"provider_receipt_set_sha256"`
	ProviderSettlementSHA256      map[string][]string `json:"provider_settlement_sha256"`
	ModelEvidenceSHA256           map[string]string   `json:"model_evidence_sha256"`
}

type inferenceMinerVectors struct {
	SystemPrompt json.RawMessage `json:"system_prompt"`
	ToolSchema   json.RawMessage `json:"tool_schema"`
	Turns        []struct {
		Sequence            uint32            `json:"sequence"`
		Messages            []json.RawMessage `json:"messages"`
		MaxCompletionTokens uint64            `json:"max_completion_tokens"`
		Response            json.RawMessage   `json:"response"`
	} `json:"turns"`
	Expected inferenceExpected `json:"expected"`
}

type inferencePolicyVectors struct {
	Policy                      json.RawMessage              `json:"policy"`
	TaskBudgets                 Budgets                      `json:"task_budgets"`
	LockedRequests              []json.RawMessage            `json:"locked_requests"`
	ProviderResponses           []json.RawMessage            `json:"provider_responses"`
	NormalizedProviderResponses []json.RawMessage            `json:"normalized_provider_responses"`
	ProviderSettlements         map[string][]json.RawMessage `json:"provider_settlements"`
	InvalidProviderResponses    map[string]json.RawMessage   `json:"invalid_provider_response_projections"`
	ReceiptSets                 map[string]json.RawMessage   `json:"receipt_sets"`
	ModelEvidence               map[string]json.RawMessage   `json:"model_evidence"`
	Expected                    inferenceExpected            `json:"expected"`
}

func loadInferenceVector[T any](t *testing.T, name string) T {
	t.Helper()
	body, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	var value T
	if err := json.Unmarshal(body, &value); err != nil {
		t.Fatal(err)
	}
	return value
}

func loadInferenceVectors(t *testing.T) (inferenceMinerVectors, inferencePolicyVectors, InferencePolicy) {
	t.Helper()
	miner := loadInferenceVector[inferenceMinerVectors](t, "coding_inference_miner_v1.json")
	policyVectors := loadInferenceVector[inferencePolicyVectors](t, "coding_inference_policy_v1.json")
	policy, err := ParseInferencePolicy(policyVectors.Policy)
	if err != nil {
		t.Fatal(err)
	}
	if err := policyVectors.TaskBudgets.Validate(); err != nil {
		t.Fatal(err)
	}
	return miner, policyVectors, policy
}

func minerRequestForTurn(turn struct {
	Sequence            uint32            `json:"sequence"`
	Messages            []json.RawMessage `json:"messages"`
	MaxCompletionTokens uint64            `json:"max_completion_tokens"`
	Response            json.RawMessage   `json:"response"`
}, tools []InferenceTool) InferenceMinerRequest {
	return InferenceMinerRequest{
		Model: "openai/gpt-5.6-luna", Messages: cloneRawMessages(turn.Messages),
		Tools: cloneInferenceTools(tools), ToolChoice: "auto",
		Reasoning:           InferenceMinerReasoning{Effort: "medium"},
		MaxCompletionTokens: turn.MaxCompletionTokens, ParallelToolCalls: false,
	}
}

func TestInferenceVectorsMatchCanonicalGoContract(t *testing.T) {
	miner, vectors, policy := loadInferenceVectors(t)
	prompt, err := ParseInferenceSystemPrompt(miner.SystemPrompt)
	if err != nil {
		t.Fatal(err)
	}
	tools, err := ParseInferenceToolSchema(miner.ToolSchema)
	if err != nil {
		t.Fatal(err)
	}
	for label, test := range map[string]struct {
		want string
		call func() (string, error)
	}{
		"policy": {vectors.Expected.InferenceGrantSHA256, func() (string, error) { return InferencePolicySHA256(policy) }},
		"prompt": {miner.Expected.PromptSHA256, func() (string, error) { return InferenceSystemPromptSHA256(prompt) }},
		"tools":  {miner.Expected.ToolSchemaSHA256, func() (string, error) { return InferenceToolSchemaSHA256(tools) }},
	} {
		got, err := test.call()
		if err != nil || got != test.want {
			t.Fatalf("%s digest=%s want=%s err=%v", label, got, test.want, err)
		}
	}
	if policy.PromptSHA256 != miner.Expected.PromptSHA256 || policy.ToolSchemaSHA256 != miner.Expected.ToolSchemaSHA256 {
		t.Fatal("policy did not bind the public prompt and tool vectors")
	}
	if len(miner.Turns) != len(vectors.LockedRequests) || len(miner.Turns) != len(vectors.ProviderResponses) {
		t.Fatal("inference vectors have inconsistent turn coverage")
	}
	for index, turn := range miner.Turns {
		request := minerRequestForTurn(turn, tools.Tools)
		minerDigest, err := InferenceMinerRequestSHA256(policy, request)
		if err != nil || minerDigest != miner.Expected.RequestSHA256[index] {
			t.Fatalf("miner request %d digest=%s want=%s err=%v", index, minerDigest, miner.Expected.RequestSHA256[index], err)
		}
		locked, err := LockInferenceRequest(policy, request)
		if err != nil {
			t.Fatal(err)
		}
		parsedLocked, err := ParseInferenceLockedRequest(vectors.LockedRequests[index], policy)
		if err != nil {
			t.Fatalf("locked request %d parse: %v", index, err)
		}
		lockedDigest, err := InferenceLockedRequestSHA256(policy, locked)
		if err != nil || lockedDigest != vectors.Expected.LockedRequestSHA256[index] {
			t.Fatalf("locked request %d digest=%s want=%s err=%v", index, lockedDigest, vectors.Expected.LockedRequestSHA256[index], err)
		}
		parsedDigest, err := InferenceLockedRequestSHA256(policy, parsedLocked)
		if err != nil || parsedDigest != lockedDigest {
			t.Fatalf("parsed locked request %d digest=%s want=%s err=%v", index, parsedDigest, lockedDigest, err)
		}
		minerResponse, err := ParseInferenceMinerResponse(turn.Response, policy)
		if err != nil {
			t.Fatal(err)
		}
		responseDigest, err := InferenceMinerResponseSHA256(policy, minerResponse)
		if err != nil || responseDigest != miner.Expected.ResponseSHA256[index] {
			t.Fatalf("miner response %d digest=%s want=%s err=%v", index, responseDigest, miner.Expected.ResponseSHA256[index], err)
		}
		providerResponse, err := ParseInferenceProviderResponse(vectors.ProviderResponses[index], policy)
		if err != nil {
			t.Fatal(err)
		}
		expectedNormalized, err := ParseInferenceNormalizedResponse(vectors.NormalizedProviderResponses[index], policy)
		if err != nil {
			t.Fatal(err)
		}
		normalized, err := NormalizeInferenceResponse(policy, providerResponse)
		if err != nil || !reflect.DeepEqual(normalized, expectedNormalized) {
			t.Fatalf("normalized response %d mismatch err=%v", index, err)
		}
		normalizedDigest, err := InferenceNormalizedResponseSHA256(policy, normalized)
		if err != nil || normalizedDigest != vectors.Expected.NormalizedResponseSHA256[index] {
			t.Fatalf("normalized response %d digest=%s want=%s err=%v", index, normalizedDigest, vectors.Expected.NormalizedResponseSHA256[index], err)
		}
	}
	var withoutToolCallsValue map[string]any
	if err := json.Unmarshal(vectors.ProviderResponses[1], &withoutToolCallsValue); err != nil {
		t.Fatal(err)
	}
	choices := withoutToolCallsValue["choices"].([]any)
	message := choices[0].(map[string]any)["message"].(map[string]any)
	delete(message, "tool_calls")
	withoutToolCalls, err := json.Marshal(withoutToolCallsValue)
	if err != nil {
		t.Fatal(err)
	}
	providerWithoutCalls, err := ParseInferenceProviderResponse(withoutToolCalls, policy)
	if err != nil {
		t.Fatalf("provider response without tool_calls was rejected: %v", err)
	}
	normalizedWithoutCalls, err := NormalizeInferenceResponse(policy, providerWithoutCalls)
	if err != nil || normalizedWithoutCalls.Choices[0].Message.ToolCalls == nil ||
		len(normalizedWithoutCalls.Choices[0].Message.ToolCalls) != 0 {
		t.Fatalf("omitted tool_calls were not normalized: %#v err=%v", normalizedWithoutCalls, err)
	}
	providerWithMultipleCalls, err := ParseInferenceProviderResponse(vectors.ProviderResponses[0], policy)
	if err != nil {
		t.Fatal(err)
	}
	secondCall := providerWithMultipleCalls.Choices[0].Message.ToolCalls[0]
	secondCall.ID = "call-second-forbidden"
	providerWithMultipleCalls.Choices[0].Message.ToolCalls = append(
		providerWithMultipleCalls.Choices[0].Message.ToolCalls, secondCall,
	)
	if err := providerWithMultipleCalls.ValidateAgainst(policy); err == nil {
		t.Fatal("provider response with multiple tool calls was accepted")
	}
	var invalidProjection any
	decoder := json.NewDecoder(bytes.NewReader(vectors.InvalidProviderResponses["response_invalid"]))
	decoder.UseNumber()
	if err := decoder.Decode(&invalidProjection); err != nil {
		t.Fatal(err)
	}
	invalidDigest, err := inferenceDigest(invalidProjection, MaxInferenceResponseBytes)
	if err != nil || invalidDigest != vectors.Expected.InvalidProviderResponseSHA256["response_invalid"] {
		t.Fatalf("invalid response digest=%s err=%v", invalidDigest, err)
	}

	authoritySet, err := ParseInferenceReceiptSet(vectors.ReceiptSets["complete"], policy)
	if err != nil {
		t.Fatal(err)
	}
	notInvoked, err := NotInvokedInferenceModelEvidence(policy, inferenceReceiptBinding(authoritySet))
	if err != nil {
		t.Fatal(err)
	}
	assertInferenceEvidenceVector(t, notInvoked, vectors.ModelEvidence["not_invoked"], vectors.Expected.ModelEvidenceSHA256["not_invoked"])
	for _, name := range []string{"complete", "retry_complete", "provider_failure", "response_invalid"} {
		set, err := ParseInferenceReceiptSet(vectors.ReceiptSets[name], policy)
		if err != nil {
			t.Fatalf("%s receipt set: %v", name, err)
		}
		if set.RequestBudget != EffectiveInferenceRequestBudget(vectors.TaskBudgets.WorkspaceToolCalls) ||
			set.PromptTokenBudget != vectors.TaskBudgets.ModelInputTokens ||
			set.CompletionTokenBudget != vectors.TaskBudgets.ModelOutputTokens {
			t.Fatalf("%s receipt set does not bind task budgets: %#v", name, set)
		}
		root, err := InferenceReceiptSetSHA256(policy, set)
		if err != nil || root != vectors.Expected.ProviderReceiptSetSHA256[name] {
			t.Fatalf("%s receipt root=%s want=%s err=%v", name, root, vectors.Expected.ProviderReceiptSetSHA256[name], err)
		}
		settlements := inferenceSettlements(t, vectors, policy, name)
		evidence, err := DeriveInferenceModelEvidence(
			policy, inferenceReceiptBinding(set), set, settlements,
		)
		if err != nil {
			t.Fatalf("%s evidence: %v", name, err)
		}
		assertInferenceEvidenceVector(t, evidence, vectors.ModelEvidence[name], vectors.Expected.ModelEvidenceSHA256[name])
	}
}

func assertInferenceEvidenceVector(t *testing.T, got ModelEvidence, raw json.RawMessage, wantDigest string) {
	t.Helper()
	var want ModelEvidence
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("model evidence mismatch\ngot:  %#v\nwant: %#v", got, want)
	}
	_, _, policy := loadInferenceVectors(t)
	digest, err := InferenceModelEvidenceSHA256(policy, got)
	if err != nil || digest != wantDigest {
		t.Fatalf("model evidence digest=%s want=%s err=%v", digest, wantDigest, err)
	}
}

func inferenceReceiptBinding(set InferenceReceiptSet) InferenceReceiptBinding {
	return InferenceReceiptBinding{
		TicketID: set.TicketID, CaseID: set.CaseID,
		ProfileCapabilityID: set.ProfileCapabilityID, GrantID: set.GrantID,
		Generation: set.Generation, InferenceGrantSHA256: set.InferenceGrantSHA256,
		RequestBudget: set.RequestBudget, PromptTokenBudget: set.PromptTokenBudget,
		CompletionTokenBudget: set.CompletionTokenBudget,
	}
}

func inferenceSettlements(
	t *testing.T,
	vectors inferencePolicyVectors,
	policy InferencePolicy,
	name string,
) []InferenceProviderSettlement {
	t.Helper()
	raw := vectors.ProviderSettlements[name]
	result := make([]InferenceProviderSettlement, len(raw))
	for index, body := range raw {
		settlement, err := ParseInferenceProviderSettlement(body, policy)
		if err != nil {
			t.Fatalf("%s settlement %d: %v", name, index, err)
		}
		digest, err := InferenceProviderSettlementSHA256(policy, settlement)
		if err != nil || digest != vectors.Expected.ProviderSettlementSHA256[name][index] {
			t.Fatalf("%s settlement %d digest=%s err=%v", name, index, digest, err)
		}
		result[index] = settlement
	}
	return result
}

func TestInferenceUnknownFieldsAreNonAuthoritative(t *testing.T) {
	_, vectors, policy := loadInferenceVectors(t)
	originalPolicy, err := InferencePolicySHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	extendedPolicy := bytes.Replace(vectors.Policy, []byte(`"schema":`), []byte(`"future_unsigned":{"nested":true},"schema":`), 1)
	parsedPolicy, err := ParseInferencePolicy(extendedPolicy)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := InferencePolicySHA256(parsedPolicy)
	if err != nil || digest != originalPolicy {
		t.Fatalf("unknown policy field changed authority: %s != %s err=%v", digest, originalPolicy, err)
	}
	originalSet, err := ParseInferenceReceiptSet(vectors.ReceiptSets["complete"], policy)
	if err != nil {
		t.Fatal(err)
	}
	originalRoot, _ := InferenceReceiptSetSHA256(policy, originalSet)
	extendedReceipt := bytes.Replace(
		vectors.ReceiptSets["complete"], []byte(`"sequence": 1,`),
		[]byte(`"future_unsigned":{"nested":true},"sequence": 1,`), 1,
	)
	parsedSet, err := ParseInferenceReceiptSet(extendedReceipt, policy)
	if err != nil {
		t.Fatal(err)
	}
	root, err := InferenceReceiptSetSHA256(policy, parsedSet)
	if err != nil || root != originalRoot {
		t.Fatalf("unknown receipt field changed root: %s != %s err=%v", root, originalRoot, err)
	}
}

func TestInferenceParsersRejectMalformedDocuments(t *testing.T) {
	miner, vectors, policy := loadInferenceVectors(t)
	cases := map[string][]byte{
		"duplicate":     bytes.Replace(vectors.Policy, []byte(`"schema":`), []byte(`"schema":"wrong","schema":`), 1),
		"missing":       bytes.Replace(vectors.Policy, []byte(`"weight_eligible": false,`), nil, 1),
		"trailing":      append(append([]byte(nil), vectors.Policy...), []byte(` {}`)...),
		"invalid UTF-8": append(append([]byte(nil), vectors.Policy...), 0xff),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseInferencePolicy(body); err == nil {
				t.Fatal("malformed policy was accepted")
			}
		})
	}
	for name, number := range map[string]string{
		"huge unknown integer":  strings.Repeat("9", 101),
		"huge unknown exponent": "1e101",
	} {
		t.Run(name, func(t *testing.T) {
			body := bytes.Replace(
				vectors.Policy, []byte(`"schema":`),
				[]byte(`"future_number":`+number+`,"schema":`), 1,
			)
			if _, err := ParseInferencePolicy(body); err == nil {
				t.Fatal("oversized unknown numeric lexeme was accepted")
			}
		})
	}
	allowedUnknown := bytes.Replace(
		vectors.Policy, []byte(`"schema":`), []byte(`"future_number":1e100,"schema":`), 1,
	)
	if _, err := ParseInferencePolicy(allowedUnknown); err != nil {
		t.Fatalf("bounded unknown numeric lexeme was rejected: %v", err)
	}
	nested := bytes.Replace(vectors.Policy, []byte(`"schema":`), []byte(`"future_nested":`+strings.Repeat("[", 33)+`0`+strings.Repeat("]", 33)+`,"schema":`), 1)
	if _, err := ParseInferencePolicy(nested); err == nil {
		t.Fatal("excessively nested policy was accepted")
	}
	oversized := append(append([]byte(nil), vectors.Policy...), bytes.Repeat([]byte(" "), MaxInferencePolicyBytes-len(vectors.Policy)+1)...)
	if _, err := ParseInferencePolicy(oversized); err == nil {
		t.Fatal("oversized policy was accepted")
	}
	missingNullable := bytes.Replace(vectors.ReceiptSets["provider_failure"], []byte(`"response_sha256": null,`), nil, 1)
	if _, err := ParseInferenceReceiptSet(missingNullable, policy); err == nil {
		t.Fatal("missing nullable receipt field was accepted")
	}
	for name, replacement := range map[string]string{
		"negative zero": `"maximum": -0`,
		"decimal":       `"maximum": 8.0`,
		"exponent":      `"maximum": 8e0`,
		"integer range": `"maximum": 9223372036854775808`,
	} {
		t.Run("tool number "+name, func(t *testing.T) {
			changed := bytes.Replace(miner.ToolSchema, []byte(`"maximum": 8`), []byte(replacement), 1)
			if _, err := ParseInferenceToolSchema(changed); err == nil {
				t.Fatal("noncanonical JSON Schema number was accepted")
			}
		})
	}
}

func TestInferencePolicyAndRequestAuthorityFailClosed(t *testing.T) {
	miner, vectors, policy := loadInferenceVectors(t)
	tools, err := ParseInferenceToolSchema(miner.ToolSchema)
	if err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*InferencePolicy){
		"weighted":       func(value *InferencePolicy) { value.WeightEligible = true },
		"fallback":       func(value *InferencePolicy) { value.AllowFallbacks = true },
		"zdr":            func(value *InferencePolicy) { value.ZDR = false },
		"reasoning":      func(value *InferencePolicy) { value.ReasoningEffort = "high" },
		"total budget":   func(value *InferencePolicy) { value.MaxTotalTokens-- },
		"request bytes":  func(value *InferencePolicy) { value.MaxRequestBytes++ },
		"response bytes": func(value *InferencePolicy) { value.MaxResponseBytes++ },
		"retries":        func(value *InferencePolicy) { value.MaxRetries = 101 },
	} {
		t.Run(name, func(t *testing.T) {
			changed := policy
			mutate(&changed)
			if err := changed.Validate(); err == nil {
				t.Fatal("invalid policy was accepted")
			}
		})
	}
	request := minerRequestForTurn(miner.Turns[0], tools.Tools)
	for name, mutate := range map[string]func(*InferenceMinerRequest){
		"model": func(value *InferenceMinerRequest) { value.Model = "openai/other" },
		"prompt": func(value *InferenceMinerRequest) {
			value.Messages[0] = json.RawMessage(`{"role":"system","content":"different"}`)
		},
		"tools": func(value *InferenceMinerRequest) { value.Tools[0].Function.Description = "different" },
		"tokens": func(value *InferenceMinerRequest) {
			value.MaxCompletionTokens = policy.MaxCompletionTokensPerRequest + 1
		},
	} {
		t.Run(name, func(t *testing.T) {
			changed := InferenceMinerRequest{
				Model: request.Model, Messages: cloneRawMessages(request.Messages), Tools: cloneInferenceTools(request.Tools),
				ToolChoice: request.ToolChoice, Reasoning: request.Reasoning,
				MaxCompletionTokens: request.MaxCompletionTokens, ParallelToolCalls: request.ParallelToolCalls,
			}
			mutate(&changed)
			if _, err := LockInferenceRequest(policy, changed); err == nil {
				t.Fatal("request escaped policy")
			}
		})
	}
	for name, raw := range map[string][]byte{
		"unknown top-level": bytes.Replace(
			vectors.LockedRequests[0], []byte(`{`), []byte(`{"provider_override":"forbidden",`), 1,
		),
		"unknown message": bytes.Replace(
			vectors.LockedRequests[0], []byte(`"role": "system"`), []byte(`"future_model_visible_field":true,"role": "system"`), 1,
		),
		"unknown tool wrapper": bytes.Replace(
			vectors.LockedRequests[0], []byte(`"type": "function"`), []byte(`"future_tool_field":true,"type": "function"`), 1,
		),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseInferenceLockedRequest(raw, policy); err == nil {
				t.Fatal("unsupported model-visible field was accepted")
			}
		})
	}
}

func TestInferenceReceiptOrderIdentityNullabilityAndBudgetsFailClosed(t *testing.T) {
	_, vectors, policy := loadInferenceVectors(t)
	base, err := ParseInferenceReceiptSet(vectors.ReceiptSets["complete"], policy)
	if err != nil {
		t.Fatal(err)
	}
	baseBinding := inferenceReceiptBinding(base)
	baseSettlements := inferenceSettlements(t, vectors, policy, "complete")
	if _, err := DeriveInferenceModelEvidence(policy, baseBinding, base, nil); err == nil {
		t.Fatal("missing provider settlements produced evidence")
	}
	reorderedSettlements := cloneInferenceSettlements(baseSettlements)
	reorderedSettlements[0], reorderedSettlements[1] = reorderedSettlements[1], reorderedSettlements[0]
	if _, err := DeriveInferenceModelEvidence(policy, baseBinding, base, reorderedSettlements); err == nil {
		t.Fatal("reordered provider settlements produced evidence")
	}
	tamperedSettlements := cloneInferenceSettlements(baseSettlements)
	tamperedSettlements[0].CostUSDMicros++
	if _, err := DeriveInferenceModelEvidence(policy, baseBinding, base, tamperedSettlements); err == nil {
		t.Fatal("tampered provider settlement produced evidence")
	}
	for name, mutate := range map[string]func(*InferenceReceiptBinding){
		"ticket":        func(value *InferenceReceiptBinding) { value.TicketID = "77777777-7777-4777-8777-777777777777" },
		"case":          func(value *InferenceReceiptBinding) { value.CaseID = "case-other" },
		"profile":       func(value *InferenceReceiptBinding) { value.ProfileCapabilityID = "profile-other" },
		"grant":         func(value *InferenceReceiptBinding) { value.GrantID = "88888888-8888-4888-8888-888888888888" },
		"generation":    func(value *InferenceReceiptBinding) { value.Generation++ },
		"policy":        func(value *InferenceReceiptBinding) { value.InferenceGrantSHA256 = strings.Repeat("f", 64) },
		"requests":      func(value *InferenceReceiptBinding) { value.RequestBudget-- },
		"prompt budget": func(value *InferenceReceiptBinding) { value.PromptTokenBudget-- },
		"output budget": func(value *InferenceReceiptBinding) { value.CompletionTokenBudget-- },
	} {
		t.Run("binding "+name, func(t *testing.T) {
			drifted := baseBinding
			mutate(&drifted)
			if _, err := DeriveInferenceModelEvidence(policy, drifted, base, baseSettlements); err == nil {
				t.Fatal("foreign receipt binding produced evidence")
			}
		})
	}
	mutations := map[string]func(*InferenceReceiptSet){
		"receipt schema": func(value *InferenceReceiptSet) { value.Receipts[0].Schema = "other" },
		"order": func(value *InferenceReceiptSet) {
			value.Receipts[0], value.Receipts[1] = value.Receipts[1], value.Receipts[0]
		},
		"sequence":           func(value *InferenceReceiptSet) { value.Receipts[1].Sequence = 3 },
		"request sequence":   func(value *InferenceReceiptSet) { value.Receipts[1].RequestSequence = 3 },
		"request ID reuse":   func(value *InferenceReceiptSet) { value.Receipts[1].RequestID = value.Receipts[0].RequestID },
		"grant":              func(value *InferenceReceiptSet) { value.InferenceGrantSHA256 = strings.Repeat("f", 64) },
		"grant ID":           func(value *InferenceReceiptSet) { value.GrantID = "{44444444-4444-4444-8444-444444444444}" },
		"generation":         func(value *InferenceReceiptSet) { value.Generation = 0 },
		"fallback":           func(value *InferenceReceiptSet) { value.Receipts[0].FallbackUsed = true },
		"provider":           func(value *InferenceReceiptSet) { value.Receipts[0].ReceiptProvider = nil },
		"response":           func(value *InferenceReceiptSet) { value.Receipts[0].ResponseSHA256 = nil },
		"missing generation": func(value *InferenceReceiptSet) { value.Receipts[0].ProviderGenerationID = nil },
		"duplicate settlement": func(value *InferenceReceiptSet) {
			value.Receipts[1].ProviderSettlementSHA256 = value.Receipts[0].ProviderSettlementSHA256
		},
		"duplicate provider generation": func(value *InferenceReceiptSet) {
			generation := *value.Receipts[0].ProviderGenerationID
			value.Receipts[1].ProviderGenerationID = &generation
		},
		"total": func(value *InferenceReceiptSet) { value.Receipts[0].TotalTokens++ },
		"prompt budget": func(value *InferenceReceiptSet) {
			value.Receipts[0].PromptTokens = policy.MaxPromptTokens + 1
			value.Receipts[0].TotalTokens = value.Receipts[0].PromptTokens + value.Receipts[0].CompletionTokens
		},
		"cost budget": func(value *InferenceReceiptSet) { value.Receipts[0].CostUSDMicros = policy.MaxCostUSDMicros + 1 },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			changed := base.Clone()
			mutate(&changed)
			if _, err := DeriveInferenceModelEvidence(policy, baseBinding, changed, baseSettlements); err == nil {
				t.Fatal("invalid receipt set produced evidence")
			}
		})
	}

	retry, err := ParseInferenceReceiptSet(vectors.ReceiptSets["retry_complete"], policy)
	if err != nil {
		t.Fatal(err)
	}
	retryBinding := inferenceReceiptBinding(retry)
	retrySettlements := inferenceSettlements(t, vectors, policy, "retry_complete")
	for name, mutate := range map[string]func(*InferenceReceipt){
		"retry selected provider": func(value *InferenceReceipt) { value.ProviderSelected = true },
		"retry receipt provider":  func(value *InferenceReceipt) { provider := policy.ReceiptProvider; value.ReceiptProvider = &provider },
		"retry usage":             func(value *InferenceReceipt) { value.PromptTokens = 1; value.TotalTokens = 1 },
		"retry response":          func(value *InferenceReceipt) { digest := strings.Repeat("1", 64); value.ResponseSHA256 = &digest },
		"retry timed out":         func(value *InferenceReceipt) { value.TimedOut = true },
	} {
		t.Run(name, func(t *testing.T) {
			changed := retry.Clone()
			mutate(&changed.Receipts[0])
			if _, err := DeriveInferenceModelEvidence(policy, retryBinding, changed, retrySettlements); err == nil {
				t.Fatal("invalid receipt-free retry produced evidence")
			}
		})
	}

	failure, err := ParseInferenceReceiptSet(vectors.ReceiptSets["provider_failure"], policy)
	if err != nil {
		t.Fatal(err)
	}
	failureBinding := inferenceReceiptBinding(failure)
	failureSettlements := inferenceSettlements(t, vectors, policy, "provider_failure")
	if !failureSettlements[0].UsageAvailable || !failureSettlements[0].CostAvailable {
		t.Fatal("selected-provider failure did not retain settled accounting")
	}
	for name, mutate := range map[string]func(*InferenceProviderSettlement){
		"missing usage settlement": func(value *InferenceProviderSettlement) { value.UsageAvailable = false },
		"missing cost settlement":  func(value *InferenceProviderSettlement) { value.CostAvailable = false },
	} {
		t.Run(name, func(t *testing.T) {
			changed := cloneInferenceSettlements(failureSettlements)
			mutate(&changed[0])
			if _, err := DeriveInferenceModelEvidence(policy, failureBinding, failure, changed); err == nil {
				t.Fatal("provider failure with unavailable accounting produced evidence")
			}
		})
	}
	if retrySettlements[0].UsageAvailable || retrySettlements[0].CostAvailable {
		t.Fatal("pre-provider retry claimed provider accounting")
	}
	for name, mutate := range map[string]func(*InferenceProviderSettlement){
		"retry claims usage": func(value *InferenceProviderSettlement) { value.UsageAvailable = true },
		"retry claims cost":  func(value *InferenceProviderSettlement) { value.CostAvailable = true },
	} {
		t.Run(name, func(t *testing.T) {
			changed := cloneInferenceSettlements(retrySettlements)
			mutate(&changed[0])
			if _, err := DeriveInferenceModelEvidence(policy, retryBinding, retry, changed); err == nil {
				t.Fatal("pre-provider retry with false accounting availability produced evidence")
			}
		})
	}
	for name, mutate := range map[string]func(*InferenceReceipt){
		"missing failure": func(value *InferenceReceipt) { value.FailureCode = nil },
		"timeout flag":    func(value *InferenceReceipt) { value.TimedOut = false },
		"wrong provider":  func(value *InferenceReceipt) { provider := "Other"; value.ReceiptProvider = &provider },
	} {
		t.Run(name, func(t *testing.T) {
			changed := failure.Clone()
			mutate(&changed.Receipts[0])
			if _, err := DeriveInferenceModelEvidence(policy, failureBinding, changed, failureSettlements); err == nil {
				t.Fatal("invalid provider failure produced evidence")
			}
		})
	}
	afterFailure := failure.Clone()
	next := base.Receipts[1]
	next.Sequence = 2
	next.RequestSequence = 2
	afterFailure.Receipts = append(afterFailure.Receipts, next)
	if _, err := DeriveInferenceModelEvidence(policy, failureBinding, afterFailure, failureSettlements); err == nil {
		t.Fatal("a new request was admitted after terminal provider failure")
	}
}

func TestInferenceReturnedValuesAreDeepOwned(t *testing.T) {
	miner, vectors, policy := loadInferenceVectors(t)
	tools, err := ParseInferenceToolSchema(miner.ToolSchema)
	if err != nil {
		t.Fatal(err)
	}
	request := minerRequestForTurn(miner.Turns[0], tools.Tools)
	locked, err := LockInferenceRequest(policy, request)
	if err != nil {
		t.Fatal(err)
	}
	request.Messages[0][0] = 'x'
	request.Tools[0].Function.Parameters[0] = 'x'
	if locked.Messages[0][0] == 'x' || locked.Tools[0].Function.Parameters[0] == 'x' {
		t.Fatal("locked request aliases miner-owned buffers")
	}
	set, err := ParseInferenceReceiptSet(vectors.ReceiptSets["complete"], policy)
	if err != nil {
		t.Fatal(err)
	}
	clone := set.Clone()
	*clone.Receipts[0].ResponseSHA256 = strings.Repeat("f", 64)
	*clone.Receipts[0].ProviderGenerationID = "generation-mutated"
	if *set.Receipts[0].ResponseSHA256 == strings.Repeat("f", 64) ||
		*set.Receipts[0].ProviderGenerationID == "generation-mutated" {
		t.Fatal("receipt clone aliases nullable fields")
	}
	provider, err := ParseInferenceProviderResponse(vectors.ProviderResponses[0], policy)
	if err != nil {
		t.Fatal(err)
	}
	normalized, err := NormalizeInferenceResponse(policy, provider)
	if err != nil {
		t.Fatal(err)
	}
	provider.Choices[0].Message.ToolCalls[0].Function.Name = "mutated"
	if normalized.Choices[0].Message.ToolCalls[0].Function.Name == "mutated" {
		t.Fatal("normalized response aliases provider-owned choices")
	}
	settlements := inferenceSettlements(t, vectors, policy, "complete")
	settlementClone := cloneInferenceSettlements(settlements)
	*settlementClone[0].ProviderGenerationID = "generation-mutated"
	settlementClone[0].RouterAttempts[0].Provider = "Other"
	settlementClone[0].PipelineStages = append(settlementClone[0].PipelineStages, "plugin")
	if *settlements[0].ProviderGenerationID == "generation-mutated" ||
		settlements[0].RouterAttempts[0].Provider == "Other" || len(settlements[0].PipelineStages) != 0 {
		t.Fatal("provider settlement clone aliases caller-owned state")
	}
}

func TestInferenceProviderCostUsesExactHalfEvenMicros(t *testing.T) {
	for raw, want := range map[string]uint64{
		"0": 0, "0.0000005": 0, "0.0000015": 2, "0.001234": 1234, "1e-6": 1,
	} {
		got, ok := inferenceCostMicros(json.Number(raw))
		if !ok || got != want {
			t.Fatalf("cost %s micros=%d,%v want=%d", raw, got, ok, want)
		}
	}
	for _, raw := range []string{
		"-0.1", "100.000001", "1e101", strings.Repeat("9", 65), "NaN", "",
	} {
		if got, ok := inferenceCostMicros(json.Number(raw)); ok {
			t.Fatalf("invalid cost %q produced %d micros", raw, got)
		}
	}
}
