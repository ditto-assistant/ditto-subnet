package privatesurface

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func TestFactRendererBoundedStructuralRetry(t *testing.T) {
	for _, mode := range []string{"recover", "exhaust", "audit-failure", "identity"} {
		t.Run(mode, func(t *testing.T) {
			calls := 0
			var audits []FactRenderAudit
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				calls++
				var body map[string]any
				_ = json.NewDecoder(req.Body).Decode(&body)
				if calls == 2 {
					user := body["messages"].([]any)[1].(map[string]any)["content"].(string)
					if !strings.Contains(user, "structural_feedback") || !strings.Contains(user, "{{value0}}") {
						t.Error("missing targeted feedback")
					}
				}
				p := universe.V13FactRenderPlan{Records: [3]string{"omitted", "{{value1}}", "{{value2}}"}, Question: "{{subject0}}?"}
				if mode == "recover" && calls == 2 {
					p.Records[0] = "{{value0}}"
				}
				plan, _ := json.Marshal(p)
				content, _ := json.Marshal(map[string]json.RawMessage{"plan": plan})
				provider := "Azure"
				if mode == "identity" {
					provider = "wrong"
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": "openai/gpt-4.1", "provider": provider, "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": string(content)}}}, "usage": map[string]any{"cost": 0.001}})
			}))
			defer s.Close()
			p := Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
			r, err := NewFactRenderer(p, "test", 4, func(BudgetSnapshot) error { return nil }, func(a FactRenderAudit) error {
				audits = append(audits, a)
				if mode == "audit-failure" {
					return errors.New("disk failed")
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			r.client.url = s.URL
			_, err = r.Plan(context.Background(), factRequestFixture())
			if (err == nil) != (mode == "recover") {
				t.Fatal("incorrect retry outcome")
			}
			want := 2
			if mode == "audit-failure" || mode == "identity" {
				want = 1
			}
			if calls != want || len(audits) != want || audits[0].Accepted {
				t.Fatal("retry bound or audit violated")
			}
			if mode == "recover" && (!audits[1].Accepted || audits[1].Attempt != 2) {
				t.Fatal("retry success not audited")
			}
		})
	}
}

func factRequestFixture() universe.V13FactRenderRequest {
	r := universe.V13FactRenderRequest{Revision: "test", Subject: "{{subject0}}", Bindings: map[string]string{"{{subject0}}": "the task", "{{value0}}": "Ada", "{{value1}}": "Bea", "{{value2}}": "Tuesday"}, QuestionAllowed: []string{"{{subject0}}"}}
	for i, token := range []string{"{{value0}}", "{{value1}}", "{{value2}}"} {
		r.Required[i] = []string{token}
		r.Allowed[i] = []string{token}
	}
	return r
}

func TestFactRendererDirectFactsAndIndependentCheck(t *testing.T) {
	request := factRequestFixture()
	plan := universe.V13FactRenderPlan{Records: [3]string{"Initially {{value0}}.", "Now {{value1}}.", "Review {{value2}}."}, Question: "Who owns {{subject0}}?"}
	rawPlan, _ := json.Marshal(plan)
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls++
		var body map[string]any
		if json.NewDecoder(req.Body).Decode(&body) != nil {
			t.Error("invalid request")
		}
		model := body["model"].(string)
		provider := "Azure"
		var content []byte
		if model == "openai/gpt-4.1" {
			schema := body["response_format"].(map[string]any)["json_schema"].(map[string]any)["schema"].(map[string]any)
			planSchema := schema["properties"].(map[string]any)["plan"].(map[string]any)
			if planSchema["type"] != "object" || planSchema["additionalProperties"] != false {
				t.Error("author schema is not a strict direct object")
			}
			records := planSchema["properties"].(map[string]any)["records"].(map[string]any)
			if records["minItems"] != float64(3) || records["maxItems"] != float64(3) {
				t.Error("record cardinality not pinned")
			}
			user := body["messages"].([]any)[1].(map[string]any)["content"].(string)
			if !strings.Contains(user, "record_required_tokens") || strings.Contains(user, "reference_text") {
				t.Error("not structured-fact input")
			}
			if strings.Contains(user, "Ada") || strings.Contains(user, "Tuesday") {
				t.Error("author received concrete bindings")
			}
			content, _ = json.Marshal(map[string]json.RawMessage{"plan": rawPlan})
		} else {
			schema := body["response_format"].(map[string]any)["json_schema"].(map[string]any)["schema"].(map[string]any)
			verdictSchema := schema["properties"].(map[string]any)["verdict"].(map[string]any)
			if verdictSchema["type"] != "object" {
				t.Error("verdict is double-encoded")
			}
			if model != "google/gemini-2.5-flash" {
				t.Error("wrong independent model")
			}
			provider = "Google"
			content, _ = json.Marshal(map[string]json.RawMessage{"verdict": json.RawMessage(`{"accepted":true,"reason":"All facts and query preserved."}`)})
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": model, "provider": provider, "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": string(content)}}}, "usage": map[string]any{"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 10}})
	}))
	defer server.Close()
	var audits []FactRenderAudit
	var budget BudgetSnapshot
	profile := Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
	r, err := NewFactRenderer(profile, "test-key", 4, func(s BudgetSnapshot) error { budget = s; return nil }, func(a FactRenderAudit) error { audits = append(audits, a); return nil })
	if err != nil {
		t.Fatal(err)
	}
	r.client.url = server.URL
	got, err := r.Plan(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	bound, err := universe.BindV13FactRenderPlan(request, got)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Check(context.Background(), request, bound); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || budget.Requests != 2 || len(audits) != 2 || !audits[0].Accepted || !audits[1].Accepted {
		t.Fatal("missing receipts or independent check")
	}
	if bound.Records[0] != "Initially Ada." {
		t.Fatal("not fact-bound")
	}
}

func TestFactRendererIdentityAndSemanticFailures(t *testing.T) {
	for _, mode := range []string{"identity", "semantic"} {
		t.Run(mode, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				calls++
				provider := "Google"
				if mode == "identity" {
					provider = "WrongProvider"
				}
				content, _ := json.Marshal(map[string]json.RawMessage{"verdict": json.RawMessage(`{"accepted":false,"reason":"A required assertion is missing."}`)})
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": "google/gemini-2.5-flash", "provider": provider, "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": string(content)}}}, "usage": map[string]any{"cost": 0.001}})
			}))
			defer server.Close()
			p := Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}
			var audits []FactRenderAudit
			r, err := NewFactRenderer(p, "test", 4, func(BudgetSnapshot) error { return nil }, func(a FactRenderAudit) error { audits = append(audits, a); return nil })
			if err != nil {
				t.Fatal(err)
			}
			r.client.url = server.URL
			if err := r.Check(context.Background(), factRequestFixture(), universe.V13FactRenderPlan{}); err == nil {
				t.Fatal("rejection ignored")
			}
			if calls != 1 {
				t.Fatal("unexpected retry")
			}
			if len(audits) != 1 || audits[0].Accepted || audits[0].Failure == "" || audits[0].Receipt.Model != "google/gemini-2.5-flash" {
				t.Fatal("rejected completion receipt was not retained")
			}
		})
	}
}

func TestFactVerdictStrict(t *testing.T) {
	for _, inner := range []string{`{"reason":"missing decision"}`, `{"accepted":null,"reason":"null"}`, `{"accepted":true,"reason":""}`, `{"accepted":true,"reason":"ok","extra":1}`, `{"accepted":true,"reason":"ok"} {}`} {
		raw := []byte(`{"verdict":` + inner + `}`)
		if _, _, err := decodeFactVerdict(raw); err == nil {
			t.Fatalf("malformed verdict accepted: %s", inner)
		}
	}
	for _, accepted := range []bool{true, false} {
		inner, _ := json.Marshal(map[string]any{"accepted": accepted, "reason": "Explicit explanation."})
		raw, _ := json.Marshal(map[string]json.RawMessage{"verdict": inner})
		got, reason, err := decodeFactVerdict(raw)
		if err != nil || got != accepted || reason == "" {
			t.Fatal("valid verdict lost")
		}
	}
}

func TestFactCheckerReceivesResolvedAssertions(t *testing.T) {
	r := factRequestFixture()
	r.SubjectEntity = "{{value0}}"
	r.Facts = []universe.V13RenderAssertion{{Entity: "{{value0}}", Field: "{{subject0}}", Value: "{{value1}}", Date: "{{value2}}"}}
	r.Query = []universe.V13RenderQuery{{Op: "read", Field: "{{subject0}}"}}
	raw, err := json.Marshal(resolvedFactCheckTruth(r))
	if err != nil || strings.Contains(string(raw), "{{") || !strings.Contains(string(raw), "Ada") || !strings.Contains(string(raw), "Bea") {
		t.Fatal("unresolved or missing facts")
	}
	if r.Facts[0].Entity != "{{value0}}" {
		t.Fatal("checker input mutated author authority")
	}
}
