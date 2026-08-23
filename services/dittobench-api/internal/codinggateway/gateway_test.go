package codinggateway

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelayjournal"
)

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (function roundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

type fakePublisher struct {
	mu          sync.Mutex
	url         string
	binding     CapabilityBinding
	handler     http.Handler
	publishErr  error
	typedNil    bool
	revokeErr   error
	closeErr    error
	publishCall int
	revokeCall  int
	closeCall   int
	events      *[]string
}

func (publisher *fakePublisher) Publish(
	_ context.Context,
	binding CapabilityBinding,
	handler http.Handler,
) (PublishedCapability, error) {
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	publisher.publishCall++
	publisher.binding = binding
	publisher.handler = handler
	if publisher.events != nil {
		*publisher.events = append(*publisher.events, "publish")
	}
	if publisher.publishErr != nil {
		return nil, publisher.publishErr
	}
	if publisher.typedNil {
		var capability *fakePublisher
		return capability, nil
	}
	return publisher, nil
}

func (publisher *fakePublisher) URL() string {
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	return publisher.url
}

func (publisher *fakePublisher) Revoke(_ context.Context) error {
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	publisher.revokeCall++
	if publisher.events != nil {
		*publisher.events = append(*publisher.events, "outer")
	}
	return publisher.revokeErr
}

func (publisher *fakePublisher) Close() error {
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	publisher.closeCall++
	return publisher.closeErr
}

type fakeAuthorizer struct {
	mu      sync.Mutex
	err     error
	calls   int
	binding CapabilityBinding
	events  *[]string
}

func (authorizer *fakeAuthorizer) Authorize(_ context.Context, binding CapabilityBinding) error {
	authorizer.mu.Lock()
	defer authorizer.mu.Unlock()
	authorizer.calls++
	authorizer.binding = binding
	if authorizer.events != nil {
		*authorizer.events = append(*authorizer.events, "authorize")
	}
	return authorizer.err
}

type fakeGrantRevoker struct {
	mu         sync.Mutex
	err        error
	failures   int
	calls      int
	revocation GrantRevocation
	events     *[]string
}

func (revoker *fakeGrantRevoker) Revoke(_ context.Context, revocation GrantRevocation) error {
	revoker.mu.Lock()
	defer revoker.mu.Unlock()
	revoker.calls++
	revoker.revocation = revocation
	if revoker.events != nil {
		*revoker.events = append(*revoker.events, "grant")
	}
	if revoker.failures > 0 {
		revoker.failures--
		return errors.New("injected revocation failure")
	}
	return revoker.err
}

type policyVector struct {
	Policy                      json.RawMessage   `json:"policy"`
	LockedRequests              []json.RawMessage `json:"locked_requests"`
	NormalizedProviderResponses []json.RawMessage `json:"normalized_provider_responses"`
}

type gatewayFixture struct {
	policy     codingcontract.InferencePolicy
	vector     policyVector
	binding    codingrelay.Binding
	capability codingplatform.GrantCapability
	now        time.Time
	root       string
	authorizer *fakeAuthorizer
	publisher  *fakePublisher
	revoker    *fakeGrantRevoker
}

func newGatewayFixture(t *testing.T) *gatewayFixture {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var vector policyVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	policy, err := codingcontract.ParseInferencePolicy(vector.Policy)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 23, 4, 0, 0, 123456000, time.UTC)
	privateKey := ed25519.NewKeyFromSeed(make([]byte, ed25519.SeedSize))
	publicKey := privateKey.Public().(ed25519.PublicKey)
	binding := codingrelay.Binding{
		AttemptID: "attempt-gateway-001", AgentArtifactSHA256: repeat("a", 64),
		HarnessInstanceID: "harness-gateway-001",
		TicketID:          "10000000-0000-4000-8000-000000000001",
		CaseID:            "case-gateway-001", ProfileCapabilityID: "profile-gateway-001",
		GrantID: "20000000-0000-4000-8000-000000000002", Generation: 2,
		InferenceGrantSHA256: digest, IssuedAt: now.Add(-time.Minute), Deadline: now.Add(time.Hour),
		RequestBudget: 32, PromptTokenBudget: 200_000, CompletionTokenBudget: 30_000,
	}
	root := filepath.Join(t.TempDir(), "relay")
	if err := os.Mkdir(root, 0o700); err != nil {
		t.Fatal(err)
	}
	publisher := &fakePublisher{url: "http://127.0.0.1:11436/capability/9e56f4e7", events: &[]string{}}
	authorizer := &fakeAuthorizer{}
	revoker := &fakeGrantRevoker{}
	return &gatewayFixture{
		policy: policy, vector: vector, binding: binding, now: now, root: root,
		authorizer: authorizer, publisher: publisher, revoker: revoker,
		capability: codingplatform.GrantCapability{
			Binding: binding, Bearer: "platform-coding-bearer-000000000000000000000000",
			BrokerPublicKey:  base64.RawURLEncoding.EncodeToString(publicKey),
			BrokerPrivateKey: append(ed25519.PrivateKey(nil), privateKey...),
			ProxyURL:         "https://relay.invalid/api/v1/inference/coding/chat/completions",
		},
	}
}

func (fixture *gatewayFixture) config() Config {
	return Config{
		Policy: fixture.policy, Capability: fixture.capability, JournalRoot: fixture.root,
		JournalMaxTotalBytes: 512 << 20,
		JournalMaxEntries:    int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
		Authorizer:           fixture.authorizer,
		Publisher:            fixture.publisher, GrantRevoker: fixture.revoker,
		Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) {
			panic("provider transport must not be called")
		}),
		Now: func() time.Time { return fixture.now }, CleanupTimeout: time.Second,
	}
}

func (fixture *gatewayFixture) recoveryConfig() RecoveryConfig {
	return RecoveryConfig{
		Policy: fixture.policy, Binding: fixture.binding, JournalRoot: fixture.root,
		JournalMaxTotalBytes: 512 << 20,
		JournalMaxEntries:    int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
		GrantRevoker:         fixture.revoker, Now: func() time.Time { return fixture.now },
		CleanupTimeout: time.Second,
	}
}

func (fixture *gatewayFixture) evidenceBinding() codingrelay.EvidenceBinding {
	return codingrelay.EvidenceBinding{
		AttemptID: fixture.binding.AttemptID, AgentArtifactSHA256: fixture.binding.AgentArtifactSHA256,
		HarnessInstanceID: fixture.binding.HarnessInstanceID, TicketID: fixture.binding.TicketID,
		CaseID: fixture.binding.CaseID, ProfileCapabilityID: fixture.binding.ProfileCapabilityID,
		InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
		Deadline:             fixture.binding.Deadline, RequestBudget: fixture.binding.RequestBudget,
		PromptTokenBudget:     fixture.binding.PromptTokenBudget,
		CompletionTokenBudget: fixture.binding.CompletionTokenBudget,
	}
}

func (fixture *gatewayFixture) incompleteDispatch(t *testing.T) codingrelay.DispatchRecord {
	t.Helper()
	locked, err := codingcontract.ParseInferenceLockedRequest(fixture.vector.LockedRequests[0], fixture.policy)
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
	if miner.MaxCompletionTokens > fixture.binding.CompletionTokenBudget {
		miner.MaxCompletionTokens = fixture.binding.CompletionTokenBudget
	}
	locked, err = codingcontract.LockInferenceRequest(fixture.policy, miner)
	if err != nil {
		t.Fatal(err)
	}
	minerSHA256, err := codingcontract.InferenceMinerRequestSHA256(fixture.policy, miner)
	if err != nil {
		t.Fatal(err)
	}
	lockedSHA256, err := codingcontract.InferenceLockedRequestSHA256(fixture.policy, locked)
	if err != nil {
		t.Fatal(err)
	}
	return codingrelay.DispatchRecord{
		Sequence: 1, RequestSequence: 1, Attempt: 1,
		RequestID:          "30000000-0000-4000-8000-000000000003",
		MinerRequestSHA256: minerSHA256, MinerRequest: miner,
		LockedRequestSHA256: lockedSHA256, LockedRequest: locked,
	}
}

func (fixture *gatewayFixture) minerRequestBody(t *testing.T) []byte {
	t.Helper()
	dispatch := fixture.incompleteDispatch(t)
	body, err := json.Marshal(dispatch.MinerRequest)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func (fixture *gatewayFixture) successfulTransport(t *testing.T, calls *int) http.RoundTripper {
	t.Helper()
	return roundTripperFunc(func(request *http.Request) (*http.Response, error) {
		*calls = *calls + 1
		if request.URL.String() != fixture.capability.ProxyURL ||
			request.Header.Get("Authorization") == "" ||
			request.Header.Get("X-Ditto-Proof") == "" {
			t.Fatalf("unexpected dispatch route or proof headers")
		}
		var dispatch struct {
			Sequence            uint32                                `json:"sequence"`
			RequestSequence     uint32                                `json:"request_sequence"`
			Attempt             uint32                                `json:"attempt"`
			RequestID           string                                `json:"request_id"`
			LockedRequestSHA256 string                                `json:"locked_request_sha256"`
			LockedRequest       codingcontract.InferenceLockedRequest `json:"locked_request"`
		}
		if err := json.NewDecoder(request.Body).Decode(&dispatch); err != nil {
			t.Fatal(err)
		}
		normalizedBody := append([]byte(nil), fixture.vector.NormalizedProviderResponses[0]...)
		normalized, err := codingcontract.ParseInferenceNormalizedResponse(normalizedBody, fixture.policy)
		if err != nil {
			t.Fatal(err)
		}
		responseSHA256, err := codingcontract.InferenceNormalizedResponseSHA256(fixture.policy, normalized)
		if err != nil {
			t.Fatal(err)
		}
		generationID := normalized.ID
		receiptProvider := fixture.policy.ReceiptProvider
		settlement := codingcontract.InferenceProviderSettlement{
			Schema:                codingcontract.InferenceProviderSettlementSchema,
			CodingContractVersion: codingcontract.ContractVersion,
			TicketID:              fixture.binding.TicketID, CaseID: fixture.binding.CaseID,
			ProfileCapabilityID:  fixture.binding.ProfileCapabilityID,
			InferenceGrantSHA256: fixture.binding.InferenceGrantSHA256,
			GrantID:              fixture.binding.GrantID, Generation: fixture.binding.Generation,
			RequestID: dispatch.RequestID, RequestSequence: dispatch.RequestSequence,
			Attempt: dispatch.Attempt, LockedRequestSHA256: dispatch.LockedRequestSHA256,
			Outcome: codingcontract.InferenceReceiptComplete, HTTPStatus: http.StatusOK,
			ResponseSHA256: &responseSHA256, ResponseDigestKind: "normalized_v1",
			ProviderGenerationID: &generationID, Model: fixture.policy.Model,
			ProviderAPI: fixture.policy.ProviderAPI, ProviderRoute: fixture.policy.ProviderRoute,
			ReceiptProvider:          &receiptProvider,
			ProviderRouteProfile:     fixture.policy.ProviderRouteProfile,
			ProviderAccountGuardrail: fixture.policy.ProviderAccountGuardrail,
			ProviderPipelinePolicy:   fixture.policy.ProviderPipelinePolicy,
			ProviderCachePolicy:      fixture.policy.ProviderCachePolicy,
			RouterMetadataVerified:   true,
			RouterAttempts:           []codingcontract.InferenceRouterAttempt{{Provider: receiptProvider, Selected: true}},
			PipelineStages:           []string{}, UsageAvailable: true,
			PromptTokens:     normalized.Usage.PromptTokens,
			CompletionTokens: normalized.Usage.CompletionTokens,
			TotalTokens:      normalized.Usage.TotalTokens, CostAvailable: true,
			CostUSDMicros: normalized.Usage.CostUSDMicros,
		}
		body, err := json.Marshal(map[string]any{
			"schema":                  "dittobench-coding-inference-dispatch-result-v1",
			"coding_contract_version": codingcontract.ContractVersion,
			"weight_eligible":         false, "sequence": dispatch.Sequence,
			"settlement":                         settlement,
			"normalized_response_base64":         base64.StdEncoding.EncodeToString(normalizedBody),
			"failure_response_projection_base64": nil,
		})
		if err != nil {
			t.Fatal(err)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header: http.Header{
				"Content-Type":  []string{"application/json"},
				"Cache-Control": []string{"no-store"},
			},
			Body: io.NopCloser(bytes.NewReader(append(body, '\n'))),
		}, nil
	})
}

func TestActivateRevokeEvidenceClose(t *testing.T) {
	fixture := newGatewayFixture(t)
	events := []string{}
	fixture.publisher.events = &events
	fixture.authorizer.events = &events
	fixture.revoker.events = &events
	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	url, err := gateway.URL()
	if err != nil || url != fixture.publisher.url {
		t.Fatalf("url=%q err=%v", url, err)
	}
	if fixture.publisher.publishCall != 1 || fixture.publisher.handler == nil ||
		!reflect.DeepEqual(fixture.publisher.binding, capabilityBinding(fixture.binding)) {
		t.Fatal("publisher did not receive the exact route binding")
	}
	if fixture.authorizer.calls != 1 ||
		!reflect.DeepEqual(fixture.authorizer.binding, capabilityBinding(fixture.binding)) {
		t.Fatal("authorizer did not receive the exact route binding")
	}
	if _, err := gateway.Evidence(t.Context(), fixture.evidenceBinding()); !errors.Is(err, ErrNotRevoked) {
		t.Fatalf("evidence before revoke err=%v", err)
	}
	if err := gateway.Close(); !errors.Is(err, ErrNotRevoked) {
		t.Fatalf("close before revoke err=%v", err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatalf("idempotent revoke: %v", err)
	}
	if !reflect.DeepEqual(events, []string{"authorize", "publish", "outer", "grant"}) {
		t.Fatalf("revocation order=%v", events)
	}
	if fixture.publisher.revokeCall != 1 || fixture.revoker.calls != 1 {
		t.Fatalf("revoke calls outer=%d grant=%d", fixture.publisher.revokeCall, fixture.revoker.calls)
	}
	if _, err := gateway.URL(); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("url after revoke err=%v", err)
	}
	evidence, err := gateway.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if evidence.UsageStatus != codingcontract.ModelUsageNotInvoked || evidence.Requests != 0 ||
		evidence.InferenceGrantSHA256 != fixture.binding.InferenceGrantSHA256 {
		t.Fatalf("unexpected not-invoked evidence: %+v", evidence)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatalf("idempotent close: %v", err)
	}
	if _, err := gateway.URL(); !errors.Is(err, ErrClosed) {
		t.Fatalf("url after close err=%v", err)
	}
}

func TestGatewayDispatchesOneLockedRequestAndFinalizesEvidence(t *testing.T) {
	fixture := newGatewayFixture(t)
	providerCalls := 0
	config := fixture.config()
	config.Transport = fixture.successfulTransport(t, &providerCalls)
	config.NewRequestID = func() string { return "30000000-0000-4000-8000-000000000003" }
	config.NewNonce = func() string { return "40000000-0000-4000-8000-000000000004" }
	gateway, err := Activate(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "http://gateway.invalid/chat/completions", bytes.NewReader(fixture.minerRequestBody(t)))
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	fixture.publisher.handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("relay status=%d body=%s", response.Code, response.Body.String())
	}
	if providerCalls != 1 {
		t.Fatalf("provider calls=%d", providerCalls)
	}
	if _, err := codingcontract.ParseInferenceMinerResponse(response.Body.Bytes(), fixture.policy); err != nil {
		t.Fatalf("miner response: %v", err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	evidence, err := gateway.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if evidence.UsageStatus != codingcontract.ModelUsageComplete || evidence.Requests != 1 ||
		evidence.PromptTokens == 0 || evidence.CompletionTokens == 0 ||
		evidence.ProviderReceiptSetSHA256 == nil {
		t.Fatalf("complete evidence=%+v", evidence)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
	recoveryRevoker := &fakeGrantRevoker{}
	recoveryConfig := fixture.recoveryConfig()
	recoveryConfig.GrantRevoker = recoveryRevoker
	recovered, err := Recover(t.Context(), recoveryConfig)
	if err != nil {
		t.Fatal(err)
	}
	recoveredEvidence, err := recovered.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(evidence, recoveredEvidence) {
		t.Fatalf("complete recovered evidence drifted\nexpected=%+v\nobserved=%+v", evidence, recoveredEvidence)
	}
	if recoveryRevoker.calls != 1 {
		t.Fatalf("complete recovery revoke calls=%d", recoveryRevoker.calls)
	}
	if err := recovered.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestUsedJournalRequiresRecoveryAndIsNeverRepublished(t *testing.T) {
	fixture := newGatewayFixture(t)
	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	expected, err := gateway.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}

	secondPublisher := &fakePublisher{url: "http://127.0.0.1:11436/capability/other"}
	secondRevoker := &fakeGrantRevoker{}
	secondConfig := fixture.config()
	secondConfig.Publisher = secondPublisher
	secondConfig.GrantRevoker = secondRevoker
	if _, err := Activate(t.Context(), secondConfig); !errors.Is(err, ErrAlreadyUsed) {
		t.Fatalf("activate used journal err=%v", err)
	}
	if secondPublisher.publishCall != 0 || secondRevoker.calls != 1 {
		t.Fatalf("used journal publish=%d revoke=%d", secondPublisher.publishCall, secondRevoker.calls)
	}

	recoveryRevoker := &fakeGrantRevoker{}
	recoveryConfig := fixture.recoveryConfig()
	recoveryConfig.GrantRevoker = recoveryRevoker
	recovered, err := Recover(t.Context(), recoveryConfig)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := recovered.URL(); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("recovery exposed URL: %v", err)
	}
	observed, err := recovered.Evidence(t.Context(), fixture.evidenceBinding())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(expected, observed) {
		t.Fatalf("recovered evidence drifted\nexpected=%+v\nobserved=%+v", expected, observed)
	}
	if recoveryRevoker.calls != 1 {
		t.Fatalf("recovery revoke calls=%d", recoveryRevoker.calls)
	}
	if err := recovered.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestDurableActivationBindingBlocksRepublishBeforeFirstDispatch(t *testing.T) {
	fixture := newGatewayFixture(t)
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: fixture.root, Policy: fixture.policy, MaxTotalBytes: 512 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := journal.Bind(t.Context(), fixture.binding); err != nil {
		t.Fatal(err)
	}
	if err := journal.Bind(t.Context(), fixture.binding); err != nil {
		t.Fatalf("idempotent bind: %v", err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}

	if _, err := Activate(t.Context(), fixture.config()); !errors.Is(err, ErrAlreadyUsed) {
		t.Fatalf("activate bound journal err=%v", err)
	}
	if fixture.publisher.publishCall != 0 || fixture.revoker.calls != 1 {
		t.Fatalf("bound journal publish=%d revoke=%d", fixture.publisher.publishCall, fixture.revoker.calls)
	}
}

func TestRecoverRevokesAmbiguousDispatchWithoutRepublishing(t *testing.T) {
	fixture := newGatewayFixture(t)
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: fixture.root, Policy: fixture.policy, MaxTotalBytes: 512 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := journal.Bind(t.Context(), fixture.binding); err != nil {
		t.Fatal(err)
	}
	if err := journal.Begin(t.Context(), fixture.binding, fixture.incompleteDispatch(t)); err != nil {
		t.Fatal(err)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}

	if _, err := Recover(t.Context(), fixture.recoveryConfig()); !errors.Is(err, ErrAmbiguousRecovery) {
		t.Fatalf("ambiguous recovery err=%v", err)
	}
	if fixture.revoker.calls != 1 || fixture.publisher.publishCall != 0 {
		t.Fatalf("ambiguous recovery revoke=%d publish=%d", fixture.revoker.calls, fixture.publisher.publishCall)
	}
	reopened, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: fixture.root, Policy: fixture.policy, MaxTotalBytes: 512 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := reopened.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if !snapshot.Revoked || len(snapshot.Entries) != 1 || snapshot.Entries[0].Completed {
		t.Fatalf("ambiguous snapshot=%+v", snapshot)
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRemoteRevocationRetriesWithoutReopeningOuterRoute(t *testing.T) {
	fixture := newGatewayFixture(t)
	fixture.revoker.failures = 1
	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	if err := gateway.Revoke(t.Context()); !errors.Is(err, ErrRevocation) {
		t.Fatalf("first revoke err=%v", err)
	}
	if _, err := gateway.URL(); !errors.Is(err, ErrCapabilityRevoked) {
		t.Fatalf("URL reopened after remote failure: %v", err)
	}
	if _, err := gateway.Evidence(t.Context(), fixture.evidenceBinding()); !errors.Is(err, ErrNotRevoked) {
		t.Fatalf("evidence before remote retry err=%v", err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if fixture.publisher.revokeCall != 1 || fixture.revoker.calls != 2 {
		t.Fatalf("retry calls outer=%d grant=%d", fixture.publisher.revokeCall, fixture.revoker.calls)
	}
	if _, err := gateway.Evidence(t.Context(), fixture.evidenceBinding()); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentRevocationCoalescesExactLifecycle(t *testing.T) {
	fixture := newGatewayFixture(t)
	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	const callers = 16
	errorsSeen := make(chan error, callers)
	var wait sync.WaitGroup
	for range callers {
		wait.Add(1)
		go func() {
			defer wait.Done()
			errorsSeen <- gateway.Revoke(t.Context())
		}()
	}
	wait.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		if err != nil {
			t.Fatalf("concurrent revoke: %v", err)
		}
	}
	if fixture.publisher.revokeCall != 1 || fixture.revoker.calls != 1 {
		t.Fatalf("concurrent calls outer=%d grant=%d", fixture.publisher.revokeCall, fixture.revoker.calls)
	}
	if _, err := gateway.Evidence(t.Context(), fixture.evidenceBinding()); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestJournalOpenFailureStillRevokesExchangedGrant(t *testing.T) {
	fixture := newGatewayFixture(t)
	config := fixture.config()
	config.JournalRoot = filepath.Join(t.TempDir(), "missing")
	if _, err := Activate(t.Context(), config); !errors.Is(err, ErrActivation) {
		t.Fatalf("journal open err=%v", err)
	}
	if fixture.publisher.publishCall != 0 || fixture.revoker.calls != 1 {
		t.Fatalf("open failure publish=%d revoke=%d", fixture.publisher.publishCall, fixture.revoker.calls)
	}
}

func TestClientConstructionFailureRevokesDurableActivation(t *testing.T) {
	fixture := newGatewayFixture(t)
	config := fixture.config()
	config.Capability.Bearer = "too-short"
	if _, err := Activate(t.Context(), config); !errors.Is(err, ErrActivation) {
		t.Fatalf("client construction err=%v", err)
	}
	if fixture.publisher.publishCall != 0 || fixture.revoker.calls != 1 {
		t.Fatalf("client failure publish=%d revoke=%d", fixture.publisher.publishCall, fixture.revoker.calls)
	}
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: fixture.root, Policy: fixture.policy, MaxTotalBytes: 512 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := journal.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Binding == nil || !snapshot.Revoked || len(snapshot.Entries) != 0 {
		t.Fatalf("failed activation snapshot=%+v", snapshot)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestAuthorizerFailureRevokesDurableActivationBeforePublish(t *testing.T) {
	fixture := newGatewayFixture(t)
	fixture.authorizer.err = errors.New("outbox marker unavailable")
	if _, err := Activate(t.Context(), fixture.config()); !errors.Is(err, ErrActivation) {
		t.Fatalf("authorizer err=%v", err)
	}
	if fixture.authorizer.calls != 1 || fixture.publisher.publishCall != 0 || fixture.revoker.calls != 1 {
		t.Fatalf(
			"authorizer failure authorize=%d publish=%d revoke=%d",
			fixture.authorizer.calls, fixture.publisher.publishCall, fixture.revoker.calls,
		)
	}
	journal, err := codingrelayjournal.Open(codingrelayjournal.Config{
		Root: fixture.root, Policy: fixture.policy, MaxTotalBytes: 512 << 20,
		MaxEntries: int(fixture.policy.MaxRequests + fixture.policy.MaxRetries),
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot, err := journal.Load(t.Context(), fixture.binding)
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Binding == nil || !snapshot.Revoked || len(snapshot.Entries) != 0 {
		t.Fatalf("authorizer failure snapshot=%+v", snapshot)
	}
	if err := journal.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestInvalidPublishedURLRevokesEveryCapability(t *testing.T) {
	fixture := newGatewayFixture(t)
	fixture.publisher.url = "https://capability.invalid/private/chat/completions"
	if _, err := Activate(t.Context(), fixture.config()); !errors.Is(err, ErrActivation) {
		t.Fatalf("activate invalid URL err=%v", err)
	}
	if fixture.publisher.publishCall != 1 || fixture.publisher.revokeCall != 1 ||
		fixture.publisher.closeCall != 1 || fixture.revoker.calls != 1 {
		t.Fatalf(
			"cleanup calls publish=%d outer=%d close=%d grant=%d",
			fixture.publisher.publishCall, fixture.publisher.revokeCall,
			fixture.publisher.closeCall, fixture.revoker.calls,
		)
	}
}

func TestTypedNilPublishedCapabilityFailsWithoutPanic(t *testing.T) {
	fixture := newGatewayFixture(t)
	fixture.publisher.typedNil = true
	if _, err := Activate(t.Context(), fixture.config()); !errors.Is(err, ErrActivation) {
		t.Fatalf("typed nil published capability err=%v", err)
	}
	if fixture.publisher.publishCall != 1 || fixture.publisher.revokeCall != 0 ||
		fixture.revoker.calls != 1 {
		t.Fatalf(
			"typed nil cleanup publish=%d outer=%d grant=%d",
			fixture.publisher.publishCall, fixture.publisher.revokeCall, fixture.revoker.calls,
		)
	}
}

func TestAuthorityDriftAndTypedNilFailClosed(t *testing.T) {
	fixture := newGatewayFixture(t)
	var nilAuthorizer *fakeAuthorizer
	config := fixture.config()
	config.Authorizer = nilAuthorizer
	if _, err := Activate(t.Context(), config); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("typed nil authorizer err=%v", err)
	}
	var nilPublisher *fakePublisher
	config = fixture.config()
	config.Publisher = nilPublisher
	if _, err := Activate(t.Context(), config); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("typed nil publisher err=%v", err)
	}
	var nilRevoker *fakeGrantRevoker
	config = fixture.config()
	config.GrantRevoker = nilRevoker
	if _, err := Activate(t.Context(), config); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("typed nil revoker err=%v", err)
	}

	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	drifted := fixture.evidenceBinding()
	drifted.ProfileCapabilityID = "profile-other"
	if _, err := gateway.Evidence(t.Context(), drifted); !errors.Is(err, ErrEvidence) {
		t.Fatalf("drifted evidence binding err=%v", err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestPrivateTypesRejectSerialization(t *testing.T) {
	fixture := newGatewayFixture(t)
	privateValues := []any{
		fixture.config(), fixture.recoveryConfig(), capabilityBinding(fixture.binding),
		grantRevocation(fixture.binding),
	}
	for _, value := range privateValues {
		if body, err := json.Marshal(value); !errors.Is(err, ErrSecretSerialization) || body != nil {
			t.Fatalf("%T marshal body=%q err=%v", value, body, err)
		}
	}
	gateway, err := Activate(t.Context(), fixture.config())
	if err != nil {
		t.Fatal(err)
	}
	if body, err := json.Marshal(gateway); !errors.Is(err, ErrSecretSerialization) || body != nil {
		t.Fatalf("gateway marshal body=%q err=%v", body, err)
	}
	if err := gateway.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := gateway.Close(); err != nil {
		t.Fatal(err)
	}
}

func repeat(value string, count int) string {
	result := ""
	for range count {
		result += value
	}
	return result
}
