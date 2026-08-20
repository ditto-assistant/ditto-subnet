package codingcertifier

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func TestHTTPHarnessClientAcceptsForwardCompatibleKnownFields(t *testing.T) {
	seed := fixtureSeed(t)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/coding/health":
			_, _ = response.Write([]byte(`{"status":"ok","supported_coding_contract_versions":[1],"capabilities":["scoped_memory_seed_v1","coding_runner_tools_v1","case_scoped_inference_v1"],"future":true}`))
		case "/coding/seed":
			_, _ = response.Write([]byte(`{"case_id":"case-cert-001","profile_capability_id":"profile-cert-001","memory_bundle_sha256":"` + seed.MemoryBundleSHA256 + `","memory_count":0,"idempotent_replay":false,"future":true}`))
		case "/coding/run":
			_, _ = response.Write([]byte(`{"case_id":"case-cert-001","final_report":{"summary":"done","remaining_risks":[]},"future":true}`))
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()
	client, err := NewHTTPHarnessClient(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	health, err := client.Health(t.Context())
	if err != nil || !health.supportsCodingV1() {
		t.Fatalf("health=%#v err=%v", health, err)
	}
	seedResponse, err := client.Seed(t.Context(), seed)
	if err != nil || seedResponse.CaseID != seed.CaseID {
		t.Fatalf("seed=%#v err=%v", seedResponse, err)
	}
	run := fixtureRunRequest()
	response, err := client.Run(t.Context(), run)
	if err != nil || response.CaseID != run.CaseID {
		t.Fatalf("run=%#v err=%v", response, err)
	}
}

func TestHTTPHarnessClientDoesNotFollowRedirects(t *testing.T) {
	targetCalls := 0
	target := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		targetCalls++
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"status":"ok","supported_coding_contract_versions":[1],"capabilities":[]}`))
	}))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		http.Redirect(response, request, target.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	client, err := NewHTTPHarnessClient(redirect.URL, redirect.Client())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Health(t.Context()); err == nil || targetCalls != 0 {
		t.Fatalf("redirect was followed: calls=%d err=%v", targetCalls, err)
	}
}

func TestHTTPHarnessClientClassifiesAbsentEndpointAndRejectsTrailingJSON(t *testing.T) {
	t.Run("absent", func(t *testing.T) {
		server := httptest.NewServer(http.NotFoundHandler())
		defer server.Close()
		client, err := NewHTTPHarnessClient(server.URL, server.Client())
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.Health(t.Context()); !errors.Is(err, ErrCodingUnsupported) {
			t.Fatalf("expected unsupported, got %v", err)
		}
	})
	t.Run("trailing", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
			response.Header().Set("Content-Type", "application/json")
			_, _ = response.Write([]byte(`{"status":"ok","supported_coding_contract_versions":[1],"capabilities":[]} {}`))
		}))
		defer server.Close()
		client, err := NewHTTPHarnessClient(server.URL, server.Client())
		if err != nil {
			t.Fatal(err)
		}
		if _, err := client.Health(t.Context()); err == nil || !strings.Contains(err.Error(), "trailing") {
			t.Fatalf("expected trailing-content failure, got %v", err)
		}
	})
}

func TestHTTPHarnessClientRejectsDuplicateKnownFields(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"status":"ok","status":"ok","supported_coding_contract_versions":[1],"capabilities":[]}`))
	}))
	defer server.Close()
	client, err := NewHTTPHarnessClient(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.Health(t.Context()); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected duplicate-field failure, got %v", err)
	}
}

func fixtureSeed(t *testing.T) codingcontract.SeedRequest {
	t.Helper()
	request := codingcontract.SeedRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              "ticket-cert-001", CaseID: "case-cert-001", ProfileCapabilityID: "profile-cert-001",
		Memories: []codingcontract.VisibleMemory{}, MemoryBundleSHA256: canonicalMemoryDigest(t, []codingcontract.VisibleMemory{}),
	}
	if err := request.Validate(); err != nil {
		t.Fatal(err)
	}
	return request
}

func fixtureRunRequest() codingcontract.RunRequest {
	return codingcontract.RunRequest{
		CodingContractVersion: codingcontract.ContractVersion,
		TicketID:              "ticket-cert-001", CaseID: "case-cert-001", ProfileCapabilityID: "profile-cert-001",
		RepositoryEpoch: "repository-epoch-001", VisibleBundleSHA256: strings.Repeat("1", 64),
		Issue:                  codingcontract.Issue{Title: "Fix normalize", Description: "Normalize trailing whitespace.", Constraints: []string{}},
		RuntimePolicy:          codingcontract.RuntimePolicy{EditablePaths: []string{"app.py"}, TestCommandIDs: []string{}, BuildCommandIDs: []string{}},
		WorkspaceCapabilityURL: "http://workspace.invalid/capability",
		InferenceBaseURL:       "http://inference.invalid/capability",
		Budgets:                codingcontract.Budgets{ModelInputTokens: 1_000, ModelOutputTokens: 1_000, WorkspaceToolCalls: 16, WallTimeSeconds: 60},
	}
}

func canonicalMemoryDigest(t *testing.T, memories []codingcontract.VisibleMemory) string {
	t.Helper()
	var buffer strings.Builder
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(struct {
		Memories []codingcontract.VisibleMemory `json:"memories"`
	}{Memories: memories}); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte(buffer.String()))
	return hex.EncodeToString(digest[:])
}
