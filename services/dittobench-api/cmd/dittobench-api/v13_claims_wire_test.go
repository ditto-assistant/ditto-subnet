package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV13ClaimsNeverReachHarnessWire drives the real runner against a capturing
// harness and asserts the v13 grader-only answer specification never crosses
// the wire: the /run body for a v13 program case carries only the question
// (no claims, expected answer, accept set, distractors, twin group, bait tool,
// or forbidden value), and the /seed body carries only pairs.
func TestV13ClaimsNeverReachHarnessWire(t *testing.T) {
	cases, pairs, err := gen.V13BusinessProgramCases(11, 28)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	bodies := map[string][]byte{}
	runBodies := map[string]string{} // wire case_id -> /run body
	harness := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies[r.URL.Path] = append(bodies[r.URL.Path], body...)
		if r.URL.Path == "/run" {
			var req protocol.RunRequest
			_ = json.Unmarshal(body, &req)
			runBodies[req.CaseID] = string(body)
		}
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/seed":
			_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: 1})
		default:
			_ = json.NewEncoder(w).Encode(protocol.RunResponse{FinalText: "ok", ToolCalls: []protocol.ObservedToolCall{}})
		}
	}))
	defer harness.Close()

	// The capturing harness is loopback-bound like a validator sandbox, so it is
	// reached through the sandbox client rather than the caller-URL SSRF guard.
	ctx := runner.TrustSandbox(context.Background())
	if _, err := runner.SeedForVersion(ctx, harness.URL, protocol.SeedRequest{UserID: gen.PrimaryUser, Pairs: pairs}, protocol.BenchVersionV13); err != nil {
		t.Fatalf("seed: %v", err)
	}
	for _, sc := range cases {
		mc := sc.Case
		if len(mc.Claims) == 0 {
			t.Fatalf("case %s has no claims to protect", mc.ID)
		}
		if _, err := runner.RunCase(ctx, harness.URL, mc.ID, mc.Question, nil, runner.CaseOptions{BenchVersion: protocol.BenchVersionV13, UserID: gen.PrimaryUser}); err != nil {
			t.Fatalf("run %s: %v", mc.ID, err)
		}
	}

	mu.Lock()
	defer mu.Unlock()
	for _, path := range []string{"/run", "/seed"} {
		lower := strings.ToLower(string(bodies[path]))
		if lower == "" {
			t.Fatalf("no %s body captured", path)
		}
		for _, key := range []string{`"claims"`, `"expected_answer"`, `"accept_any"`, `"answer_items"`, `"answer_item_accept_any"`, `"distractor_answers"`, `"twin_group"`, `"bait_tool"`, `"forbidden_answer"`, `"question_type"`, `"v10_provenance"`, `"relation"`} {
			if strings.Contains(lower, key) {
				t.Fatalf("%s body carries grader-only key %s", path, key)
			}
		}
	}
	// The graded values themselves are absent from each case's /run body: the
	// only case content on the wire is the question. (The generic conflict
	// marker cluster — "disagree", "conflict" — is ordinary English a question
	// may legitimately use, so it is not a value to hide.)
	for _, sc := range cases {
		body, ok := runBodies[sc.Case.ID]
		if !ok {
			t.Fatalf("no /run body captured for case %s", sc.Case.ID)
		}
		lower := strings.ToLower(body)
		question := strings.ToLower(sc.Case.Question)
		for _, claim := range sc.Case.Claims {
			if claim.Kind == protocol.ClaimKindConflict {
				continue
			}
			for _, accept := range append([]string{claim.Expected}, claim.Accept...) {
				if strings.Contains(lower, strings.ToLower(accept)) && !strings.Contains(question, strings.ToLower(accept)) {
					t.Fatalf("/run body for case %s carries the graded value %q: %s", sc.Case.ID, accept, body)
				}
			}
		}
	}
}
