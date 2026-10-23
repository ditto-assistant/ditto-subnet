package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

// oracleHarness answers every pack question with its gold answer.
func oracleHarness(t *testing.T, p challenge.Pack) *httptest.Server {
	t.Helper()
	answers := map[string]string{}
	for _, c := range p.Cases {
		a := c.ExpectedAnswer
		if len(c.AnswerItems) > 0 {
			a = strings.Join(c.AnswerItems, " and ")
		}
		answers[c.Question] = a
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/health":
		case "/seed":
			var req protocol.SeedRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: len(req.Pairs)})
		case "/run":
			var req protocol.RunRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			a := answers[req.UserInput]
			_ = json.NewEncoder(w).Encode(protocol.RunResponse{Answer: a, FinalText: a})
		}
	}))
}

func TestParseAgentFlag(t *testing.T) {
	got, err := parseAgentFlag("a1=http://h:1/,owner=o9,champion")
	if err != nil || got.ID != "a1" || got.URL != "http://h:1" || got.Owner != "o9" || !got.Champion {
		t.Fatalf("%+v %v", got, err)
	}
	if _, err := parseAgentFlag("nourl"); err == nil {
		t.Fatal("missing url accepted")
	}
	if _, err := parseAgentFlag("a=http://h,bogus"); err == nil {
		t.Fatal("unknown option accepted")
	}
}

func TestProcessOneLeasesRunsAndPosts(t *testing.T) {
	runner.Configure(true)
	pack := challenge.ReferencePack(12, testHotkey)
	raw, _ := json.Marshal(pack)
	sha := challenge.Digest(raw)
	harness := oracleHarness(t, pack)
	defer harness.Close()

	var posted resultRequest
	served := false
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer secret-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/v1/generator-scorer/work":
			if served {
				_ = json.NewEncoder(w).Encode(workResponse{})
				return
			}
			served = true
			_ = json.NewEncoder(w).Encode(workResponse{Work: &workItem{
				LeaseID: "lease-1", PackID: "pack-1", PackSHA256: sha, PackBase64: base64.StdEncoding.EncodeToString(raw),
				Panel: []panelSlot{
					{AgentID: "a1", OwnerKey: "o1", Champion: true},
					{AgentID: "a2", OwnerKey: "o2"},
					{AgentID: "unprovisioned", OwnerKey: "o3"},
				},
			}})
		case "/api/v1/generator-scorer/results":
			_ = json.NewDecoder(r.Body).Decode(&posted)
		default:
			http.NotFound(w, r)
		}
	}))
	defer platform.Close()

	client := &platformClient{base: platform.URL, token: "secret-token", http: platform.Client()}
	harnesses := map[string]string{"a1": harness.URL, "a2": harness.URL}
	processed, err := processOne(context.Background(), client, harnesses, 2)
	if err != nil || !processed {
		t.Fatalf("processed=%v err=%v", processed, err)
	}
	if !posted.Validation.Valid || posted.PackSHA256 != sha || posted.LeaseID != "lease-1" || posted.ScorerVersion != ScorerVersion {
		t.Fatalf("posted %+v", posted)
	}
	if len(posted.Panel) != 2 {
		t.Fatalf("panel %d, want the two provisioned agents", len(posted.Panel))
	}
	for _, m := range posted.Panel {
		if len(m.Results) != len(pack.Cases) {
			t.Fatalf("%s: %d results", m.AgentID, len(m.Results))
		}
		for _, r := range m.Results {
			if r.Fault || r.Score != 1 {
				t.Fatalf("%s/%s: %+v", m.AgentID, r.CaseID, r)
			}
		}
	}
	// Nothing left: the next cycle is idle.
	processed, err = processOne(context.Background(), client, harnesses, 2)
	if processed || err != nil {
		t.Fatalf("idle cycle processed=%v err=%v", processed, err)
	}
}

func TestProcessOneRefusesMismatchedPin(t *testing.T) {
	runner.Configure(true)
	pack := challenge.ReferencePack(12, testHotkey)
	raw, _ := json.Marshal(pack)
	posted := false
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/generator-scorer/results" {
			posted = true
			return
		}
		_ = json.NewEncoder(w).Encode(workResponse{Work: &workItem{
			LeaseID: "l", PackID: "p", PackSHA256: strings.Repeat("0", 64), PackBase64: base64.StdEncoding.EncodeToString(raw),
		}})
	}))
	defer platform.Close()
	client := &platformClient{base: platform.URL, token: "t", http: platform.Client()}
	if _, err := processOne(context.Background(), client, nil, 1); err == nil || posted {
		t.Fatalf("mismatched pin scored: err=%v posted=%v", err, posted)
	}
}

func TestProcessOneReportsInvalidPacksWithoutRunning(t *testing.T) {
	pack := challenge.ReferencePack(12, testHotkey)
	pack.Cases = pack.Cases[:3]
	raw, _ := json.Marshal(pack)
	sha := challenge.Digest(raw)
	var posted resultRequest
	platform := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/generator-scorer/results" {
			_ = json.NewDecoder(r.Body).Decode(&posted)
			return
		}
		_ = json.NewEncoder(w).Encode(workResponse{Work: &workItem{
			LeaseID: "l", PackID: "p", PackSHA256: sha, PackBase64: base64.StdEncoding.EncodeToString(raw),
			Panel: []panelSlot{{AgentID: "a1", OwnerKey: "o1"}},
		}})
	}))
	defer platform.Close()
	client := &platformClient{base: platform.URL, token: "t", http: platform.Client()}
	if _, err := processOne(context.Background(), client, map[string]string{"a1": "http://127.0.0.1:1"}, 1); err != nil {
		t.Fatal(err)
	}
	if posted.Validation.Valid || len(posted.Validation.Issues) == 0 || len(posted.Panel) != 0 {
		t.Fatalf("posted %+v", posted)
	}
}
