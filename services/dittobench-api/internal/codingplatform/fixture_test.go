package codingplatform

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type policyVector struct {
	Policy                      json.RawMessage              `json:"policy"`
	LockedRequests              []json.RawMessage            `json:"locked_requests"`
	NormalizedProviderResponses []json.RawMessage            `json:"normalized_provider_responses"`
	ProviderSettlements         map[string][]json.RawMessage `json:"provider_settlements"`
	InvalidProviderResponses    map[string]json.RawMessage   `json:"invalid_provider_response_projections"`
}

type platformFixture struct {
	now        time.Time
	policy     codingcontract.InferencePolicy
	binding    codingrelay.Binding
	capability GrantCapability
	request    codingrelay.UpstreamRequest
	settlement codingcontract.InferenceProviderSettlement
	normalized []byte
	privateKey ed25519.PrivateKey
}

func newPlatformFixture(t *testing.T) *platformFixture {
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
	locked, err := codingcontract.ParseInferenceLockedRequest(vector.LockedRequests[0], policy)
	if err != nil {
		t.Fatal(err)
	}
	lockedSHA256, err := codingcontract.InferenceLockedRequestSHA256(policy, locked)
	if err != nil {
		t.Fatal(err)
	}
	settlement, err := codingcontract.ParseInferenceProviderSettlement(
		vector.ProviderSettlements["complete"][0],
		policy,
	)
	if err != nil {
		t.Fatal(err)
	}
	normalized := append([]byte(nil), vector.NormalizedProviderResponses[0]...)
	normalizedResponse, err := codingcontract.ParseInferenceNormalizedResponse(normalized, policy)
	if err != nil {
		t.Fatal(err)
	}
	normalizedSHA256, err := codingcontract.InferenceNormalizedResponseSHA256(policy, normalizedResponse)
	if err != nil || settlement.ResponseSHA256 == nil || *settlement.ResponseSHA256 != normalizedSHA256 {
		t.Fatalf("normalized response digest=%s settlement=%v err=%v", normalizedSHA256, settlement.ResponseSHA256, err)
	}
	seed := bytes.Repeat([]byte{0x42}, ed25519.SeedSize)
	privateKey := ed25519.NewKeyFromSeed(seed)
	publicKey := privateKey.Public().(ed25519.PublicKey)
	now := time.Date(2026, 8, 22, 20, 0, 0, 123456000, time.UTC)
	grantSHA256, err := codingcontract.InferencePolicySHA256(policy)
	if err != nil {
		t.Fatal(err)
	}
	binding := codingrelay.Binding{
		AttemptID: "attempt-platform-001", AgentArtifactSHA256: "ab" + repeat("c", 62),
		HarnessInstanceID: "harness-platform-001", TicketID: settlement.TicketID,
		CaseID: settlement.CaseID, ProfileCapabilityID: settlement.ProfileCapabilityID,
		GrantID: settlement.GrantID, Generation: settlement.Generation,
		InferenceGrantSHA256: grantSHA256, IssuedAt: now.Add(-time.Minute), Deadline: now.Add(time.Hour),
		RequestBudget: 166, PromptTokenBudget: 200_000, CompletionTokenBudget: 30_000,
		CostBudgetUSDMicros: policy.MaxCostUSDMicros,
	}
	request := codingrelay.UpstreamRequest{
		Sequence: 1, RequestSequence: settlement.RequestSequence, Attempt: settlement.Attempt,
		RequestID: settlement.RequestID, LockedRequestSHA256: lockedSHA256,
		LockedRequest: locked, Deadline: binding.Deadline,
	}
	capability := GrantCapability{
		Binding: binding, Bearer: "platform-coding-bearer-000000000000000000000000",
		BrokerPublicKey:  base64.RawURLEncoding.EncodeToString(publicKey),
		BrokerPrivateKey: append(ed25519.PrivateKey(nil), privateKey...),
		ProxyURL:         "https://relay.invalid" + dispatchAPIPath,
	}
	return &platformFixture{
		now: now, policy: policy, binding: binding, capability: capability,
		request: request, settlement: settlement, normalized: normalized, privateKey: privateKey,
	}
}

func repeat(value string, count int) string {
	var buffer bytes.Buffer
	for range count {
		buffer.WriteString(value)
	}
	return buffer.String()
}

func (fixture *platformFixture) config(transport http.RoundTripper) Config {
	return Config{
		Policy: fixture.policy, Capability: fixture.capability, Transport: transport,
		Now:      func() time.Time { return fixture.now },
		NewNonce: func() string { return "66666666-6666-4666-8666-666666666666" },
	}
}

func (fixture *platformFixture) responseBody(
	t *testing.T,
	settlement codingcontract.InferenceProviderSettlement,
	normalized []byte,
	failure []byte,
) []byte {
	t.Helper()
	value := map[string]any{
		"schema": dispatchResponseSchema, "coding_contract_version": 1,
		"weight_eligible": false, "sequence": fixture.request.Sequence,
		"settlement":                         settlement,
		"normalized_response_base64":         nullableBase64(normalized),
		"failure_response_projection_base64": nullableBase64(failure),
	}
	body, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func nullableBase64(value []byte) any {
	if value == nil {
		return nil
	}
	return base64.StdEncoding.EncodeToString(value)
}

func response(status int, body []byte) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header: http.Header{
			"Content-Type":  []string{"application/json"},
			"Cache-Control": []string{"private, no-store"},
		},
		Body: io.NopCloser(bytes.NewReader(body)),
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func assertSettlementsEqual(
	t *testing.T,
	got codingcontract.InferenceProviderSettlement,
	want codingcontract.InferenceProviderSettlement,
) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("settlement=%#v want=%#v", got, want)
	}
}
