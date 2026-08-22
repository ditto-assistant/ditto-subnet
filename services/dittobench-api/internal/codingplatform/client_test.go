package codingplatform

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func TestClientDispatchesExactProofBoundLockedRequest(t *testing.T) {
	fixture := newPlatformFixture(t)
	var calls int
	transport := roundTripFunc(func(request *http.Request) (*http.Response, error) {
		calls++
		if request.Method != http.MethodPost || request.URL.String() != fixture.capability.ProxyURL {
			t.Fatalf("request route=%s %s", request.Method, request.URL)
		}
		if request.Header.Get("Content-Type") != "application/json" ||
			request.Header.Get("Accept") != "application/json" ||
			request.Header.Get("Cache-Control") != "no-store" ||
			request.Header.Get("HTTP-Referer") != "" || request.Header.Get("X-OpenRouter-Title") != "" {
			t.Fatalf("request headers=%v", request.Header)
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Fatal(err)
		}
		var dispatched dispatchRequest
		if err := json.Unmarshal(body, &dispatched); err != nil {
			t.Fatal(err)
		}
		if dispatched.Schema != dispatchRequestSchema || dispatched.CodingContractVersion != 1 ||
			dispatched.WeightEligible || dispatched.TicketID != fixture.binding.TicketID ||
			dispatched.CaseID != fixture.binding.CaseID ||
			dispatched.ProfileCapabilityID != fixture.binding.ProfileCapabilityID ||
			dispatched.InferenceGrantSHA256 != fixture.binding.InferenceGrantSHA256 ||
			dispatched.GrantID != fixture.binding.GrantID || dispatched.Generation != fixture.binding.Generation ||
			dispatched.Sequence != fixture.request.Sequence ||
			dispatched.RequestSequence != fixture.request.RequestSequence ||
			dispatched.Attempt != fixture.request.Attempt || dispatched.RequestID != fixture.request.RequestID ||
			dispatched.LockedRequestSHA256 != fixture.request.LockedRequestSHA256 ||
			dispatched.Deadline != isoformatMicro(fixture.binding.Deadline) {
			t.Fatalf("dispatch authority=%#v", dispatched)
		}
		digest, err := codingcontract.InferenceLockedRequestSHA256(fixture.policy, dispatched.LockedRequest)
		if err != nil || digest != fixture.request.LockedRequestSHA256 {
			t.Fatalf("locked digest=%s err=%v", digest, err)
		}
		if request.Header.Get("Authorization") != "Bearer "+fixture.capability.Bearer ||
			request.Header.Get("X-Ditto-Grant") != fixture.binding.GrantID ||
			request.Header.Get("X-Ditto-Generation") != "1" ||
			request.Header.Get("X-Ditto-Nonce") != "66666666-6666-4666-8666-666666666666" ||
			request.Header.Get("X-Ditto-Requested-At") != isoformatMicro(fixture.now) {
			t.Fatalf("proof headers=%v", request.Header)
		}
		proof, err := base64.RawURLEncoding.DecodeString(request.Header.Get("X-Ditto-Proof"))
		if err != nil || !ed25519.Verify(
			fixture.privateKey.Public().(ed25519.PublicKey),
			dispatchProofMessage(
				fixture.binding.GrantID,
				fixture.binding.Generation,
				request.Header.Get("X-Ditto-Nonce"),
				fixture.now,
				body,
			),
			proof,
		) {
			t.Fatal("dispatch proof did not verify")
		}
		return response(
			http.StatusOK,
			fixture.responseBody(t, fixture.settlement, fixture.normalized, nil),
		), nil
	})
	client, err := New(fixture.config(transport))
	if err != nil {
		t.Fatal(err)
	}
	beforeDigest, err := codingcontract.InferenceLockedRequestSHA256(fixture.policy, fixture.request.LockedRequest)
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.Complete(t.Context(), fixture.request)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 1 || !bytes.Equal(result.NormalizedResponse, fixture.normalized) ||
		len(result.FailureResponseProjection) != 0 {
		t.Fatalf("calls=%d result=%#v", calls, result)
	}
	assertSettlementsEqual(t, result.Settlement, fixture.settlement)
	afterDigest, err := codingcontract.InferenceLockedRequestSHA256(fixture.policy, fixture.request.LockedRequest)
	if err != nil || afterDigest != beforeDigest {
		t.Fatalf("caller request mutated: before=%s after=%s err=%v", beforeDigest, afterDigest, err)
	}
	result.NormalizedResponse[0] ^= 0xff
	if bytes.Equal(result.NormalizedResponse, fixture.normalized) {
		t.Fatal("result response aliased fixture bytes")
	}
}

func TestClientAcceptsRetryAndAuditableFailureProjections(t *testing.T) {
	fixture := newPlatformFixture(t)
	body, err := io.ReadAll(bytes.NewReader(mustReadPolicyVector(t)))
	if err != nil {
		t.Fatal(err)
	}
	var vector policyVector
	if err := json.Unmarshal(body, &vector); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name       string
		settlement json.RawMessage
		failure    []byte
	}{
		{name: "receipt-free retry", settlement: vector.ProviderSettlements["retry_complete"][0]},
		{name: "provider failure without response", settlement: vector.ProviderSettlements["provider_failure"][0]},
		{
			name:       "invalid provider response",
			settlement: vector.ProviderSettlements["response_invalid"][0],
			failure:    append([]byte(nil), vector.InvalidProviderResponses["response_invalid"]...),
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			settlement, err := codingcontract.ParseInferenceProviderSettlement(test.settlement, fixture.policy)
			if err != nil {
				t.Fatal(err)
			}
			request := fixture.request
			request.Sequence = 1
			request.RequestSequence = settlement.RequestSequence
			request.Attempt = settlement.Attempt
			request.RequestID = settlement.RequestID
			request.LockedRequestSHA256 = settlement.LockedRequestSHA256
			if settlement.LockedRequestSHA256 != fixture.request.LockedRequestSHA256 {
				locked, parseErr := codingcontract.ParseInferenceLockedRequest(vector.LockedRequests[0], fixture.policy)
				if parseErr != nil {
					t.Fatal(parseErr)
				}
				request.LockedRequest = locked
			}
			transport := roundTripFunc(func(*http.Request) (*http.Response, error) {
				return response(
					http.StatusOK,
					fixture.responseBody(t, settlement, nil, test.failure),
				), nil
			})
			config := fixture.config(transport)
			config.NewNonce = func() string { return "66666666-6666-4666-8666-666666666666" }
			client, err := New(config)
			if err != nil {
				t.Fatal(err)
			}
			result, err := client.Complete(t.Context(), request)
			if err != nil {
				t.Fatal(err)
			}
			if len(result.NormalizedResponse) != 0 || !bytes.Equal(result.FailureResponseProjection, test.failure) {
				t.Fatalf("result=%#v", result)
			}
			assertSettlementsEqual(t, result.Settlement, settlement)
		})
	}
}

func TestClientRejectsResponseIdentityAndProjectionDrift(t *testing.T) {
	fixture := newPlatformFixture(t)
	tests := map[string]func(*codingcontract.InferenceProviderSettlement, *[]byte, *uint32){
		"sequence": func(_ *codingcontract.InferenceProviderSettlement, _ *[]byte, sequence *uint32) {
			*sequence = *sequence + 1
		},
		"ticket": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.TicketID = "88888888-8888-4888-8888-888888888888"
		},
		"case": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.CaseID = "case-other"
		},
		"profile": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.ProfileCapabilityID = "profile-other"
		},
		"grant digest": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.InferenceGrantSHA256 = repeat("a", sha256.Size*2)
		},
		"grant": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.GrantID = "99999999-9999-4999-8999-999999999999"
		},
		"request": func(value *codingcontract.InferenceProviderSettlement, _ *[]byte, _ *uint32) {
			value.RequestID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
		},
		"projection": func(_ *codingcontract.InferenceProviderSettlement, body *[]byte, _ *uint32) {
			(*body)[0] ^= 0x01
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			settlement := fixture.settlement.Clone()
			normalized := append([]byte(nil), fixture.normalized...)
			sequence := fixture.request.Sequence
			mutate(&settlement, &normalized, &sequence)
			body := fixture.responseBody(t, settlement, normalized, nil)
			var envelope map[string]any
			if err := json.Unmarshal(body, &envelope); err != nil {
				t.Fatal(err)
			}
			envelope["sequence"] = sequence
			body, _ = json.Marshal(envelope)
			client, err := New(fixture.config(roundTripFunc(func(*http.Request) (*http.Response, error) {
				return response(http.StatusOK, body), nil
			})))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.Complete(t.Context(), fixture.request); !errors.Is(err, ErrResponseIntegrity) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestDispatchProofBindsExactBody(t *testing.T) {
	fixture := newPlatformFixture(t)
	body := []byte(`{"one":1}`)
	message := dispatchProofMessage(
		fixture.binding.GrantID,
		fixture.binding.Generation,
		"66666666-6666-4666-8666-666666666666",
		fixture.now,
		body,
	)
	digest := sha256.Sum256(body)
	want := "ditto-inference:v1:" + fixture.binding.GrantID + ":1:" +
		"66666666-6666-4666-8666-666666666666:" + isoformatMicro(fixture.now) + ":" +
		hex.EncodeToString(digest[:])
	if string(message) != want {
		t.Fatalf("message=%q want=%q", message, want)
	}
	body[1] = 't'
	if reflect.DeepEqual(message, dispatchProofMessage(
		fixture.binding.GrantID,
		fixture.binding.Generation,
		"66666666-6666-4666-8666-666666666666",
		fixture.now,
		body,
	)) {
		t.Fatal("proof message did not bind exact body bytes")
	}
}

func mustReadPolicyVector(t *testing.T) []byte {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(
		"..", "..", "..", "..", "packages", "dittobench-coding-contract", "testdata",
		"coding_inference_policy_v1.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	return body
}
