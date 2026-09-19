package privatesurface

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

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
			user := body["messages"].([]any)[1].(map[string]any)["content"].(string)
			if !strings.Contains(user, "record_required_tokens") || strings.Contains(user, "reference_text") {
				t.Error("not structured-fact input")
			}
			if strings.Contains(user, "Ada") || strings.Contains(user, "Tuesday") {
				t.Error("author received concrete bindings")
			}
			content, _ = json.Marshal(map[string]string{"text": string(rawPlan)})
		} else {
			if model != "google/gemini-2.5-flash" {
				t.Error("wrong independent model")
			}
			provider = "Google"
			content = []byte("{\"accepted\":true}")
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
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": "google/gemini-2.5-flash", "provider": provider, "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": "{\"accepted\":false}"}}}, "usage": map[string]any{"cost": 0.001}})
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
