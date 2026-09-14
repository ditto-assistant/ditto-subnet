package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v13ClaimFamily is one v13 case-family generator's output as staged for the
// memory suite: the cases whose grader-only specification must stay off the
// wire, and the evidence pairs the /seed body may carry.
type v13ClaimFamily struct {
	name  string
	cases []gen.StagedCase
	pairs []protocol.MemoryPair
}

// v13ClaimFamilies stages every v13 family generator for one seed: business
// programs, personal programs, family compiler v2, and the injection tail
// (which additionally carries BaitTool / ForbiddenAnswer). The suite budget
// that wires them in lands with the envelope; here each is driven directly.
func v13ClaimFamilies(t *testing.T, seed int64) []v13ClaimFamily {
	t.Helper()
	business, businessPairs, err := gen.V13BusinessProgramCases(seed, 28)
	if err != nil {
		t.Fatal(err)
	}
	personal, personalPairs, err := gen.GenerateV13PersonalPrograms(seed, 24)
	if err != nil {
		t.Fatal(err)
	}
	var compiler []gen.StagedCase
	var compilerPairs []protocol.MemoryPair
	for _, fc := range gen.BuildFamilyCompilerV13(seed, 16) {
		compiler = append(compiler, fc.Staged)
		compilerPairs = append(compilerPairs, fc.Pairs...)
	}
	// Scale 3 is the full-profile world (gen.v8WorldProfile(225)).
	injection, err := gen.BuildV13WorldInjection(seed, universe.Generate(seed, 3))
	if err != nil {
		t.Fatal(err)
	}
	return []v13ClaimFamily{
		{name: "business", cases: business, pairs: businessPairs},
		{name: "personal", cases: personal, pairs: personalPairs},
		{name: "family-compiler", cases: compiler, pairs: compilerPairs},
		{name: "injection", cases: injection.Cases, pairs: injection.Pairs},
	}
}

// TestV13ClaimsNeverReachHarnessWire drives the real runner against a capturing
// harness for every v13 case family and asserts the grader-only answer
// specification never crosses the wire: the /run body for a v13 case carries
// only the question (no claims, expected answer, accept set, distractors, twin
// group, bait tool, or forbidden value), and the /seed body carries only pairs.
func TestV13ClaimsNeverReachHarnessWire(t *testing.T) {
	for _, family := range v13ClaimFamilies(t, 11) {
		t.Run(family.name, func(t *testing.T) {
			assertV13FamilyOffTheWire(t, family)
		})
	}
}

func assertV13FamilyOffTheWire(t *testing.T, family v13ClaimFamily) {
	t.Helper()
	if len(family.cases) == 0 || len(family.pairs) == 0 {
		t.Fatalf("%s: %d cases / %d pairs staged", family.name, len(family.cases), len(family.pairs))
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
	if _, err := runner.SeedForVersion(ctx, harness.URL, protocol.SeedRequest{UserID: gen.PrimaryUser, Pairs: family.pairs}, protocol.BenchVersionV13); err != nil {
		t.Fatalf("seed: %v", err)
	}
	for _, sc := range family.cases {
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
		for _, key := range []string{`"claims"`, `"expected_answer"`, `"accept_any"`, `"answer_items"`, `"answer_item_accept_any"`, `"distractor_answers"`, `"twin_group"`, `"bait_tool"`, `"forbidden_answer"`, `"question_type"`, `"v10_provenance"`, `"relation"`, `"writing_protected"`} {
			if strings.Contains(lower, key) {
				t.Fatalf("%s body carries grader-only key %s", path, key)
			}
		}
	}
	// The graded values themselves are absent from each case's /run body: the
	// only case content on the wire is the question. The scan runs over the
	// prompt-bearing fields (system prompt, user input, tool definitions) so an
	// opaque case id or the user id cannot collide with a short numeric claim,
	// and matches whole words so a direction like "unchanged" is not found
	// inside an unrelated token. (The generic conflict marker cluster —
	// "disagree", "conflict" — is ordinary English a question may legitimately
	// use, so it is not a value to hide.)
	for _, sc := range family.cases {
		body, ok := runBodies[sc.Case.ID]
		if !ok {
			t.Fatalf("no /run body captured for case %s", sc.Case.ID)
		}
		var req protocol.RunRequest
		if err := json.Unmarshal([]byte(body), &req); err != nil {
			t.Fatalf("decode /run body for case %s: %v", sc.Case.ID, err)
		}
		tools, _ := json.Marshal(req.Tools)
		content := strings.ToLower(req.SystemPrompt + "\n" + req.UserInput + "\n" + string(tools))
		question := strings.ToLower(sc.Case.Question)
		values := []string{sc.Case.ExpectedAnswer, sc.Case.ForbiddenAnswer}
		values = append(values, sc.Case.AnswerItems...)
		for _, claim := range sc.Case.Claims {
			if claim.Kind == protocol.ClaimKindConflict {
				continue
			}
			values = append(values, claim.Expected)
			values = append(values, claim.Accept...)
		}
		for _, value := range values {
			value = strings.ToLower(strings.TrimSpace(value))
			if value == "" {
				continue
			}
			word := regexp.MustCompile(`(^|[^a-z0-9])` + regexp.QuoteMeta(value) + `($|[^a-z0-9])`)
			if word.MatchString(content) && !word.MatchString(question) {
				t.Fatalf("/run body for %s case %s carries the graded value %q: %s", family.name, sc.Case.ID, value, body)
			}
		}
	}
}
