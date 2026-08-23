package codinggrantrevoke

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
	"github.com/ditto-assistant/dittobench-api/internal/codingplatform"
	"github.com/ditto-assistant/dittobench-api/internal/codingrelay"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func revokeFixture() (time.Time, codingrelay.Binding, codingplatform.RevocationCapability, codinggateway.GrantRevocation) {
	now := time.Date(2026, 8, 23, 18, 0, 0, 0, time.UTC)
	binding := codingrelay.Binding{
		AttemptID: "attempt-revoke-001", AgentArtifactSHA256: strings.Repeat("a", 64),
		HarnessInstanceID: "harness-revoke-001",
		TicketID:          "33333333-3333-4333-8333-333333333333",
		CaseID:            "case-revoke-001", ProfileCapabilityID: "profile-revoke-001",
		GrantID: "88888888-8888-4888-8888-888888888888", Generation: 1,
		InferenceGrantSHA256: strings.Repeat("9", 64),
		IssuedAt:             now, Deadline: now.Add(time.Hour), RequestBudget: 16,
		PromptTokenBudget: 1000, CompletionTokenBudget: 500,
	}
	capability := codingplatform.RevocationCapability{
		GrantID: binding.GrantID, TicketID: binding.TicketID, Generation: binding.Generation,
		InferenceGrantSHA256: binding.InferenceGrantSHA256, Deadline: binding.Deadline,
		Bearer: strings.Repeat("r", 43),
		URL:    "https://platform.invalid" + revokePath,
	}
	expected := codinggateway.GrantRevocation{
		TicketID: binding.TicketID, CaseID: binding.CaseID,
		ProfileCapabilityID: binding.ProfileCapabilityID, GrantID: binding.GrantID,
		Generation: binding.Generation, InferenceGrantSHA256: binding.InferenceGrantSHA256,
		Deadline: binding.Deadline,
	}
	return now, binding, capability, expected
}

func revokeResponse(now time.Time, idempotent bool) *http.Response {
	body := `{"schema":"dittobench-coding-inference-revocation-v1","coding_contract_version":1,"weight_eligible":false,"grant_id":"88888888-8888-4888-8888-888888888888","ticket_id":"33333333-3333-4333-8333-333333333333","status":"revoked","generation":1,"revoked_at":"` + now.Format(time.RFC3339) + `","idempotent":`
	if idempotent {
		body += "true}"
	} else {
		body += "false}"
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"application/json"}, "Cache-Control": {"no-store"}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

func TestRevokerUsesNarrowBearerAndCachesDurableSuccess(t *testing.T) {
	now, binding, capability, expected := revokeFixture()
	calls := 0
	revoker, err := New(Config{
		Capability: capability, Binding: binding, Now: func() time.Time { return now },
		Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
			calls++
			if request.URL.String() != capability.URL || request.Method != http.MethodPost ||
				request.Header.Get("Authorization") != "Bearer "+capability.Bearer ||
				request.Header.Get("Cache-Control") != "no-store" {
				t.Fatalf("request authority drift: %#v", request)
			}
			var body capabilityRevokeRequest
			if json.NewDecoder(request.Body).Decode(&body) != nil || body.GrantID != capability.GrantID ||
				body.TicketID != capability.TicketID || body.Generation != capability.Generation {
				t.Fatalf("request body=%#v", body)
			}
			return revokeResponse(now, false), nil
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if revoker.capability.Bearer != "" || string(revoker.bearer) != capability.Bearer {
		t.Fatal("revoker retained or lost the wrong bearer representation")
	}
	if err := revoker.Revoke(t.Context(), expected); err != nil {
		t.Fatal(err)
	}
	if err := revoker.Revoke(t.Context(), expected); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("calls=%d", calls)
	}
	if _, err := json.Marshal(revoker); !errors.Is(err, ErrSecret) {
		t.Fatalf("marshal err=%v", err)
	}
	if strings.Contains(revoker.String(), capability.Bearer) || strings.Contains(revoker.String(), capability.URL) {
		t.Fatal("revocation capability leaked")
	}
	if err := revoker.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestRevokerRetriesAmbiguousResponseWithSameAuthority(t *testing.T) {
	now, binding, capability, expected := revokeFixture()
	calls := 0
	revoker, err := New(Config{
		Capability: capability, Binding: binding, Now: func() time.Time { return now },
		Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			calls++
			if calls == 1 {
				return nil, errors.New("response lost")
			}
			return revokeResponse(now, true), nil
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := revoker.Revoke(t.Context(), expected); !errors.Is(err, ErrTransport) {
		t.Fatalf("first err=%v", err)
	}
	if err := revoker.Revoke(t.Context(), expected); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("calls=%d", calls)
	}
}

func TestRevokerRejectsBindingAndResponseDrift(t *testing.T) {
	now, binding, capability, expected := revokeFixture()
	for name, mutate := range map[string]func(*codingplatform.RevocationCapability){
		"ticket": func(value *codingplatform.RevocationCapability) {
			value.TicketID = "44444444-4444-4444-8444-444444444444"
		},
		"generation": func(value *codingplatform.RevocationCapability) { value.Generation++ },
		"policy":     func(value *codingplatform.RevocationCapability) { value.InferenceGrantSHA256 = strings.Repeat("8", 64) },
		"bearer":     func(value *codingplatform.RevocationCapability) { value.Bearer = "short" },
		"url":        func(value *codingplatform.RevocationCapability) { value.URL = "http://platform.invalid" + revokePath },
	} {
		t.Run(name, func(t *testing.T) {
			changed := capability
			mutate(&changed)
			if _, err := New(Config{Capability: changed, Binding: binding, Now: func() time.Time { return now }}); !errors.Is(err, ErrInvalid) {
				t.Fatalf("err=%v", err)
			}
		})
	}
	driftedBinding := binding
	driftedBinding.CaseID = ""
	if _, err := New(Config{Capability: capability, Binding: driftedBinding, Now: func() time.Time { return now }}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("invalid binding err=%v", err)
	}
	revoker, err := New(Config{
		Capability: capability, Binding: binding, Now: func() time.Time { return now },
		Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			response := revokeResponse(now, false)
			response.Header.Del("Cache-Control")
			return response, nil
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := revoker.Revoke(t.Context(), expected); !errors.Is(err, ErrTransport) {
		t.Fatalf("response err=%v", err)
	}
	drift := expected
	drift.CaseID = "other"
	if err := revoker.Revoke(t.Context(), drift); !errors.Is(err, ErrInvalid) {
		t.Fatalf("drift err=%v", err)
	}
}

func TestRevokerRejectsMalformedSuccessWithoutForgettingBearer(t *testing.T) {
	now, binding, capability, expected := revokeFixture()
	calls := 0
	revoker, err := New(Config{
		Capability: capability, Binding: binding, Now: func() time.Time { return now },
		Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			calls++
			if calls == 1 {
				response := revokeResponse(now, false)
				response.Body = io.NopCloser(strings.NewReader(`{"schema":"bad"}`))
				return response, nil
			}
			return revokeResponse(now, true), nil
		}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := revoker.Revoke(t.Context(), expected); !errors.Is(err, ErrResponse) {
		t.Fatalf("malformed err=%v", err)
	}
	if err := revoker.Revoke(t.Context(), expected); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("calls=%d", calls)
	}
}
