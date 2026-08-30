package codingrelay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

type relayPolicyVector struct {
	Policy                      json.RawMessage            `json:"policy"`
	LockedRequests              []json.RawMessage          `json:"locked_requests"`
	NormalizedProviderResponses []json.RawMessage          `json:"normalized_provider_responses"`
	InvalidProviderResponses    map[string]json.RawMessage `json:"invalid_provider_response_projections"`
}

type fixtureClock struct {
	mu  sync.Mutex
	now time.Time
}

func (clock *fixtureClock) Now() time.Time {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	return clock.now
}

func (clock *fixtureClock) Advance(delta time.Duration) {
	clock.mu.Lock()
	clock.now = clock.now.Add(delta)
	clock.mu.Unlock()
}

func (clock *fixtureClock) Rewind(delta time.Duration) {
	clock.mu.Lock()
	clock.now = clock.now.Add(-delta)
	clock.mu.Unlock()
}

type upstreamFunc func(context.Context, UpstreamRequest) (UpstreamResult, error)

func (function upstreamFunc) Complete(
	ctx context.Context,
	request UpstreamRequest,
) (UpstreamResult, error) {
	return function(ctx, request)
}

type fakeJournal struct {
	mu sync.Mutex

	snapshot  JournalSnapshot
	bindings  []Binding
	begins    int
	completes int
	revokes   int

	loadErr          error
	beginErr         error
	completeErr      error
	revokeErr        error
	skipBindingCheck bool
}

func (journal *fakeJournal) Load(_ context.Context, binding Binding) (JournalSnapshot, error) {
	journal.mu.Lock()
	defer journal.mu.Unlock()
	journal.bindings = append(journal.bindings, cloneBinding(binding))
	if !journal.skipBindingCheck && journal.snapshot.Binding != nil && !bindingMatches(binding, *journal.snapshot.Binding) {
		return JournalSnapshot{}, errors.New("journal binding mismatch")
	}
	return cloneSnapshot(journal.snapshot), journal.loadErr
}

func (journal *fakeJournal) Begin(
	_ context.Context,
	binding Binding,
	dispatch DispatchRecord,
) error {
	journal.mu.Lock()
	defer journal.mu.Unlock()
	journal.bindings = append(journal.bindings, cloneBinding(binding))
	journal.begins++
	if journal.beginErr != nil {
		return journal.beginErr
	}
	if journal.snapshot.Binding == nil {
		stored := cloneBinding(binding)
		journal.snapshot.Binding = &stored
	}
	if len(journal.snapshot.Entries) > 0 {
		last := journal.snapshot.Entries[len(journal.snapshot.Entries)-1]
		if !last.Completed {
			if reflect.DeepEqual(last.Dispatch, dispatch) {
				return nil
			}
			return errors.New("conflicting dispatch")
		}
	}
	if dispatch.Sequence != uint32(len(journal.snapshot.Entries)+1) {
		return errors.New("dispatch sequence gap")
	}
	journal.snapshot.Entries = append(journal.snapshot.Entries, JournalEntry{
		Dispatch: cloneDispatch(dispatch),
	})
	return nil
}

func (journal *fakeJournal) Complete(
	_ context.Context,
	binding Binding,
	entry JournalEntry,
) error {
	journal.mu.Lock()
	defer journal.mu.Unlock()
	journal.bindings = append(journal.bindings, cloneBinding(binding))
	journal.completes++
	if journal.completeErr != nil {
		return journal.completeErr
	}
	if len(journal.snapshot.Entries) == 0 {
		return errors.New("completion without dispatch")
	}
	index := len(journal.snapshot.Entries) - 1
	current := journal.snapshot.Entries[index]
	if current.Completed {
		if reflect.DeepEqual(current, entry) {
			return nil
		}
		return errors.New("conflicting completion")
	}
	if !reflect.DeepEqual(current.Dispatch, entry.Dispatch) || !entry.Completed {
		return errors.New("completion binding mismatch")
	}
	journal.snapshot.Entries[index] = cloneEntry(entry)
	return nil
}

func (journal *fakeJournal) Revoke(_ context.Context, binding Binding) error {
	journal.mu.Lock()
	defer journal.mu.Unlock()
	journal.bindings = append(journal.bindings, cloneBinding(binding))
	journal.revokes++
	if journal.revokeErr != nil {
		return journal.revokeErr
	}
	if journal.snapshot.Binding == nil {
		stored := cloneBinding(binding)
		journal.snapshot.Binding = &stored
	}
	journal.snapshot.Revoked = true
	return nil
}

func (journal *fakeJournal) Snapshot() JournalSnapshot {
	journal.mu.Lock()
	defer journal.mu.Unlock()
	return cloneSnapshot(journal.snapshot)
}

type requestIDQueue struct {
	mu     sync.Mutex
	values []string
	index  int
}

func (queue *requestIDQueue) Next() string {
	queue.mu.Lock()
	defer queue.mu.Unlock()
	if queue.index >= len(queue.values) {
		return "ffffffff-ffff-4fff-8fff-ffffffffffff"
	}
	value := queue.values[queue.index]
	queue.index++
	return value
}

type relayFixture struct {
	policy     codingcontract.InferencePolicy
	binding    Binding
	clock      *fixtureClock
	journal    *fakeJournal
	requests   [][]byte
	normalized []json.RawMessage
	invalid    map[string]json.RawMessage
	ids        *requestIDQueue
}

func newRelayFixture(t *testing.T) relayFixture {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector relayPolicyVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	policy, err := codingcontract.ParseInferencePolicy(vector.Policy)
	if err != nil {
		t.Fatal(err)
	}
	requests := make([][]byte, len(vector.LockedRequests))
	requestIDs := []string{
		"55555555-5555-4555-8555-555555555555",
		"66666666-6666-4666-8666-666666666666",
	}
	for index, raw := range vector.LockedRequests {
		locked, err := codingcontract.ParseInferenceLockedRequest(raw, policy)
		if err != nil {
			t.Fatal(err)
		}
		miner := codingcontract.InferenceMinerRequest{
			Model: locked.Model, Messages: locked.Messages, Tools: locked.Tools,
			ToolChoice:          locked.ToolChoice,
			Reasoning:           codingcontract.InferenceMinerReasoning{Effort: locked.Reasoning.Effort},
			MaxCompletionTokens: locked.MaxCompletionTokens,
			ParallelToolCalls:   locked.ParallelToolCalls,
		}
		requests[index], err = json.Marshal(miner)
		if err != nil {
			t.Fatal(err)
		}
	}
	now := time.Now().UTC().Truncate(time.Second)
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	return relayFixture{
		policy: policy,
		binding: Binding{
			AttemptID:           "certification-synthetic-001",
			AgentArtifactSHA256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			HarnessInstanceID:   "harness-synthetic-001",
			TicketID:            "33333333-3333-4333-8333-333333333333", CaseID: "case-inference-001",
			ProfileCapabilityID: "profile-inference-001",
			GrantID:             "44444444-4444-4444-8444-444444444444", Generation: 1,
			InferenceGrantSHA256: digest, IssuedAt: now, Deadline: now.Add(time.Hour),
			RequestBudget: policy.MaxRequests, PromptTokenBudget: policy.MaxPromptTokens,
			CompletionTokenBudget: policy.MaxCompletionTokens, CostBudgetUSDMicros: policy.MaxCostUSDMicros,
		},
		clock: &fixtureClock{now: now}, journal: &fakeJournal{}, requests: requests,
		normalized: vector.NormalizedProviderResponses,
		invalid:    vector.InvalidProviderResponses,
		ids:        &requestIDQueue{values: requestIDs},
	}
}

func (fixture relayFixture) config(upstream Upstream) Config {
	return Config{
		Policy: fixture.policy, Binding: fixture.binding, Upstream: upstream,
		Journal: fixture.journal, Now: fixture.clock.Now,
		NewRequestID: fixture.ids.Next, OperationTimeout: time.Second,
	}
}

func (fixture relayFixture) evidenceBinding() EvidenceBinding {
	return EvidenceBinding{
		AttemptID:           fixture.binding.AttemptID,
		AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID:   fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256:  fixture.binding.InferenceGrantSHA256,
		Deadline:              fixture.binding.Deadline,
		RequestBudget:         fixture.binding.RequestBudget,
		PromptTokenBudget:     fixture.binding.PromptTokenBudget,
		CompletionTokenBudget: fixture.binding.CompletionTokenBudget,
		CostBudgetUSDMicros:   fixture.binding.CostBudgetUSDMicros,
	}
}

func (fixture relayFixture) completeResult(
	t *testing.T,
	request UpstreamRequest,
	normalizedIndex int,
) UpstreamResult {
	t.Helper()
	normalized, err := codingcontract.ParseInferenceNormalizedResponse(
		fixture.normalized[normalizedIndex], fixture.policy,
	)
	if err != nil {
		t.Fatal(err)
	}
	responseSHA256, err := codingcontract.InferenceNormalizedResponseSHA256(fixture.policy, normalized)
	if err != nil {
		t.Fatal(err)
	}
	generationID := normalized.ID
	receiptProvider := fixture.policy.ReceiptProvider
	return UpstreamResult{
		NormalizedResponse: append([]byte(nil), fixture.normalized[normalizedIndex]...),
		Settlement: codingcontract.InferenceProviderSettlement{
			Schema:                codingcontract.InferenceProviderSettlementSchema,
			CodingContractVersion: codingcontract.ContractVersion,
			TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
			ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
			InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
			GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
			RequestID: request.RequestID, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
			LockedRequestSHA256: request.LockedRequestSHA256,
			Outcome:             codingcontract.InferenceReceiptComplete, HTTPStatus: 200,
			ResponseSHA256: &responseSHA256, ResponseDigestKind: "normalized_v1",
			ProviderGenerationID: &generationID, Model: fixture.policy.Model,
			ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
			ReceiptProvider: &receiptProvider, ProviderRouteProfile: fixture.policy.ProviderRouteProfile,
			ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
			ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
			ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
			RouterMetadataVerified:   true,
			RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: receiptProvider, Selected: true}},
			PipelineStages:           []string{}, UsageAvailable: true,
			PromptTokens:     normalized.Usage.PromptTokens,
			CompletionTokens: normalized.Usage.CompletionTokens,
			TotalTokens:      normalized.Usage.TotalTokens,
			CostAvailable:    true, CostUSDMicros: normalized.Usage.CostUSDMicros,
		},
	}
}

func (fixture relayFixture) customCompleteResult(
	t *testing.T,
	request UpstreamRequest,
	promptTokens, completionTokens, costUSDMicros uint64,
) UpstreamResult {
	t.Helper()
	content := "Synthetic bounded completion."
	normalized := codingcontract.InferenceNormalizedResponse{
		Schema: codingcontract.InferenceResponseSchema,
		ID:     fmt.Sprintf("generation-custom-%03d", request.RequestSequence), Model: fixture.policy.Model,
		Provider: fixture.policy.ReceiptProvider,
		Choices: []codingcontract.InferenceResponseChoice{{Message: codingcontract.InferenceResponseMessage{
			Content: &content, ToolCalls: []codingcontract.InferenceToolCall{},
		}}},
		Usage: codingcontract.InferenceNormalizedUsage{
			PromptTokens: promptTokens, CompletionTokens: completionTokens,
			TotalTokens: promptTokens + completionTokens, CostUSDMicros: costUSDMicros,
		},
	}
	body, err := json.Marshal(normalized)
	if err != nil {
		t.Fatal(err)
	}
	responseSHA256, err := codingcontract.InferenceNormalizedResponseSHA256(fixture.policy, normalized)
	if err != nil {
		t.Fatal(err)
	}
	generationID := normalized.ID
	receiptProvider := fixture.policy.ReceiptProvider
	return UpstreamResult{
		NormalizedResponse: body,
		Settlement: codingcontract.InferenceProviderSettlement{
			Schema:                codingcontract.InferenceProviderSettlementSchema,
			CodingContractVersion: codingcontract.ContractVersion,
			TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
			ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
			InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
			GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
			RequestID: request.RequestID, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
			LockedRequestSHA256: request.LockedRequestSHA256,
			Outcome:             codingcontract.InferenceReceiptComplete, HTTPStatus: 200,
			ResponseSHA256: &responseSHA256, ResponseDigestKind: "normalized_v1",
			ProviderGenerationID: &generationID, Model: fixture.policy.Model,
			ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
			ReceiptProvider: &receiptProvider, ProviderRouteProfile: fixture.policy.ProviderRouteProfile,
			ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
			ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
			ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
			RouterMetadataVerified:   true,
			RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: receiptProvider, Selected: true}},
			PipelineStages:           []string{}, UsageAvailable: true,
			PromptTokens: promptTokens, CompletionTokens: completionTokens,
			TotalTokens:   promptTokens + completionTokens,
			CostAvailable: true, CostUSDMicros: costUSDMicros,
		},
	}
}

func (fixture relayFixture) retryResult(request UpstreamRequest) UpstreamResult {
	failure := "pre_provider_unavailable"
	return UpstreamResult{Settlement: codingcontract.InferenceProviderSettlement{
		Schema:                codingcontract.InferenceProviderSettlementSchema,
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
		GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
		RequestID: request.RequestID, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
		LockedRequestSHA256: request.LockedRequestSHA256,
		Outcome:             codingcontract.InferenceReceiptFreeRetry, TerminalErrorCode: &failure,
		HTTPStatus: 503, ResponseDigestKind: "none", Model: fixture.policy.Model,
		ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
		ProviderRouteProfile:     fixture.policy.ProviderRouteProfile,
		ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
		ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
		ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
		RouterMetadataVerified:   true,
		RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: fixture.policy.ReceiptProvider}},
		PipelineStages:           []string{},
	}}
}

func (fixture relayFixture) failureResult(request UpstreamRequest) UpstreamResult {
	failure := "provider_timeout"
	receiptProvider := fixture.policy.ReceiptProvider
	return UpstreamResult{Settlement: codingcontract.InferenceProviderSettlement{
		Schema:                codingcontract.InferenceProviderSettlementSchema,
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
		GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
		RequestID: request.RequestID, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
		LockedRequestSHA256: request.LockedRequestSHA256,
		Outcome:             codingcontract.InferenceReceiptProviderFailed, TerminalErrorCode: &failure,
		HTTPStatus: 504, ResponseDigestKind: "none", Model: fixture.policy.Model,
		ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
		ReceiptProvider: &receiptProvider, ProviderRouteProfile: fixture.policy.ProviderRouteProfile,
		ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
		ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
		ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
		RouterMetadataVerified:   true,
		RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: receiptProvider, Selected: true}},
		PipelineStages:           []string{}, UsageAvailable: true, CostAvailable: true, TimedOut: true,
	}}
}

func (fixture relayFixture) invalidResponseResult(t *testing.T, request UpstreamRequest) UpstreamResult {
	t.Helper()
	projection := fixture.invalid["response_invalid"]
	responseSHA256, err := codingcontract.InferenceCanonicalResponseProjectionSHA256(fixture.policy, projection)
	if err != nil {
		t.Fatal(err)
	}
	failure := "provider_response_invalid"
	generationID := "generation-synthetic-invalid"
	receiptProvider := fixture.policy.ReceiptProvider
	return UpstreamResult{
		FailureResponseProjection: append([]byte(nil), projection...),
		Settlement: codingcontract.InferenceProviderSettlement{
			Schema:                codingcontract.InferenceProviderSettlementSchema,
			CodingContractVersion: codingcontract.ContractVersion,
			TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
			ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
			InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
			GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
			RequestID: request.RequestID, RequestSequence: request.RequestSequence, Attempt: request.Attempt,
			LockedRequestSHA256: request.LockedRequestSHA256,
			Outcome:             codingcontract.InferenceReceiptProviderFailed, TerminalErrorCode: &failure,
			HTTPStatus: 200, ResponseSHA256: &responseSHA256, ResponseDigestKind: "canonical_json_v1",
			ProviderGenerationID: &generationID, Model: fixture.policy.Model,
			ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
			ReceiptProvider: &receiptProvider, ProviderRouteProfile: fixture.policy.ProviderRouteProfile,
			ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
			ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
			ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
			RouterMetadataVerified:   true,
			RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: receiptProvider, Selected: true}},
			PipelineStages:           []string{}, UsageAvailable: true, CostAvailable: true,
		},
	}
}

func parseMinerResponse(t *testing.T, policy codingcontract.InferencePolicy, body []byte) codingcontract.InferenceMinerResponse {
	t.Helper()
	response, err := codingcontract.ParseInferenceMinerResponse(body, policy)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func assertBytesEqual(t *testing.T, left, right []byte) {
	t.Helper()
	if !bytes.Equal(left, right) {
		t.Fatalf("bytes differ\nleft:  %s\nright: %s", left, right)
	}
}
