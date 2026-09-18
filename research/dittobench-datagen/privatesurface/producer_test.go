package privatesurface

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func testProfile() Profile {
	return Profile{RewriteModel: "rewrite-v1", RewriteProvider: "provider-a", ValidatorModel: "validator-v1", ValidatorProvider: "provider-b"}
}

func TestProfileBindsModelAndRoute(t *testing.T) {
	p := testProfile()
	first, err := p.Digest()
	if err != nil {
		t.Fatal(err)
	}
	p.ValidatorProvider = "provider-c"
	second, _ := p.Digest()
	if first == second {
		t.Fatal("route not bound")
	}
	p.ValidatorModel = p.RewriteModel
	if _, err := p.Digest(); err == nil {
		t.Fatal("same model accepted")
	}
}

func fakeClient(t *testing.T, responder func(int, map[string]any) (int, any)) *Client {
	t.Helper()
	var mu sync.Mutex
	count := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-secret" {
			t.Error("missing auth")
		}
		raw, _ := io.ReadAll(r.Body)
		var request map[string]any
		if json.Unmarshal(raw, &request) != nil {
			t.Error("bad JSON")
		}
		provider := request["provider"].(map[string]any)
		if provider["zdr"] != true || provider["data_collection"] != "deny" || provider["allow_fallbacks"] != false || provider["require_parameters"] != true {
			t.Error("privacy/fallback contract lost")
		}
		if len(provider["only"].([]any)) != 1 {
			t.Error("route not exclusive")
		}
		mu.Lock()
		count++
		n := count
		mu.Unlock()
		status, payload := responder(n, request)
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(payload)
	}))
	t.Cleanup(server.Close)
	c, err := NewClient(testProfile(), "test-secret")
	if err != nil {
		t.Fatal(err)
	}
	c.url = server.URL
	c.retryDelay = 0
	return c
}

func completion(content string) any {
	return map[string]any{"id": "receipt-1", "model": "actual-v1", "provider": "actual-provider", "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]any{"content": content}}}, "usage": map[string]any{"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.001}}
}

func TestRewriteHasIndependentValidationAndDigestReceipt(t *testing.T) {
	c := fakeClient(t, func(n int, request map[string]any) (int, any) {
		if n == 1 {
			if request["model"] != "rewrite-v1" {
				t.Error("wrong rewrite model")
			}
			return 200, completion(`{"text":"Please find the record."}`)
		}
		if request["model"] != "validator-v1" {
			t.Error("same model used to validate")
		}
		return 200, completion(`{"accepted":true}`)
	})
	after, receipt, err := c.RewriteOne(context.Background(), gen.PrivateSurfaceRequest{Location: "hidden-location", Text: "Find the record."})
	if err != nil || after != "Please find the record." {
		t.Fatalf("rewrite failed: %v", err)
	}
	if receipt.Rewrite.RequestSHA256 == receipt.Validation.RequestSHA256 || len(receipt.BeforeSHA256) != 64 || receipt.Rewrite.CostUSD != .001 {
		t.Fatal("invalid receipt")
	}
	raw, _ := json.Marshal(receipt)
	for _, sensitive := range []string{"hidden-location", "Find the record.", "test-secret"} {
		if strings.Contains(string(raw), sensitive) {
			t.Fatal("receipt leaked surface/credential")
		}
	}
}

func TestRewriteReferenceContainsOnlyTheSameSource(t *testing.T) {
	source := "Lois gos by Kit."
	c := fakeClient(t, func(n int, request map[string]any) (int, any) {
		if n != 1 {
			t.Error("exact identity must not call a probabilistic judge")
			return 200, completion(`{"accepted":false}`)
		}
		messages := request["messages"].([]any)
		var input map[string]any
		_ = json.Unmarshal([]byte(messages[1].(map[string]any)["content"].(string)), &input)
		if input["reference_text"] != source || input["text"] == source {
			t.Fatal("rewrite lost source context or protected-token masking")
		}
		out, _ := json.Marshal(map[string]any{"text": input["text"]})
		return 200, completion(string(out))
	})
	after, receipt, err := c.RewriteOne(context.Background(), gen.PrivateSurfaceRequest{Location: "x", Text: source, Protected: []string{"Lois", "gos", "Kit"}})
	if err != nil || after != source {
		t.Fatalf("source-preserving candidate failed: %v", err)
	}
	if receipt.ValidationMethod != "exact-byte-identity-v1" || receipt.Validation.ID != "" || receipt.BeforeSHA256 != receipt.AfterSHA256 {
		t.Fatal("identity proof must be explicit and never claim an LLM validation")
	}
}

func TestRejectAndProviderFailureNeverReturnCandidate(t *testing.T) {
	for _, verdict := range []string{`{"accepted":false}`, `{"accepted":"true"}`, `{}`, `{"accepted":true,"explanation":"secret"}`, `{"accepted":false,"accepted":true}`, `{"accepted":true} {"accepted":true}`} {
		t.Run(verdict, func(t *testing.T) {
			c := fakeClient(t, func(n int, _ map[string]any) (int, any) {
				if n == 1 {
					return 200, completion(`{"text":"different text"}`)
				}
				return 200, completion(verdict)
			})
			after, _, err := c.RewriteOne(context.Background(), gen.PrivateSurfaceRequest{Text: "source"})
			if err == nil || after != "" || strings.Contains(err.Error(), "secret") {
				t.Fatal("invalid verdict accepted or leaked")
			}
		})
	}
	c := fakeClient(t, func(_ int, _ map[string]any) (int, any) { return 429, map[string]string{"error": "secret prompt"} })
	after, _, err := c.RewriteOne(context.Background(), gen.PrivateSurfaceRequest{Text: "source"})
	if err == nil || after != "" || strings.Contains(err.Error(), "secret") {
		t.Fatal("provider failure accepted or leaked")
	}
}

func TestExplicitPreservationIsNotMalformedResponseFallback(t *testing.T) {
	for _, content := range []string{`{"text":null}`, `{}`, `null`, `{"text":null,"extra":true}`, `{"text":null,"text":"changed"}`} {
		t.Run(content, func(t *testing.T) {
			calls := 0
			c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
				calls++
				if request["model"] != "rewrite-v1" {
					t.Error("preservation must not invent an independent LLM judgment")
				}
				return 200, completion(content)
			})
			source := "Keep <EXACT> with intentional typoos."
			after, receipt, err := c.RewriteOne(context.Background(), gen.PrivateSurfaceRequest{Text: source, Protected: []string{"<EXACT>", "typoos"}})
			if content == `{"text":null}` {
				if err != nil || after != source || receipt.ValidationMethod != "exact-byte-identity-v1" || receipt.Rewrite.ID == "" || receipt.Validation.ID != "" {
					t.Fatalf("explicit preservation lost provenance: %v", err)
				}
			} else if err == nil || after != "" {
				t.Fatal("malformed output became implicit preservation")
			}
			if calls != 1 {
				t.Fatal("unexpected provider calls")
			}
		})
	}
}

func TestProduceNeverApprovesUnchangedArtifact(t *testing.T) {
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":true}`)
		}
		messages := request["messages"].([]any)
		input := messages[1].(map[string]any)["content"].(string)
		var source map[string]any
		_ = json.Unmarshal([]byte(input), &source)
		out, _ := json.Marshal(map[string]any{"text": source["text"]})
		return 200, completion(string(out))
	})
	profile, _ := gen.ProfileForVersion("small", 13)
	base, err := gen.GenerateDatasetWithSurface(42, profile, 13, gen.SurfaceOptions{Salt: 42})
	if err != nil {
		t.Fatal(err)
	}
	data, receipt, err := c.Produce(context.Background(), base, 4)
	if err == nil || data != nil || receipt != nil {
		t.Fatal("unchanged dataset approved")
	}
}

func TestFullProfileProducesBoundedReceiptAndExactReplay(t *testing.T) {
	// Whitespace and an accepting fake judge exercise plumbing ONLY.
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":true}`)
		}
		messages := request["messages"].([]any)
		var source map[string]any
		_ = json.Unmarshal([]byte(messages[1].(map[string]any)["content"].(string)), &source)
		out, _ := json.Marshal(map[string]any{"text": source["text"].(string) + "\n"})
		return 200, completion(string(out))
	})
	profile, _ := gen.ProfileForVersion("full", 13)
	base, err := gen.GenerateDatasetWithSurface(4242, profile, 13, gen.SurfaceOptions{Salt: 91})
	if err != nil {
		t.Fatal(err)
	}
	data, receipt, err := c.Produce(context.Background(), base, 8)
	if err != nil {
		t.Fatal(err)
	}
	if len(receipt) <= 1<<20 || len(receipt) > 4<<20 {
		t.Fatalf("unexpected full receipt size %d", len(receipt))
	}
	if _, err := gen.DecodePrivateArtifact(data, digest(data), 4242, "full"); err != nil {
		t.Fatal(err)
	}
	var parsed Receipt
	if json.Unmarshal(receipt, &parsed) != nil || !parsed.Accepted || parsed.DatasetSHA256 != digest(data) {
		t.Fatal("invalid receipt")
	}
}

func TestProduceSemanticRetriesAreBoundedAndRetained(t *testing.T) {
	calls := 0
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		calls++
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":false}`)
		}
		messages := request["messages"].([]any)
		prompt := messages[0].(map[string]any)["content"].(string)
		if strings.Contains(prompt, preservationPrompt) != (calls == 2*maxSurfaceAttempts-1) {
			t.Error("preservation prompt must only be the final bounded candidate")
		}
		if calls == 2*maxSurfaceAttempts-1 {
			format := request["response_format"].(map[string]any)["json_schema"].(map[string]any)["schema"].(map[string]any)
			if format["properties"].(map[string]any)["text"].(map[string]any)["type"] != "null" {
				t.Error("preservation must be schema-bound")
			}
			return 200, completion(`{"text":null}`)
		}
		return 200, completion(`{"text":"changed text"}`)
	})
	base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, ToolCases: []protocol.ToolCase{{ID: "t", Prompt: "source text"}}}
	var diagnostics []Diagnostic
	data, receipt, err := c.ProduceWithDiagnostics(context.Background(), base, 1, func(d Diagnostic) { diagnostics = append(diagnostics, d) })
	if err == nil || data != nil || receipt != nil || calls != 2*maxSurfaceAttempts-1 {
		t.Fatalf("retry boundary failed: calls=%d err=%v", calls, err)
	}
	if len(diagnostics) != 1 || len(diagnostics[0].Receipt.Rejected) != maxSurfaceAttempts-1 {
		t.Fatal("missing rejected-call provenance")
	}
}

func TestGlobalAnswerIntroductionRetriesWithoutDisclosingHiddenValues(t *testing.T) {
	calls := 0
	c := fakeClient(t, func(n int, request map[string]any) (int, any) {
		calls++
		body, _ := json.Marshal(request)
		if strings.Contains(string(body), "ZEBRA") {
			t.Fatal("hidden answer was disclosed to provider")
		}
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":true}`)
		}
		if n == 1 {
			return 200, completion(`{"text":"ZEBRA is the code."}`)
		}
		return 200, completion(`{"text":"Please find my code."}`)
	})
	base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, MemoryCases: []gen.ArtifactCase{{MemoryCase: protocol.MemoryCase{ID: "q", Question: "Find my code.", ExpectedAnswer: "ZEBRA"}}}}
	data, receipt, err := c.Produce(context.Background(), base, 1)
	if err != nil || len(data) == 0 || calls != 3 {
		t.Fatalf("global candidate protection failed: calls=%d err=%v", calls, err)
	}
	var parsed Receipt
	if json.Unmarshal(receipt, &parsed) != nil || len(parsed.Surfaces) != 1 || len(parsed.Surfaces[0].Rejected) != 1 {
		t.Fatal("missing protected rejection provenance")
	}
}

func TestTransientProviderRetriesShareTheCandidateBudget(t *testing.T) {
	for _, status := range []int{429, 502, 503, 504, 401, 404} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			calls := 0
			c := fakeClient(t, func(_ int, _ map[string]any) (int, any) {
				calls++
				return status, map[string]string{"error": "PRIVATE provider detail"}
			})
			base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, ToolCases: []protocol.ToolCase{{ID: "t", Prompt: "source text"}}}
			data, receipt, err := c.Produce(context.Background(), base, 1)
			want := maxSurfaceAttempts
			if status == 401 || status == 404 {
				want = 1
			}
			if err == nil || data != nil || receipt != nil || calls != want || strings.Contains(err.Error(), "PRIVATE") {
				t.Fatalf("retry boundary: calls=%d err=%v", calls, err)
			}
		})
	}
}

func TestTransientRecoveryStillRequiresSemanticValidation(t *testing.T) {
	calls := 0
	c := fakeClient(t, func(n int, request map[string]any) (int, any) {
		calls++
		if n == 1 {
			return 503, nil
		}
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":true}`)
		}
		return 200, completion(`{"text":"Please consider the source text."}`)
	})
	base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, ToolCases: []protocol.ToolCase{{ID: "t", Prompt: "source text"}}}
	data, raw, err := c.Produce(context.Background(), base, 1)
	if err != nil || len(data) == 0 || calls != 3 {
		t.Fatalf("recovery failed: calls=%d err=%v", calls, err)
	}
	var receipt Receipt
	if json.Unmarshal(raw, &receipt) != nil || len(receipt.Surfaces[0].RejectedReasons) != 1 {
		t.Fatal("transient failure not recorded")
	}
}

func TestTruncatedOutputRecoveryIsBoundedAndValidated(t *testing.T) {
	for _, recoverAfterFirst := range []bool{false, true} {
		calls, validations := 0, 0
		c := fakeClient(t, func(n int, request map[string]any) (int, any) {
			calls++
			if !recoverAfterFirst || n == 1 {
				out := completion(`{"text":"TRUNCATED must never be accepted"}`).(map[string]any)
				out["choices"].([]any)[0].(map[string]any)["finish_reason"] = "length"
				return 200, out
			}
			if request["model"] == "validator-v1" {
				validations++
				return 200, completion(`{"accepted":true}`)
			}
			return 200, completion(`{"text":"Please consider the source text."}`)
		})
		base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, ToolCases: []protocol.ToolCase{{ID: "t", Prompt: "source text"}}}
		data, raw, err := c.Produce(context.Background(), base, 1)
		if recoverAfterFirst {
			if err != nil || calls != 3 || validations != 1 || strings.Contains(string(data), "TRUNCATED") {
				t.Fatalf("invalid recovery: calls=%d validations=%d err=%v", calls, validations, err)
			}
			var receipt Receipt
			if json.Unmarshal(raw, &receipt) != nil || len(receipt.Surfaces[0].RejectedReasons) != 1 {
				t.Fatal("truncation rejection was not recorded")
			}
		} else if err == nil || calls != maxSurfaceAttempts || data != nil || raw != nil {
			t.Fatalf("truncation bypassed retry boundary: calls=%d err=%v", calls, err)
		}
	}
}
