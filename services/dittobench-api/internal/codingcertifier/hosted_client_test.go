package codingcertifier

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func hostedRequests(t *testing.T) (codingcontract.HostedSeedRequest, codingcontract.HostedRunRequest) {
	seed := codingcontract.HostedSeedRequest(fixtureSeed(t))
	seed.CodingContractVersion = 2
	seed.TicketID, seed.CaseID = "10000000-0000-4000-8000-000000000001", "20000000-0000-4000-8000-000000000002"
	run := codingcontract.HostedRunRequest(fixtureRunRequest())
	run.CodingContractVersion = 2
	run.TicketID, run.CaseID = seed.TicketID, seed.CaseID
	return seed, run
}

func TestHostedClientUsesV2AndLeavesLegacyValidationUnchanged(t *testing.T) {
	seed, run := hostedRequests(t)
	seedBytes, err := codingcontract.CanonicalJSON(seed)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := codingcontract.ParseHostedSeedRequest(seedBytes); err != nil {
		t.Fatal(err)
	}
	if _, err := codingcontract.ParseSeedRequest(seedBytes); err == nil {
		t.Fatal("legacy parser accepted native seed")
	}
	runBytes, err := codingcontract.CanonicalJSON(run)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := codingcontract.ParseHostedRunRequest(runBytes); err != nil {
		t.Fatal(err)
	}
	if _, err := codingcontract.ParseRunRequest(runBytes); err == nil {
		t.Fatal("legacy parser accepted native run")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/coding/health":
			_ = json.NewEncoder(w).Encode(HealthResponse{Status: "ok", SupportedCodingContractVersions: []int{1, 2}, Capabilities: []string{"scoped_memory_seed_v2", "coding_runner_tools_v2", "case_scoped_inference_v2"}})
		case "/coding/seed":
			var body codingcontract.HostedSeedRequest
			if json.NewDecoder(r.Body).Decode(&body) != nil || body.Validate() != nil {
				t.Error("invalid native seed")
			}
			_ = json.NewEncoder(w).Encode(SeedResponse{CaseID: body.CaseID, ProfileCapabilityID: body.ProfileCapabilityID, MemoryBundleSHA256: body.MemoryBundleSHA256, MemoryCount: len(body.Memories)})
		case "/coding/run":
			var body codingcontract.HostedRunRequest
			if json.NewDecoder(r.Body).Decode(&body) != nil || body.Validate() != nil {
				t.Error("invalid native run")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"case_id": body.CaseID, "final_report": map[string]any{"summary": "done", "remaining_risks": []string{}}})
		}
	}))
	defer server.Close()
	client, err := NewHostedHTTPHarnessClient(server.URL, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	if health, err := client.Health(t.Context()); err != nil || !health.SupportsCodingV2() {
		t.Fatal("native health failed")
	}
	if _, err := client.Seed(t.Context(), seed); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Run(t.Context(), run); err != nil {
		t.Fatal(err)
	}
	if codingcontract.SeedRequest(seed).Validate() == nil || codingcontract.RunRequest(run).Validate() == nil {
		t.Fatal("legacy validators accepted v2")
	}
	seed.CodingContractVersion = 1
	if _, err := client.Seed(t.Context(), seed); err == nil {
		t.Fatal("native client accepted v1")
	}
}

func TestHostedClientRequiresCompleteSeedAcknowledgementAndAuthorizedTransport(t *testing.T) {
	var missing *http.Transport
	if _, err := NewHostedHTTPHarnessClient("https://example.invalid", &http.Client{Transport: missing}); err == nil {
		t.Fatal("typed nil transport accepted")
	}
	if _, err := NewHostedHTTPHarnessClient("https://example.invalid", &http.Client{}); err == nil {
		t.Fatal("implicit transport accepted")
	}
	if _, err := NewHostedHTTPHarnessClient("https://example.invalid", &http.Client{Transport: http.DefaultTransport}); err == nil {
		t.Fatal("ambient proxy transport accepted")
	}
	seed, _ := hostedRequests(t)
	for _, bad := range []string{"missing_count", "null_count", "missing_replay", "wrong_identity", "pending"} {
		t.Run(bad, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				body := map[string]any{"case_id": seed.CaseID, "profile_capability_id": seed.ProfileCapabilityID, "memory_bundle_sha256": seed.MemoryBundleSHA256, "memory_count": 0, "idempotent_replay": false}
				switch bad {
				case "missing_count":
					delete(body, "memory_count")
				case "null_count":
					body["memory_count"] = nil
				case "missing_replay":
					delete(body, "idempotent_replay")
				case "wrong_identity":
					body["case_id"] = "wrong"
				case "pending":
					w.WriteHeader(http.StatusAccepted)
				}
				_ = json.NewEncoder(w).Encode(body)
			}))
			defer server.Close()
			client, err := NewHostedHTTPHarnessClient(server.URL, server.Client())
			if err != nil {
				t.Fatal(err)
			}
			if _, err := client.Seed(t.Context(), seed); err == nil {
				t.Fatal("invalid acknowledgement accepted")
			}
		})
	}
}
