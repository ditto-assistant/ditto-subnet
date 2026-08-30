package codingcanary

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
)

const testToken = "coding-canary-control-token-00000001"

type stubBackend struct {
	outcome Outcome
	err     error
}

func (stub stubBackend) Certify(context.Context, Request) (Outcome, error) {
	return stub.outcome, stub.err
}

func TestHandlerRejectsUnauthorizedAndInvalidRequests(t *testing.T) {
	t.Parallel()
	service, err := New(Config{
		ControlToken: testToken,
		Backend: stubBackend{outcome: Outcome{
			LeaseID:             "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
			CapabilitiesRevoked: true, HarnessDestroyed: true,
		}},
		Now: func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(service.Handler())
	t.Cleanup(server.Close)

	unauthorized, err := http.Post(server.URL+"/v1/coding/certifier/canary", "application/json", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	unauthorized.Body.Close()
	if unauthorized.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d", unauthorized.StatusCode)
	}

	request, err := http.NewRequest(http.MethodPost, server.URL+"/v1/coding/certifier/canary", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+testToken)
	request.Header.Set("Content-Type", "application/json")
	invalid, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	invalid.Body.Close()
	if invalid.StatusCode != http.StatusBadRequest {
		t.Fatalf("status=%d", invalid.StatusCode)
	}
}

func TestHandlerRequiresRevokedDestroyedCanaryResult(t *testing.T) {
	t.Parallel()
	leaseID := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	service, err := New(Config{
		ControlToken: testToken,
		Backend: stubBackend{outcome: Outcome{
			LeaseID: leaseID, CapabilitiesRevoked: true, HarnessDestroyed: true,
			Receipt: codingcertifier.Receipt{Schema: codingcertifier.CertificationSchema, WeightEligible: false},
		}},
		Now: func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(service.Handler())
	t.Cleanup(server.Close)

	payload := map[string]any{
		"schema": RequestSchema, "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"lease_id": leaseID, "deadline": "2026-08-30T18:20:00Z",
		"agent_id":                  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
		"agent_artifact_sha256":     strings.Repeat("a", 64),
		"screened_image_sha256":     strings.Repeat("b", 64),
		"screened_image_id":         "sha256:" + strings.Repeat("c", 64),
		"screened_image_ref":        "ditto-screen/cccccccc-cccc-4ccc-8ccc-cccccccccccc:latest",
		"screened_image_upload_id":  "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
		"screened_image_size_bytes": 1024,
		"screening_policy_version":  9,
		"image_url":                 "https://storage.invalid/image.tar?signature=synthetic",
		"image_expires_at":          "2026-08-30T18:04:00Z",
		"bench_version":             12,
		"canary_manifest_sha256":    strings.Repeat("d", 64),
		"runner_plan_sha256":        strings.Repeat("e", 64),
		"grader_plan_sha256":        strings.Repeat("f", 64),
		"resource_profile_sha256":   strings.Repeat("1", 64),
		"inference_policy_sha256":   strings.Repeat("2", 64),
		"coding_contract_version":   1, "weight_eligible": false,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequest(http.MethodPost, server.URL+"/v1/coding/certifier/canary", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+testToken)
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(response.Body)
		t.Fatalf("status=%d body=%s", response.StatusCode, raw)
	}
	var decoded Response
	if err := json.NewDecoder(response.Body).Decode(&decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Schema != ResponseSchema || !decoded.CapabilitiesRevoked || !decoded.HarnessDestroyed || decoded.LeaseID != leaseID {
		t.Fatalf("decoded=%+v", decoded)
	}
}

func TestHandlerReportsUnavailableWhenBackendIsMissing(t *testing.T) {
	t.Parallel()
	leaseID := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	service, err := New(Config{
		ControlToken: testToken,
		Backend: stubBackend{outcome: Outcome{
			LeaseID: leaseID, CapabilitiesRevoked: true, HarnessDestroyed: true,
		}},
		Now: func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	service.mu.Lock()
	service.backend = nil
	service.mu.Unlock()
	response := postCanary(t, service, leaseID)
	defer response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status=%d", response.StatusCode)
	}
	assertErrorCode(t, response, "unavailable")
}

func TestHandlerReportsConflictWhenLeaseIsActive(t *testing.T) {
	t.Parallel()
	leaseID := "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
	service, err := New(Config{
		ControlToken: testToken,
		Backend: stubBackend{outcome: Outcome{
			LeaseID: leaseID, CapabilitiesRevoked: true, HarnessDestroyed: true,
		}},
		Now: func() time.Time { return time.Date(2026, 8, 30, 18, 0, 0, 0, time.UTC) },
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := service.begin(leaseID); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { service.release(leaseID) })
	response := postCanary(t, service, leaseID)
	defer response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("status=%d", response.StatusCode)
	}
	assertErrorCode(t, response, "conflict")
}

func postCanary(t *testing.T, service *Service, leaseID string) *http.Response {
	t.Helper()
	server := httptest.NewServer(service.Handler())
	t.Cleanup(server.Close)
	payload := map[string]any{
		"schema": RequestSchema, "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		"lease_id": leaseID, "deadline": "2026-08-30T18:20:00Z",
		"agent_id":                  "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
		"agent_artifact_sha256":     strings.Repeat("a", 64),
		"screened_image_sha256":     strings.Repeat("b", 64),
		"screened_image_id":         "sha256:" + strings.Repeat("c", 64),
		"screened_image_ref":        "ditto-screen/cccccccc-cccc-4ccc-8ccc-cccccccccccc:latest",
		"screened_image_upload_id":  "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
		"screened_image_size_bytes": 1024,
		"screening_policy_version":  9,
		"image_url":                 "https://storage.invalid/image.tar?signature=synthetic",
		"image_expires_at":          "2026-08-30T18:04:00Z",
		"bench_version":             12,
		"canary_manifest_sha256":    strings.Repeat("d", 64),
		"runner_plan_sha256":        strings.Repeat("e", 64),
		"grader_plan_sha256":        strings.Repeat("f", 64),
		"resource_profile_sha256":   strings.Repeat("1", 64),
		"inference_policy_sha256":   strings.Repeat("2", 64),
		"coding_contract_version":   1, "weight_eligible": false,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequest(http.MethodPost, server.URL+"/v1/coding/certifier/canary", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+testToken)
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func assertErrorCode(t *testing.T, response *http.Response, want string) {
	t.Helper()
	if response.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("cache-control=%q", response.Header.Get("Cache-Control"))
	}
	var payload map[string]string
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["error"] != want {
		t.Fatalf("error=%q", payload["error"])
	}
}
