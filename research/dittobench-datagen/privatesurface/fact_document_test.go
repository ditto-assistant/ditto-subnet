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

func TestFactDocumentTransportBoundaries(t *testing.T) {
	for _, mode := range []string{"success", "retry", "exhaust", "identity", "semantic", "checker-identity", "audit", "invalid-source"} {
		t.Run(mode, func(t *testing.T) {
			request := universe.V13FactDocumentRequest{Revision: universe.V13FactDocumentRevision, Domain: "story", Bindings: map[string]string{"{{owner0}}": "Ada", "{{owner1}}": "Bea"}, Records: []universe.V13FactDocumentRecord{
				{MinBytes: 1, MaxBytes: 200, Assertions: []universe.V13DocumentAssertion{{Kind: "owner", Relation: "initial", Arguments: map[string]string{"person": "{{owner0}}"}}}},
				{MinBytes: 1, MaxBytes: 200, Assertions: []universe.V13DocumentAssertion{{Kind: "owner", Relation: "supersedes initial", Arguments: map[string]string{"person": "{{owner1}}"}}}},
			}}
			calls, authors, checks := 0, 0, 0
			var audits []FactRenderAudit
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				calls++
				var body map[string]any
				if json.NewDecoder(req.Body).Decode(&body) != nil {
					t.Error("invalid request")
					return
				}
				model := body["model"].(string)
				user := body["messages"].([]any)[1].(map[string]any)["content"].(string)
				provider := "Azure"
				var content []byte
				if model == "openai/gpt-4.1" {
					authors++
					if strings.Contains(user, "Ada") || strings.Contains(user, "Bea") || strings.Contains(user, "reference_text") {
						t.Error("author received private bindings or prose")
					}
					schema := body["response_format"].(map[string]any)["json_schema"].(map[string]any)["schema"].(map[string]any)["properties"].(map[string]any)["plan"].(map[string]any)
					recs := schema["properties"].(map[string]any)["records"].(map[string]any)
					if recs["minItems"] != float64(1) || recs["maxItems"] != float64(1) || schema["additionalProperties"] != false {
						t.Error("document schema not exact")
					}
					plan := universe.V13FactDocumentPlan{Records: []string{"Initial owner: {{owner0}}."}}
					if strings.Contains(user, "{{owner1}}") {
						plan.Records[0] = "Final owner replacing initial: {{owner1}}."
						if strings.Contains(user, "{{owner0}}") {
							t.Error("author saw another record")
						}
					}
					if mode == "exhaust" || (mode == "retry" && authors == 1) {
						plan.Records[0] = "Missing."
					}
					if mode == "retry" && authors == 2 && !strings.Contains(user, "structural_feedback") {
						t.Error("missing retry feedback")
					}
					content, _ = json.Marshal(map[string]any{"plan": plan})
					if mode == "identity" {
						provider = "wrong"
					}
				} else {
					checks++
					if model != "google/gemini-2.5-flash" {
						t.Error("wrong checker")
					}
					provider = "Google"
					if !strings.Contains(user, "Ada") || !strings.Contains(user, "Bea") || strings.Contains(user, "{{owner") {
						t.Error("checker not comparing concrete facts")
					}
					if mode == "checker-identity" {
						provider = "wrong"
					}
					content, _ = json.Marshal(map[string]any{"verdict": map[string]any{"accepted": mode != "semantic", "reason": "fixture verdict"}})
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "fixture", "model": model, "provider": provider, "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": string(content)}}}, "usage": map[string]any{"cost": 0.001}})
			}))
			defer server.Close()
			r, err := NewFactRenderer(Profile{RewriteModel: "openai/gpt-4.1", RewriteProvider: "azure", ValidatorModel: "google/gemini-2.5-flash", ValidatorProvider: "google-vertex"}, "fixture", 4, func(BudgetSnapshot) error { return nil }, func(a FactRenderAudit) error {
				audits = append(audits, a)
				if mode == "audit" {
					return errors.New("disk failure")
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			r.client.url = server.URL
			if mode == "invalid-source" {
				request.Bindings = nil
			}
			got, err := universe.RenderV13FactDocument(context.Background(), request, r)
			wantSuccess := mode == "success" || mode == "retry"
			if (err == nil) != wantSuccess {
				t.Fatalf("unexpected outcome: %v", err)
			}
			if wantSuccess && (got.Records[0] != "Initial owner: Ada." || checks != 1) {
				t.Fatal("unchecked or incorrectly bound output")
			}
			if !wantSuccess && len(got.Records) != 0 {
				t.Fatal("partial output escaped")
			}
			wantCalls := map[string]int{"success": 3, "retry": 4, "exhaust": 2, "identity": 1, "semantic": 3, "checker-identity": 3, "audit": 1, "invalid-source": 0}[mode]
			if calls != wantCalls || len(audits) != calls {
				t.Fatalf("unexpected calls/audits: %d/%d want %d", calls, len(audits), wantCalls)
			}
			if calls > 0 && (audits[0].DocumentPlan == nil || audits[0].RequestSHA256 == "" || audits[0].PlanSHA256 == "") {
				t.Fatal("missing document provenance")
			}
			if request.Bindings != nil && request.Bindings["{{owner0}}"] != "Ada" {
				t.Fatal("source changed")
			}
		})
	}
}
