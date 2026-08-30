package codingrelayjournal

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/sys/unix"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type journalVector struct {
	Policy                      json.RawMessage   `json:"policy"`
	LockedRequests              []json.RawMessage `json:"locked_requests"`
	NormalizedProviderResponses []json.RawMessage `json:"normalized_provider_responses"`
}

type journalFixture struct {
	policy         codingcontract.InferencePolicy
	binding        codingrelay.Binding
	minerBody      []byte
	dispatch       codingrelay.DispatchRecord
	normalizedBody []byte
	now            time.Time
}

type upstreamFunction func(context.Context, codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error)

func (function upstreamFunction) Complete(
	ctx context.Context,
	request codingrelay.UpstreamRequest,
) (codingrelay.UpstreamResult, error) {
	return function(ctx, request)
}

func newJournalFixture(t *testing.T) journalFixture {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector journalVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	policy, err := codingcontract.ParseInferencePolicy(vector.Policy)
	if err != nil {
		t.Fatal(err)
	}
	locked, err := codingcontract.ParseInferenceLockedRequest(vector.LockedRequests[0], policy)
	if err != nil {
		t.Fatal(err)
	}
	miner := codingcontract.InferenceMinerRequest{
		Model: locked.Model, Messages: locked.Messages, Tools: locked.Tools, ToolChoice: locked.ToolChoice,
		Reasoning:           codingcontract.InferenceMinerReasoning{Effort: locked.Reasoning.Effort},
		MaxCompletionTokens: locked.MaxCompletionTokens, ParallelToolCalls: locked.ParallelToolCalls,
	}
	minerBody, err := json.Marshal(miner)
	if err != nil {
		t.Fatal(err)
	}
	minerDigest, err := codingcontract.InferenceMinerRequestSHA256(policy, miner)
	if err != nil {
		t.Fatal(err)
	}
	lockedDigest, err := codingcontract.InferenceLockedRequestSHA256(policy, locked)
	if err != nil {
		t.Fatal(err)
	}
	policyDigest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 22, 10, 0, 0, 123_456_789, time.UTC)
	return journalFixture{
		policy: policy,
		binding: codingrelay.Binding{
			AttemptID: "shadow-attempt-journal-001", AgentArtifactSHA256: strings.Repeat("a", 64),
			HarnessInstanceID: "harness-journal-001", TicketID: "33333333-3333-4333-8333-333333333333",
			CaseID: "case-journal-001", ProfileCapabilityID: "profile-journal-001",
			GrantID: "44444444-4444-4444-8444-444444444444", Generation: 1,
			InferenceGrantSHA256: policyDigest, IssuedAt: now, Deadline: now.Add(time.Hour),
			RequestBudget: policy.MaxRequests, PromptTokenBudget: policy.MaxPromptTokens,
			CompletionTokenBudget: policy.MaxCompletionTokens,
		},
		minerBody: minerBody,
		dispatch: codingrelay.DispatchRecord{
			Sequence: 1, RequestSequence: 1, Attempt: 1,
			RequestID:          "55555555-5555-4555-8555-555555555555",
			MinerRequestSHA256: minerDigest, MinerRequest: miner,
			LockedRequestSHA256: lockedDigest, LockedRequest: locked,
		},
		normalizedBody: append([]byte(nil), vector.NormalizedProviderResponses[0]...),
		now:            now,
	}
}

func (fixture journalFixture) storeConfig(root string) Config {
	return Config{
		Root: root, Policy: fixture.policy, MaxTotalBytes: 256 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	}
}

func (fixture journalFixture) relayConfig(journal codingrelay.Journal, upstream codingrelay.Upstream) codingrelay.Config {
	return codingrelay.Config{
		Policy: fixture.policy, Binding: fixture.binding, Journal: journal, Upstream: upstream,
		Now: func() time.Time { return fixture.now },
		NewRequestID: func() string {
			return fixture.dispatch.RequestID
		},
		OperationTimeout: time.Second,
	}
}

func (fixture journalFixture) completeResult(
	t *testing.T,
	request codingrelay.UpstreamRequest,
) codingrelay.UpstreamResult {
	t.Helper()
	normalized, err := codingcontract.ParseInferenceNormalizedResponse(fixture.normalizedBody, fixture.policy)
	if err != nil {
		t.Fatal(err)
	}
	responseDigest, err := codingcontract.InferenceNormalizedResponseSHA256(fixture.policy, normalized)
	if err != nil {
		t.Fatal(err)
	}
	generationID := normalized.ID
	receiptProvider := fixture.policy.ReceiptProvider
	return codingrelay.UpstreamResult{
		NormalizedResponse: append([]byte(nil), fixture.normalizedBody...),
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
			ResponseSHA256: &responseDigest, ResponseDigestKind: "normalized_v1",
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

func (fixture journalFixture) retryResult(
	request codingrelay.UpstreamRequest,
) codingrelay.UpstreamResult {
	failure := "pre_provider_unavailable"
	return codingrelay.UpstreamResult{Settlement: codingcontract.InferenceProviderSettlement{
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

func (fixture journalFixture) failureResult(
	request codingrelay.UpstreamRequest,
) codingrelay.UpstreamResult {
	failure := "provider_timeout"
	receiptProvider := fixture.policy.ReceiptProvider
	return codingrelay.UpstreamResult{Settlement: codingcontract.InferenceProviderSettlement{
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

func makeJournalRoot(t *testing.T) string {
	t.Helper()
	root := filepath.Join(t.TempDir(), "relay-journal")
	if err := os.Mkdir(root, 0o700); err != nil {
		t.Fatal(err)
	}
	return root
}

func completedJournalEntry(t *testing.T, fixture journalFixture) codingrelay.JournalEntry {
	t.Helper()
	root := makeJournalRoot(t)
	store, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	upstream := upstreamFunction(func(_ context.Context, request codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
		return fixture.completeResult(t, request), nil
	})
	relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.minerBody); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Load(t.Context(), fixture.binding)
	if err != nil || len(snapshot.Entries) != 1 || !snapshot.Entries[0].Completed {
		t.Fatalf("completed fixture snapshot=%#v err=%v", snapshot, err)
	}
	return snapshot.Entries[0]
}

func TestStoreCompleteWithoutBeginFailsClosed(t *testing.T) {
	fixture := newJournalFixture(t)
	store, err := Open(fixture.storeConfig(makeJournalRoot(t)))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Complete(t.Context(), fixture.binding, codingrelay.JournalEntry{Completed: true}); !errors.Is(err, ErrState) {
		t.Fatalf("empty complete err=%v", err)
	}
}

func TestStoreSurvivesRestartAndReplaysExactMinerResponse(t *testing.T) {
	fixture := newJournalFixture(t)
	root := makeJournalRoot(t)
	store, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	var upstreamCalls atomic.Int32
	upstream := upstreamFunction(func(_ context.Context, request codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
		upstreamCalls.Add(1)
		return fixture.completeResult(t, request), nil
	})
	relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
	if err != nil {
		t.Fatal(err)
	}
	firstResponse, err := relay.Complete(t.Context(), fixture.minerBody)
	if err != nil {
		t.Fatal(err)
	}
	if upstreamCalls.Load() != 1 {
		t.Fatalf("upstream calls=%d", upstreamCalls.Load())
	}
	snapshot, err := store.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Binding == nil || snapshot.Revoked || len(snapshot.Entries) != 1 ||
		!snapshot.Entries[0].Completed || len(snapshot.Entries[0].MinerResponse) == 0 {
		t.Fatalf("snapshot=%#v", snapshot)
	}
	if err := store.Complete(t.Context(), fixture.binding, snapshot.Entries[0]); err != nil {
		t.Fatalf("exact completion replay: %v", err)
	}
	snapshot.Binding.CaseID = "mutated"
	snapshot.Entries[0].MinerResponse[0] ^= 0xff
	again, err := store.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if again.Binding.CaseID != fixture.binding.CaseID || bytes.Equal(snapshot.Entries[0].MinerResponse, again.Entries[0].MinerResponse) {
		t.Fatal("returned snapshot aliased durable state")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	upstreamCalls.Store(0)
	restored, err := codingrelay.New(t.Context(), fixture.relayConfig(reopened, upstream))
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := restored.Complete(t.Context(), fixture.minerBody)
	if err != nil {
		t.Fatal(err)
	}
	if upstreamCalls.Load() != 0 || !bytes.Equal(firstResponse, replayed) {
		t.Fatalf("response-loss replay dispatched=%d equal=%v", upstreamCalls.Load(), bytes.Equal(firstResponse, replayed))
	}
	if err := restored.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	evidence, err := restored.Evidence(t.Context(), codingrelay.EvidenceBinding{
		AttemptID: fixture.binding.AttemptID, AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		HarnessInstanceID: fixture.binding.HarnessInstanceID, TicketID: fixture.binding.TicketID,
		CaseID: fixture.binding.CaseID, ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256, Deadline: fixture.binding.Deadline,
		RequestBudget: fixture.binding.RequestBudget, PromptTokenBudget: fixture.binding.PromptTokenBudget,
		CompletionTokenBudget: fixture.binding.CompletionTokenBudget,
	})
	if err != nil || evidence.UsageStatus != codingcontract.ModelUsageComplete {
		t.Fatalf("evidence=%#v err=%v", evidence, err)
	}
	if err := restored.Revoke(t.Context()); err != nil {
		t.Fatalf("exact revoke replay: %v", err)
	}
	for _, name := range []string{"state.json", filepath.Join("entries", "00000001.json")} {
		info, err := os.Stat(filepath.Join(root, name))
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode=%v err=%v", name, info.Mode().Perm(), err)
		}
	}
}

func TestStoreRestoresCompletionCappedMinerRequest(t *testing.T) {
	fixture := newJournalFixture(t)
	fixture.binding.CompletionTokenBudget = 300
	root := makeJournalRoot(t)
	store, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	var upstreamCalls atomic.Int32
	upstream := upstreamFunction(func(_ context.Context, request codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
		upstreamCalls.Add(1)
		if request.LockedRequest.MaxCompletionTokens != 300 {
			t.Fatalf("locked completion tokens=%d", request.LockedRequest.MaxCompletionTokens)
		}
		return fixture.completeResult(t, request), nil
	})
	relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
	if err != nil {
		t.Fatal(err)
	}
	first, err := relay.Complete(t.Context(), fixture.minerBody)
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Load(t.Context(), fixture.binding)
	if err != nil || len(snapshot.Entries) != 1 {
		t.Fatalf("snapshot=%#v err=%v", snapshot, err)
	}
	if snapshot.Entries[0].Dispatch.MinerRequest.MaxCompletionTokens ==
		snapshot.Entries[0].Dispatch.LockedRequest.MaxCompletionTokens {
		t.Fatal("journal lost the original miner request before applying the effective cap")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	upstreamCalls.Store(0)
	restored, err := codingrelay.New(t.Context(), fixture.relayConfig(reopened, upstream))
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := restored.Complete(t.Context(), fixture.minerBody)
	if err != nil {
		t.Fatal(err)
	}
	if upstreamCalls.Load() != 0 || !bytes.Equal(first, replayed) {
		t.Fatalf("capped replay dispatched=%d equal=%v", upstreamCalls.Load(), bytes.Equal(first, replayed))
	}
}

func TestIncompleteDispatchIsDurableAndNeverRerunnable(t *testing.T) {
	fixture := newJournalFixture(t)
	root := makeJournalRoot(t)
	store, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
		t.Fatal(err)
	}
	if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
		t.Fatalf("exact begin replay: %v", err)
	}
	conflict := fixture.dispatch
	conflict.RequestID = "66666666-6666-4666-8666-666666666666"
	if err := store.Begin(t.Context(), fixture.binding, conflict); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting begin err=%v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var calls atomic.Int32
	upstream := upstreamFunction(func(context.Context, codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
		calls.Add(1)
		return codingrelay.UpstreamResult{}, errors.New("must not dispatch")
	})
	if _, err := codingrelay.New(t.Context(), fixture.relayConfig(reopened, upstream)); !errors.Is(err, codingrelay.ErrAmbiguousDispatch) || calls.Load() != 0 {
		t.Fatalf("restart err=%v calls=%d", err, calls.Load())
	}
}

func TestStorePersistsRetryFailureAndNotInvokedStates(t *testing.T) {
	fixture := newJournalFixture(t)
	t.Run("receipt-free retry then completion", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		upstream := upstreamFunction(func(_ context.Context, request codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
			if request.Attempt == 1 {
				return fixture.retryResult(request), nil
			}
			return fixture.completeResult(t, request), nil
		})
		relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.minerBody); err != nil {
			t.Fatal(err)
		}
		snapshot, err := store.Load(t.Context(), fixture.binding)
		if err != nil || len(snapshot.Entries) != 2 ||
			snapshot.Entries[0].Receipt.Outcome != codingcontract.InferenceReceiptFreeRetry ||
			snapshot.Entries[1].Receipt.Outcome != codingcontract.InferenceReceiptComplete {
			t.Fatalf("retry snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("provider failure", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		upstream := upstreamFunction(func(_ context.Context, request codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
			return fixture.failureResult(request), nil
		})
		relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.minerBody); !errors.Is(err, codingrelay.ErrProviderFailure) {
			t.Fatalf("provider failure err=%v", err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		snapshot, err := store.Load(t.Context(), fixture.binding)
		if err != nil || !snapshot.Revoked || len(snapshot.Entries) != 1 ||
			snapshot.Entries[0].Receipt.Outcome != codingcontract.InferenceReceiptProviderFailed {
			t.Fatalf("failure snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("not invoked", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		upstream := upstreamFunction(func(context.Context, codingrelay.UpstreamRequest) (codingrelay.UpstreamResult, error) {
			return codingrelay.UpstreamResult{}, errors.New("not invoked")
		})
		relay, err := codingrelay.New(t.Context(), fixture.relayConfig(store, upstream))
		if err != nil {
			t.Fatal(err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		evidence, err := relay.Evidence(t.Context(), codingrelay.EvidenceBinding{
			AttemptID: fixture.binding.AttemptID, AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
			HarnessInstanceID: fixture.binding.HarnessInstanceID, TicketID: fixture.binding.TicketID,
			CaseID: fixture.binding.CaseID, ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
			InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256, Deadline: fixture.binding.Deadline,
			RequestBudget: fixture.binding.RequestBudget, PromptTokenBudget: fixture.binding.PromptTokenBudget,
			CompletionTokenBudget: fixture.binding.CompletionTokenBudget,
		})
		if err != nil || evidence.UsageStatus != codingcontract.ModelUsageNotInvoked {
			t.Fatalf("not-invoked evidence=%#v err=%v", evidence, err)
		}
	})
}

func TestStoreBindingLockCapacityAndConcurrency(t *testing.T) {
	fixture := newJournalFixture(t)
	t.Run("process lock and binding", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrLocked) {
			t.Fatalf("second owner err=%v", err)
		}
		if err := store.Revoke(t.Context(), fixture.binding); err != nil {
			t.Fatal(err)
		}
		foreign := fixture.binding
		foreign.CaseID = "case-journal-foreign"
		if _, err := store.Load(t.Context(), foreign); !errors.Is(err, ErrConflict) {
			t.Fatalf("foreign binding err=%v", err)
		}
	})

	t.Run("completion reservation", func(t *testing.T) {
		root := makeJournalRoot(t)
		config := fixture.storeConfig(root)
		config.MaxTotalBytes = 1 << 20
		store, err := Open(config)
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		if err := store.Revoke(t.Context(), fixture.binding); err != nil {
			t.Fatalf("small durable revoke: %v", err)
		}
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); !errors.Is(err, ErrState) {
			t.Fatalf("revoked begin err=%v", err)
		}

		secondRoot := makeJournalRoot(t)
		config.Root = secondRoot
		other, err := Open(config)
		if err != nil {
			t.Fatal(err)
		}
		defer other.Close()
		if err := other.Begin(t.Context(), fixture.binding, fixture.dispatch); !errors.Is(err, ErrCapacity) {
			t.Fatalf("unreserved begin err=%v", err)
		}
		snapshot, err := other.Load(t.Context(), fixture.binding)
		if err != nil || snapshot.Binding != nil || len(snapshot.Entries) != 0 {
			t.Fatalf("capacity failure mutated journal: snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("one coherent concurrent winner", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		left := fixture.dispatch
		right := fixture.dispatch
		right.RequestID = "66666666-6666-4666-8666-666666666666"
		start := make(chan struct{})
		results := make(chan error, 2)
		var wait sync.WaitGroup
		for _, dispatch := range []codingrelay.DispatchRecord{left, right} {
			wait.Add(1)
			go func(value codingrelay.DispatchRecord) {
				defer wait.Done()
				<-start
				results <- store.Begin(t.Context(), fixture.binding, value)
			}(dispatch)
		}
		close(start)
		wait.Wait()
		close(results)
		var successes, conflicts int
		for err := range results {
			switch {
			case err == nil:
				successes++
			case errors.Is(err, ErrConflict):
				conflicts++
			default:
				t.Fatalf("concurrent begin err=%v", err)
			}
		}
		if successes != 1 || conflicts != 1 {
			t.Fatalf("successes=%d conflicts=%d", successes, conflicts)
		}
	})
}

func TestStoreValidatesEffectiveDispatchAndRecordGenerations(t *testing.T) {
	fixture := newJournalFixture(t)
	t.Run("locked request must reflect effective completion budget", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		binding := fixture.binding
		binding.CompletionTokenBudget = fixture.dispatch.LockedRequest.MaxCompletionTokens - 1
		if err := store.Begin(t.Context(), binding, fixture.dispatch); !errors.Is(err, ErrConflict) {
			t.Fatalf("uncapped locked request err=%v", err)
		}
		snapshot, err := store.Load(t.Context(), binding)
		if err != nil || snapshot.Binding != nil || len(snapshot.Entries) != 0 {
			t.Fatalf("invalid dispatch mutated journal: snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("state generation", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Revoke(t.Context(), fixture.binding); err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, "state.json")
		body, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		record, err := decodeStateRecord(body)
		if err != nil {
			t.Fatal(err)
		}
		record.Generation = 2
		record.Revoked = false
		body, err = stateRecordBytes(record)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, body, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("impossible state generation err=%v", err)
		}
	})

	t.Run("entry generation", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		entry := completedJournalEntry(t, fixture)
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
			t.Fatal(err)
		}
		if err := store.Complete(t.Context(), fixture.binding, entry); err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, "entries", "00000001.json")
		body, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		record, err := decodeEntryRecord(body, fixture.policy, fixture.binding)
		if err != nil {
			t.Fatal(err)
		}
		record.Generation = 1
		body, err = entryRecordBytes(record)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, body, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("impossible entry generation err=%v", err)
		}
	})
}

func TestStoreRetriesAmbiguousDirectorySyncWithoutLosingCommit(t *testing.T) {
	fixture := newJournalFixture(t)
	t.Run("dispatch", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		var calls atomic.Int32
		store.fsync = func(fd int) error {
			if calls.Add(1) == 4 {
				return errors.New("synthetic directory sync failure")
			}
			return unix.Fsync(fd)
		}
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err == nil || !store.syncPending {
			t.Fatalf("ambiguous begin err=%v sync_pending=%v", err, store.syncPending)
		}
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil || store.syncPending {
			t.Fatalf("exact begin retry err=%v sync_pending=%v", err, store.syncPending)
		}
		snapshot, err := store.Load(t.Context(), fixture.binding)
		if err != nil || len(snapshot.Entries) != 1 || snapshot.Entries[0].Completed {
			t.Fatalf("dispatch snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("completion", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
			t.Fatal(err)
		}
		entry := completedJournalEntry(t, fixture)
		var calls atomic.Int32
		store.fsync = func(fd int) error {
			if calls.Add(1) == 1 {
				return errors.New("synthetic directory sync failure")
			}
			return unix.Fsync(fd)
		}
		if err := store.Complete(t.Context(), fixture.binding, entry); err == nil || !store.syncPending {
			t.Fatalf("ambiguous completion err=%v sync_pending=%v", err, store.syncPending)
		}
		if err := store.Complete(t.Context(), fixture.binding, entry); err != nil || store.syncPending {
			t.Fatalf("exact completion retry err=%v sync_pending=%v", err, store.syncPending)
		}
		snapshot, err := store.Load(t.Context(), fixture.binding)
		if err != nil || len(snapshot.Entries) != 1 || !snapshot.Entries[0].Completed {
			t.Fatalf("completion snapshot=%#v err=%v", snapshot, err)
		}
	})

	t.Run("close replays durability failure", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		store.syncPending = true
		store.fsync = func(int) error { return errors.New("synthetic directory sync failure") }
		first := store.Close()
		second := store.Close()
		if first == nil || second == nil || first.Error() != second.Error() {
			t.Fatalf("close errors first=%v second=%v", first, second)
		}
	})
}

func TestStoreFilesystemAndRecordIntegrityFailClosed(t *testing.T) {
	fixture := newJournalFixture(t)
	t.Run("root permissions", func(t *testing.T) {
		root := filepath.Join(t.TempDir(), "unsafe")
		if err := os.Mkdir(root, 0o755); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrInvalid) {
			t.Fatalf("unsafe root err=%v", err)
		}
	})

	t.Run("symlink root", func(t *testing.T) {
		parent := t.TempDir()
		target := filepath.Join(parent, "target")
		if err := os.Mkdir(target, 0o700); err != nil {
			t.Fatal(err)
		}
		link := filepath.Join(parent, "link")
		if err := os.Symlink(target, link); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(link)); err == nil {
			t.Fatal("symlink root was accepted")
		}
	})

	t.Run("record symlink and fifo", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		sentinel := filepath.Join(filepath.Dir(root), "sentinel")
		if err := os.WriteFile(sentinel, []byte("unchanged"), 0o600); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(sentinel, filepath.Join(root, "state.json")); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("state symlink err=%v", err)
		}
		body, err := os.ReadFile(sentinel)
		if err != nil || string(body) != "unchanged" {
			t.Fatalf("outside sentinel changed: %q err=%v", body, err)
		}
		if err := os.Remove(filepath.Join(root, "state.json")); err != nil {
			t.Fatal(err)
		}
		if err := unix.Mkfifo(filepath.Join(root, "entries", "00000001.json"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("fifo entry err=%v", err)
		}
	})

	t.Run("unknown record field", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Revoke(t.Context(), fixture.binding); err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(root, "state.json")
		body, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		body = bytes.Replace(body, []byte("{"), []byte(`{"future_authority":"forbidden",`), 1)
		if err := os.WriteFile(path, body, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("unknown field err=%v", err)
		}
	})

	t.Run("entry checksum and sequence", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		entry := filepath.Join(root, "entries", "00000001.json")
		body, err := os.ReadFile(entry)
		if err != nil {
			t.Fatal(err)
		}
		body[len(body)/2] ^= 1
		if err := os.WriteFile(entry, body, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("entry checksum err=%v", err)
		}

		secondRoot := makeJournalRoot(t)
		second, err := Open(fixture.storeConfig(secondRoot))
		if err != nil {
			t.Fatal(err)
		}
		if err := second.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
			t.Fatal(err)
		}
		if err := second.Close(); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(
			filepath.Join(secondRoot, "entries", "00000001.json"),
			filepath.Join(secondRoot, "entries", "00000002.json"),
		); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(secondRoot)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("sequence gap err=%v", err)
		}
	})

	t.Run("hardlink and mode", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Revoke(t.Context(), fixture.binding); err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		state := filepath.Join(root, "state.json")
		other := filepath.Join(filepath.Dir(root), "state-hardlink")
		if err := os.Link(state, other); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("hardlinked state err=%v", err)
		}
		if err := os.Remove(other); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(state, 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := Open(fixture.storeConfig(root)); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("unsafe state mode err=%v", err)
		}
	})

	t.Run("abandoned stage is removed", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		if err := store.Close(); err != nil {
			t.Fatal(err)
		}
		stage := filepath.Join(root, ".staging", "stage-"+strings.Repeat("a", 32))
		if err := os.WriteFile(stage, []byte("orphan"), 0o600); err != nil {
			t.Fatal(err)
		}
		reopened, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer reopened.Close()
		if _, err := os.Stat(stage); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("abandoned stage remains: %v", err)
		}
	})

	t.Run("root replacement is detected", func(t *testing.T) {
		root := makeJournalRoot(t)
		store, err := Open(fixture.storeConfig(root))
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		moved := root + "-moved"
		if err := os.Rename(root, moved); err != nil {
			t.Fatal(err)
		}
		if err := os.Mkdir(root, 0o700); err != nil {
			t.Fatal(err)
		}
		if _, err := store.Load(t.Context(), fixture.binding); !errors.Is(err, ErrCorrupt) {
			t.Fatalf("replacement root err=%v", err)
		}
	})
}

func TestStoreRejectsCallerMutationAndSafeDiagnostics(t *testing.T) {
	fixture := newJournalFixture(t)
	root := makeJournalRoot(t)
	store, err := Open(fixture.storeConfig(root))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	dispatch := fixture.dispatch
	if err := store.Begin(t.Context(), fixture.binding, dispatch); err != nil {
		t.Fatal(err)
	}
	if err := store.Revoke(t.Context(), fixture.binding); err != nil {
		t.Fatal(err)
	}
	if err := store.Begin(t.Context(), fixture.binding, fixture.dispatch); err != nil {
		t.Fatalf("exact begin replay after revoke: %v", err)
	}
	dispatch.LockedRequest.Messages[0][0] ^= 0xff
	snapshot, err := store.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(snapshot.Entries[0].Dispatch.LockedRequest.Messages, dispatch.LockedRequest.Messages) {
		t.Fatal("caller mutation aliased stored dispatch")
	}
	encoded, err := json.Marshal(store)
	if err == nil || encoded != nil {
		t.Fatalf("store serialized: %s err=%v", encoded, err)
	}
	for _, text := range []string{store.String(), fmt.Sprintf("%#v", store)} {
		if strings.Contains(text, fixture.binding.TicketID) || strings.Contains(text, fixture.binding.CaseID) ||
			strings.Contains(text, string(fixture.minerBody)) {
			t.Fatalf("diagnostic leaked private authority: %s", text)
		}
	}
	canceled, cancel := context.WithCancel(t.Context())
	cancel()
	if err := store.Begin(canceled, fixture.binding, fixture.dispatch); !errors.Is(err, ErrInvalid) {
		t.Fatalf("canceled begin err=%v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(t.Context(), fixture.binding); !errors.Is(err, ErrClosed) {
		t.Fatalf("closed load err=%v", err)
	}
	var nilStore *Store
	if err := nilStore.Begin(t.Context(), fixture.binding, fixture.dispatch); !errors.Is(err, ErrInvalid) {
		t.Fatalf("nil store begin err=%v", err)
	}
	if err := nilStore.Close(); err != nil {
		t.Fatalf("nil close err=%v", err)
	}
}
