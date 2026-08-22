package codingrelay

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func TestRelayCompletesTwoTurnsReplaysAndFinalizesEvidence(t *testing.T) {
	fixture := newRelayFixture(t)
	var calls atomic.Int32
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		index := int(calls.Add(1)) - 1
		return fixture.completeResult(t, request, index), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Evidence(t.Context(), fixture.evidenceBinding()); !errors.Is(err, ErrNotRevoked) {
		t.Fatalf("evidence before revoke err=%v", err)
	}

	first, err := relay.Complete(t.Context(), fixture.requests[0])
	if err != nil {
		t.Fatal(err)
	}
	firstResponse := parseMinerResponse(t, fixture.policy, first)
	if firstResponse.ID != "generation-synthetic-001" || len(firstResponse.Choices[0].Message.ToolCalls) != 1 {
		t.Fatalf("first response=%#v", firstResponse)
	}
	var public map[string]any
	if err := json.Unmarshal(first, &public); err != nil {
		t.Fatal(err)
	}
	if _, leaked := public["provider"]; leaked {
		t.Fatal("miner response leaked provider identity")
	}
	usage := public["usage"].(map[string]any)
	if _, leaked := usage["cost_usd_micros"]; leaked {
		t.Fatal("miner response leaked trusted cost")
	}

	replayed, err := relay.Complete(t.Context(), fixture.requests[0])
	if err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, replayed, first)
	if calls.Load() != 1 {
		t.Fatalf("replay made %d provider calls", calls.Load())
	}

	second, err := relay.Complete(t.Context(), fixture.requests[1])
	if err != nil {
		t.Fatal(err)
	}
	secondResponse := parseMinerResponse(t, fixture.policy, second)
	if secondResponse.ID != "generation-synthetic-002" ||
		secondResponse.Choices[0].Message.Content == nil ||
		*secondResponse.Choices[0].Message.Content != "Applied the parser repair." {
		t.Fatalf("second response=%#v", secondResponse)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if evidence.UsageStatus != codingcontract.ModelUsageComplete || evidence.Requests != 2 ||
		evidence.PromptTokens != 2500 || evidence.CompletionTokens != 320 ||
		evidence.CostUSDMicros != 1690 || evidence.RetryCount != 0 ||
		evidence.ProviderReceiptSetSHA256 == nil {
		t.Fatalf("evidence=%#v", evidence)
	}
	relay.mu.Lock()
	retained := cloneSnapshot(JournalSnapshot{Entries: relay.entries}).Entries
	relay.mu.Unlock()
	if len(retained) != 2 || len(retained[0].Dispatch.MinerRequest.Messages) != 0 ||
		len(retained[1].Dispatch.MinerRequest.Messages) != 0 ||
		len(retained[0].Dispatch.LockedRequest.Messages) != 0 ||
		len(retained[1].Dispatch.LockedRequest.Messages) != 0 ||
		len(retained[0].NormalizedResponse) != 0 || len(retained[1].NormalizedResponse) != 0 ||
		len(retained[0].MinerResponse) != 0 || len(retained[1].MinerResponse) == 0 {
		t.Fatalf("live retention=%#v", retained)
	}
	*evidence.ProviderReceiptSetSHA256 = strings.Repeat("f", 64)
	again, err := relay.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil || again.ProviderReceiptSetSHA256 == nil ||
		*again.ProviderReceiptSetSHA256 == strings.Repeat("f", 64) {
		t.Fatalf("evidence alias or error: %#v %v", again, err)
	}
	snapshot := fixture.journal.Snapshot()
	if len(snapshot.Entries) != 2 || fixture.journal.begins != 2 ||
		fixture.journal.completes != 2 || fixture.journal.revokes != 1 {
		t.Fatalf("snapshot=%#v journal=%#v", snapshot, fixture.journal)
	}
	for _, entry := range snapshot.Entries {
		if len(entry.NormalizedResponse) == 0 || len(entry.MinerResponse) == 0 {
			t.Fatal("complete journal entry omitted replay material")
		}
	}
}

func TestRelayRestartRecoversExactResponseAndRevokedEvidence(t *testing.T) {
	fixture := newRelayFixture(t)
	var calls atomic.Int32
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		calls.Add(1)
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	want, err := relay.Complete(t.Context(), fixture.requests[0])
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	got, err := restarted.Complete(t.Context(), fixture.requests[0])
	if err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, got, want)
	if calls.Load() != 1 {
		t.Fatal("restart replay called provider")
	}
	if err := restarted.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	final, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := final.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("revoked restart accepted request: %v", err)
	}
	if _, err := final.Evidence(t.Context(), fixture.evidenceBinding()); err != nil {
		t.Fatal(err)
	}
}

func TestRelayPersistsNotInvokedAndCertifierAdapterChecksSourceBinding(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		t.Fatal("not-invoked relay called upstream")
		return UpstreamResult{}, nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil || evidence.UsageStatus != codingcontract.ModelUsageNotInvoked ||
		evidence.ProviderReceiptSetSHA256 != nil {
		t.Fatalf("evidence=%#v err=%v", evidence, err)
	}
	adapter, err := NewCertifierEvidenceAdapter(relay)
	if err != nil {
		t.Fatal(err)
	}
	certifierBinding := codingcertifierBinding(fixture)
	if got, err := adapter.Evidence(t.Context(), certifierBinding); err != nil ||
		got.UsageStatus != codingcontract.ModelUsageNotInvoked {
		t.Fatalf("adapter evidence=%#v err=%v", got, err)
	}
	certifierBinding.Deadline = certifierBinding.Deadline.Add(time.Second)
	if _, err := adapter.Evidence(t.Context(), certifierBinding); !errors.Is(err, ErrEvidenceBinding) {
		t.Fatalf("deadline drift err=%v", err)
	}
	if _, err := (*CertifierEvidenceAdapter)(nil).Evidence(t.Context(), certifierBinding); !errors.Is(err, ErrEvidenceUnavailable) {
		t.Fatalf("nil adapter err=%v", err)
	}
}

func codingcertifierBinding(fixture relayFixture) codingcertifier.InferenceBinding {
	return codingcertifier.InferenceBinding{
		CertificationID:     fixture.binding.AttemptID,
		AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		HarnessInstanceID:   fixture.binding.HarnessInstanceID,
		TicketID:            fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
		ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
		Deadline:             fixture.binding.Deadline,
		Budgets: codingcontract.Budgets{
			ModelInputTokens:   fixture.binding.PromptTokenBudget,
			ModelOutputTokens:  fixture.binding.CompletionTokenBudget,
			WorkspaceToolCalls: fixture.binding.RequestBudget - codingcontract.InferenceFinalizationTurnSlack,
			WallTimeSeconds:    60,
		},
		RequestBudget: fixture.binding.RequestBudget,
	}
}

func TestRelayReceiptFreeRetryAndProviderFailureRemainDistinct(t *testing.T) {
	t.Run("retry then complete", func(t *testing.T) {
		fixture := newRelayFixture(t)
		var calls atomic.Int32
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			if calls.Add(1) == 1 {
				return fixture.retryResult(request), nil
			}
			return fixture.completeResult(t, request, 0), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
			t.Fatal(err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding())
		if err != nil || evidence.UsageStatus != codingcontract.ModelUsageComplete ||
			evidence.Requests != 1 || evidence.RetryCount != 1 || calls.Load() != 2 {
			t.Fatalf("evidence=%#v calls=%d err=%v", evidence, calls.Load(), err)
		}
		snapshot := fixture.journal.Snapshot()
		if len(snapshot.Entries) != 2 || snapshot.Entries[0].Dispatch.RequestID != snapshot.Entries[1].Dispatch.RequestID ||
			snapshot.Entries[0].Dispatch.LockedRequestSHA256 != snapshot.Entries[1].Dispatch.LockedRequestSHA256 {
			t.Fatalf("retry journal=%#v", snapshot)
		}
	})

	t.Run("terminal provider failure", func(t *testing.T) {
		fixture := newRelayFixture(t)
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.failureResult(request), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrProviderFailure) {
			t.Fatalf("provider failure err=%v", err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrProviderFailure) {
			t.Fatalf("request after provider failure err=%v", err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding())
		if err != nil || evidence.UsageStatus != codingcontract.ModelUsageProviderFailure {
			t.Fatalf("evidence=%#v err=%v", evidence, err)
		}
	})

	t.Run("invalid provider response retains canonical projection", func(t *testing.T) {
		fixture := newRelayFixture(t)
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.invalidResponseResult(t, request), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrProviderFailure) {
			t.Fatalf("invalid response err=%v", err)
		}
		snapshot := fixture.journal.Snapshot()
		if len(snapshot.Entries) != 1 || len(snapshot.Entries[0].FailureResponseProjection) == 0 ||
			len(snapshot.Entries[0].NormalizedResponse) != 0 || len(snapshot.Entries[0].MinerResponse) != 0 {
			t.Fatalf("journal=%#v", snapshot)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		if evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding()); err != nil ||
			evidence.UsageStatus != codingcontract.ModelUsageProviderFailure {
			t.Fatalf("evidence=%#v err=%v", evidence, err)
		}
	})
}

func TestRelayDetachesAdmittedProviderAttemptFromClientCancellation(t *testing.T) {
	fixture := newRelayFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	upstream := upstreamFunc(func(ctx context.Context, request UpstreamRequest) (UpstreamResult, error) {
		close(started)
		select {
		case <-release:
			return fixture.completeResult(t, request, 0), nil
		case <-ctx.Done():
			return UpstreamResult{}, ctx.Err()
		}
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	clientContext, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)
	go func() {
		_, err := relay.Complete(clientContext, fixture.requests[0])
		result <- err
	}()
	<-started
	cancel()
	close(release)
	if err := <-result; err != nil {
		t.Fatalf("admitted attempt inherited client cancellation: %v", err)
	}
	if len(fixture.journal.Snapshot().Entries) != 1 {
		t.Fatal("detached provider settlement was not journaled")
	}
}

func TestRelaySerializesSameRequestAndRejectsDifferentConcurrentRequest(t *testing.T) {
	fixture := newRelayFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		if calls.Add(1) == 1 {
			close(started)
			<-release
		}
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	first := make(chan []byte, 1)
	firstErr := make(chan error, 1)
	go func() {
		body, err := relay.Complete(t.Context(), fixture.requests[0])
		first <- body
		firstErr <- err
	}()
	<-started
	if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrConcurrentRequest) {
		t.Fatalf("different concurrent request err=%v", err)
	}
	same := make(chan []byte, 1)
	sameErr := make(chan error, 1)
	go func() {
		body, err := relay.Complete(t.Context(), fixture.requests[0])
		same <- body
		sameErr <- err
	}()
	close(release)
	firstBody, sameBody := <-first, <-same
	if err := <-firstErr; err != nil {
		t.Fatal(err)
	}
	if err := <-sameErr; err != nil {
		t.Fatal(err)
	}
	assertBytesEqual(t, firstBody, sameBody)
	if calls.Load() != 1 {
		t.Fatalf("same request made %d upstream calls", calls.Load())
	}
}

func TestRevokeWaitsForFlightAndClosesAdmission(t *testing.T) {
	fixture := newRelayFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		close(started)
		<-release
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	completeErr := make(chan error, 1)
	go func() {
		_, err := relay.Complete(t.Context(), fixture.requests[0])
		completeErr <- err
	}()
	<-started
	revokeErr := make(chan error, 1)
	go func() { revokeErr <- relay.Revoke(t.Context()) }()
	if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("revoking relay admitted request: %v", err)
	}
	close(release)
	if err := <-completeErr; err != nil {
		t.Fatal(err)
	}
	if err := <-revokeErr; err != nil {
		t.Fatal(err)
	}
	if !fixture.journal.Snapshot().Revoked {
		t.Fatal("revoke was not durable")
	}
}

func TestCanceledRevokeRemainsClosedAndCanBeRetried(t *testing.T) {
	fixture := newRelayFixture(t)
	started := make(chan struct{})
	release := make(chan struct{})
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		close(started)
		<-release
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	completeErr := make(chan error, 1)
	go func() {
		_, err := relay.Complete(t.Context(), fixture.requests[0])
		completeErr <- err
	}()
	<-started
	revokeContext, cancel := context.WithCancel(t.Context())
	cancel()
	if err := relay.Revoke(revokeContext); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled revoke err=%v", err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("canceled revoke reopened admission: %v", err)
	}
	close(release)
	if err := <-completeErr; err != nil {
		t.Fatal(err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if !fixture.journal.Snapshot().Revoked {
		t.Fatal("retried revoke was not durable")
	}
}

func TestRelayRejectsForeignEvidenceBindings(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New("unused")
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	base := fixture.evidenceBinding()
	tests := map[string]func(*EvidenceBinding){
		"attempt":    func(value *EvidenceBinding) { value.AttemptID = "different" },
		"artifact":   func(value *EvidenceBinding) { value.AgentArtifactSHA256 = strings.Repeat("b", 64) },
		"harness":    func(value *EvidenceBinding) { value.HarnessInstanceID = "different" },
		"ticket":     func(value *EvidenceBinding) { value.TicketID = "77777777-7777-4777-8777-777777777777" },
		"case":       func(value *EvidenceBinding) { value.CaseID = "different" },
		"profile":    func(value *EvidenceBinding) { value.ProfileCapabilityID = "different" },
		"grant":      func(value *EvidenceBinding) { value.InferenceGrantSHA256 = strings.Repeat("b", 64) },
		"deadline":   func(value *EvidenceBinding) { value.Deadline = value.Deadline.Add(time.Second) },
		"requests":   func(value *EvidenceBinding) { value.RequestBudget-- },
		"prompt":     func(value *EvidenceBinding) { value.PromptTokenBudget-- },
		"completion": func(value *EvidenceBinding) { value.CompletionTokenBudget-- },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			changed := base
			mutate(&changed)
			if _, err := relay.Evidence(t.Context(), changed); !errors.Is(err, ErrEvidenceBinding) {
				t.Fatalf("foreign binding err=%v", err)
			}
		})
	}
}

func TestRelayDiagnosticsDoNotExposeLockedPromptsOrProviderMaterial(t *testing.T) {
	fixture := newRelayFixture(t)
	locked, err := codingcontract.ParseInferenceLockedRequest(
		mustLockedRequest(t, fixture.policy, fixture.requests[0]), fixture.policy,
	)
	if err != nil {
		t.Fatal(err)
	}
	request := UpstreamRequest{
		Sequence: 1, Attempt: 1, RequestID: "55555555-5555-4555-8555-555555555555",
		LockedRequestSHA256: strings.Repeat("a", 64), LockedRequest: locked,
	}
	if _, err := json.Marshal(request); err == nil {
		t.Fatal("upstream request serialized into diagnostics")
	}
	rendered := fmt.Sprintf("%#v", request)
	if strings.Contains(rendered, "Repair the parser") || strings.Contains(rendered, request.RequestID) {
		t.Fatalf("diagnostics leaked request: %s", rendered)
	}
	result := fixture.completeResult(t, request, 0)
	if _, err := json.Marshal(result); err == nil {
		t.Fatal("upstream result serialized into diagnostics")
	}
	if rendered := fmt.Sprintf("%#v", result); strings.Contains(rendered, "Azure") ||
		strings.Contains(rendered, "generation-synthetic") {
		t.Fatalf("diagnostics leaked settlement: %s", rendered)
	}
	entry := JournalEntry{
		Dispatch:  DispatchRecord{Sequence: 1, Attempt: 1, RequestID: request.RequestID, LockedRequest: locked},
		Completed: true, Settlement: result.Settlement,
		NormalizedResponse: result.NormalizedResponse,
	}
	for label, value := range map[string]any{
		"binding":  fixture.binding,
		"dispatch": entry.Dispatch,
		"entry":    entry,
		"snapshot": JournalSnapshot{Binding: &fixture.binding, Entries: []JournalEntry{entry}},
	} {
		rendered := fmt.Sprintf("%#v", value)
		if strings.Contains(rendered, request.RequestID) || strings.Contains(rendered, "Repair the parser") ||
			strings.Contains(rendered, "Azure") {
			t.Fatalf("%s diagnostics leaked private material: %s", label, rendered)
		}
	}
}

func mustLockedRequest(t *testing.T, policy codingcontract.InferencePolicy, minerBody []byte) []byte {
	t.Helper()
	miner, err := codingcontract.ParseInferenceMinerRequest(minerBody, policy)
	if err != nil {
		t.Fatal(err)
	}
	locked, err := codingcontract.LockInferenceRequest(policy, miner)
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(locked)
	if err != nil {
		t.Fatal(err)
	}
	return body
}
