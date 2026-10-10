package privatesurface

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

func TestLiteralModeProtectsTyposAndRequiresSemanticJudgment(t *testing.T) {
	for _, tc := range []struct {
		after   string
		verdict bool
		want    error
	}{
		{"Lois goes by Kit.", true, errProtected},
		{"Kit Kit is what Lois gos by.", true, errProtected},
		{"Kit is what Lois gos by.", false, errSemantic},
		{"Kit is what Lois gos by.", true, nil},
	} {
		calls := 0
		c := fakeClient(t, func(n int, request map[string]any) (int, any) {
			calls++
			if n == 1 {
				messages := request["messages"].([]any)
				var input map[string]any
				if json.Unmarshal([]byte(messages[1].(map[string]any)["content"].(string)), &input) != nil || input["text"] != "Lois gos by Kit." {
					t.Fatal("literal source lost")
				}
				raw, _ := json.Marshal(map[string]string{"text": tc.after})
				return 200, completion(string(raw))
			}
			if request["model"] != "validator-v1" {
				t.Fatal("independent validator missing")
			}
			raw, _ := json.Marshal(map[string]bool{"accepted": tc.verdict})
			return 200, completion(string(raw))
		})
		c.profile.RewriteMode = "literal-text-v1"
		_, _, err := c.ProbeOne(context.Background(), gen.PrivateSurfaceRequest{Text: "Lois gos by Kit.", Protected: []string{"Lois", "gos", "Kit"}})
		if err != tc.want {
			t.Fatalf("wrong disposition: %v", err)
		}
		if tc.want == errProtected && calls != 1 {
			t.Fatal("changed typo reached judge")
		}
		if tc.want != errProtected && calls != 2 {
			t.Fatal("semantic judge bypassed")
		}
	}
}

func TestLiteralModeFiltersAbsentProtectedValues(t *testing.T) {
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		raw, _ := json.Marshal(request)
		if strings.Contains(string(raw), "absent-secret") {
			t.Fatal("absent answer exposed")
		}
		return 200, completion(`{"text":null}`)
	})
	c.profile.RewriteMode = "literal-text-v1"
	after, receipt, err := c.ProbeOne(context.Background(), gen.PrivateSurfaceRequest{Text: "Lois gos by Kit.", Protected: []string{"gos", "absent-secret"}})
	if err != nil || after != "Lois gos by Kit." || receipt.ValidationMethod != "exact-byte-identity-v1" {
		t.Fatal("preservation lost")
	}
}

func TestLiteralModeBindsProfile(t *testing.T) {
	p := testProfile()
	before, err := p.Digest()
	// Produced by the pre-mode word-boundary producer; empty mode must not
	// silently invalidate previously approved legacy profile identities.
	if err != nil || before != "e9107255da30f1e2ccd3b17b34afac0f9869e2cea8669c913af72606607c4502" {
		t.Fatal("legacy profile digest changed")
	}
	p.RewriteMode = "literal-text-v1"
	after, err := p.Digest()
	if err != nil || before == after {
		t.Fatal("mode not bound")
	}
	p.RewriteMode = "unknown"
	if _, err := p.Digest(); err == nil {
		t.Fatal("unknown mode accepted")
	}
}
