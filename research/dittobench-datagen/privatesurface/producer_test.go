package privatesurface

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func testProfile() Profile { return Profile{"rewrite-v1", "provider-a", "validator-v1", "provider-b"} }

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
		return 200, completion(`{"text":"changed text"}`)
	})
	base := gen.DatasetArtifact{BenchVersion: 13, SurfaceSalt: 1, ToolCases: []protocol.ToolCase{{ID: "t", Prompt: "source text"}}}
	var diagnostics []Diagnostic
	data, receipt, err := c.ProduceWithDiagnostics(context.Background(), base, 1, func(d Diagnostic) { diagnostics = append(diagnostics, d) })
	if err == nil || data != nil || receipt != nil || calls != 2*maxSurfaceAttempts {
		t.Fatalf("retry boundary failed: calls=%d err=%v", calls, err)
	}
	if len(diagnostics) != 1 || len(diagnostics[0].Receipt.Rejected) != maxSurfaceAttempts {
		t.Fatal("missing rejected-call provenance")
	}
}
