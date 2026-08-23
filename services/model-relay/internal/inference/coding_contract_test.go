package inference

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
)

func codingDispatchFixture(t *testing.T) []byte {
	t.Helper()
	vector := loadCodingPolicyVector(t)
	var locked codingLockedRequest
	if err := json.Unmarshal(vector.LockedRequests[0], &locked); err != nil {
		t.Fatal(err)
	}
	request := codingDispatchRequest{
		Schema: codingDispatchRequestSchema, CodingContractVersion: 1, WeightEligible: false,
		TicketID: "33333333-3333-4333-8333-333333333333", CaseID: "case-inference-001",
		ProfileCapabilityID: "profile-inference-001", InferenceGrantSHA256: codingInferenceGrantSHA256,
		GrantID: "44444444-4444-4444-8444-444444444444", Generation: 1,
		Sequence: 1, RequestSequence: 1, Attempt: 1,
		RequestID:           "55555555-5555-4555-8555-555555555555",
		LockedRequestSHA256: vector.Expected.LockedRequestSHA256[0], LockedRequest: locked,
		Deadline: isoformatMicro(time.Now().UTC().Add(time.Hour)),
	}
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestCodingTransportEnvelopeRejectedBeforeDatabase(t *testing.T) {
	body := codingDispatchFixture(t)
	base := func() *http.Request {
		request := httptest.NewRequest(http.MethodPost, "/api/v1/inference/coding/chat/completions", bytes.NewReader(body))
		for key, value := range map[string]string{
			"Authorization":        "Bearer " + codingTestBearer,
			"Content-Type":         "application/json",
			"X-Ditto-Grant":        "44444444-4444-4444-8444-444444444444",
			"X-Ditto-Generation":   "1",
			"X-Ditto-Nonce":        "66666666-6666-4666-8666-666666666666",
			"X-Ditto-Requested-At": isoformatMicro(time.Now().UTC()),
			"X-Ditto-Proof":        strings.Repeat("A", 86),
		} {
			request.Header.Set(key, value)
		}
		return request
	}
	tests := map[string]func(*http.Request){
		"query":                  func(request *http.Request) { request.URL.RawQuery = "target=other" },
		"content type":           func(request *http.Request) { request.Header.Set("Content-Type", "text/plain") },
		"duplicate content type": func(request *http.Request) { request.Header.Add("Content-Type", "application/json") },
		"content encoding":       func(request *http.Request) { request.Header.Set("Content-Encoding", "gzip") },
		"duplicate proof":        func(request *http.Request) { request.Header.Add("X-Ditto-Proof", "second") },
		"missing proof":          func(request *http.Request) { request.Header.Del("X-Ditto-Proof") },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			request := base()
			mutate(request)
			deps := newGateDeps(t, codingTestConfig(t, "http://127.0.0.1:1"))
			response := serve(deps, request)
			want := http.StatusBadRequest
			if name == "missing proof" {
				want = http.StatusUnauthorized
			}
			if response.Code != want {
				t.Fatalf("response=%d %s", response.Code, response.Body.String())
			}
		})
	}
}

func TestCodingDispatchParserMatchesSharedLockedVector(t *testing.T) {
	body := codingDispatchFixture(t)
	dispatch, locked, err := parseCodingDispatch(body)
	if err != nil {
		t.Fatal(err)
	}
	if dispatch.Schema != codingDispatchRequestSchema || dispatch.WeightEligible ||
		dispatch.LockedRequest.Model != codingModel || len(locked) == 0 {
		t.Fatalf("dispatch=%#v locked=%d", dispatch, len(locked))
	}
	var withFuture map[string]any
	if err := json.Unmarshal(body, &withFuture); err != nil {
		t.Fatal(err)
	}
	withFuture["future_outer_field"] = map[string]any{"ignored": true}
	forward, _ := json.Marshal(withFuture)
	if _, _, err := parseCodingDispatch(forward); err != nil {
		t.Fatalf("non-authoritative outer field rejected: %v", err)
	}
}

func TestCodingSettlementAndNormalizedDigestsMatchSharedVector(t *testing.T) {
	vector := loadCodingPolicyVector(t)
	var settlement codingProviderSettlement
	if err := json.Unmarshal(vector.ProviderSettlements["complete"][0], &settlement); err != nil {
		t.Fatal(err)
	}
	digest, _, err := codingSettlementDigest(settlement)
	if err != nil || digest != vector.Expected.ProviderSettlementSHA256["complete"][0] {
		t.Fatalf("settlement digest=%s want=%s err=%v", digest, vector.Expected.ProviderSettlementSHA256["complete"][0], err)
	}
	var normalized any
	decoder := json.NewDecoder(bytes.NewReader(vector.NormalizedProviderResponses[0]))
	decoder.UseNumber()
	if err := decoder.Decode(&normalized); err != nil {
		t.Fatal(err)
	}
	normalizedDigest, err := codingCanonicalSHA256(normalized)
	if err != nil || normalizedDigest != vector.Expected.NormalizedResponseSHA256[0] {
		t.Fatalf("normalized digest=%s want=%s err=%v", normalizedDigest, vector.Expected.NormalizedResponseSHA256[0], err)
	}
}

func TestCodingDispatchRejectsModelVisibleDriftAndUnsafeJSON(t *testing.T) {
	valid := codingDispatchFixture(t)
	mutations := map[string]func([]byte) []byte{
		"duplicate": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"schema":`), []byte(`"schema":"wrong","schema":`), 1)
		},
		"invalid UTF-8": func(body []byte) []byte { return append(body, 0xff) },
		"locked unknown": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"locked_request":{`), []byte(`"locked_request":{"temperature":0,`), 1)
		},
		"message unknown": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"role":"system"`), []byte(`"future":true,"role":"system"`), 1)
		},
		"system prompt": func(body []byte) []byte {
			return bytes.Replace(body, []byte("You are a repository coding agent"), []byte("You are an altered coding agent"), 1)
		},
		"tool schema": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"name":"repo_list_tree"`), []byte(`"name":"repo_other_tree"`), 1)
		},
		"provider fallback": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"allow_fallbacks":false`), []byte(`"allow_fallbacks":true`), 1)
		},
		"digest": func(body []byte) []byte {
			return bytes.Replace(body, []byte(`"locked_request_sha256":"`), []byte(`"locked_request_sha256":"a`), 1)
		},
		"naive deadline": func(body []byte) []byte {
			var value map[string]any
			_ = json.Unmarshal(body, &value)
			value["deadline"] = "2026-08-23T12:00:00"
			mutated, _ := json.Marshal(value)
			return mutated
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			if _, _, err := parseCodingDispatch(mutate(append([]byte(nil), valid...))); err == nil {
				t.Fatal("unsafe dispatch was accepted")
			}
		})
	}
}

func TestCodingCanonicalNumberGrammarIsBounded(t *testing.T) {
	for _, raw := range []string{"-0", "1e101", "1e-101", strings.Repeat("9", 101)} {
		if validCodingNumberLexeme(raw) {
			t.Fatalf("number %q accepted", raw)
		}
	}
	for _, raw := range []string{"0", "-1", "1e100", "1e-100", "0.001234"} {
		if !validCodingNumberLexeme(raw) {
			t.Fatalf("number %q rejected", raw)
		}
	}
}

func TestCodingProviderCostUsesExactHalfEvenMicros(t *testing.T) {
	for raw, want := range map[string]int64{
		"0": 0, "0.0000005": 0, "0.0000015": 2, "0.001234": 1234, "100": 100_000_000,
	} {
		got, ok := codingFloatToMicros(json.Number(raw))
		if !ok || got != want {
			t.Fatalf("cost %s = %d ok=%v, want %d", raw, got, ok, want)
		}
	}
	for _, raw := range []string{"-0", "-1", "100.000001", "0e101", strings.Repeat("9", 65)} {
		if got, ok := codingFloatToMicros(json.Number(raw)); ok {
			t.Fatalf("invalid cost %s accepted as %d", raw, got)
		}
	}
}

func TestCodingDispatchAuthorityRejectsUUIDAliases(t *testing.T) {
	body := codingDispatchFixture(t)
	var value map[string]any
	if err := json.Unmarshal(body, &value); err != nil {
		t.Fatal(err)
	}
	value["request_id"] = "{" + uuid.MustParse(value["request_id"].(string)).String() + "}"
	mutated, _ := json.Marshal(value)
	if _, _, err := parseCodingDispatch(mutated); err == nil {
		t.Fatal("noncanonical UUID alias was accepted")
	}
}
