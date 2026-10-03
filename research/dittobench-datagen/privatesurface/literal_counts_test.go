package privatesurface

import (
	"context"
	"encoding/json"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"strings"
	"testing"
)

func TestVisibleLiteralFeedbackNeverDisclosesAbsentValues(t *testing.T) {
	req := gen.PrivateSurfaceRequest{Text: "Kit met Kit.", Protected: []string{"Kit", "Kit", "absent-secret", ""}}
	counts, mismatches := visibleLiteralCounts(req, "absent-secret met nobody.")
	if len(counts) != 1 || counts[0].Required != 2 || len(mismatches) != 1 || mismatches[0].Observed == nil || *mismatches[0].Observed != 0 {
		t.Fatal("incorrect visible count feedback")
	}
	raw, _ := json.Marshal([]any{counts, mismatches})
	if strings.Contains(string(raw), "absent-secret") || !strings.Contains(string(raw), `"observed":0`) {
		t.Fatal("hidden value exposed or zero omitted")
	}
}

func TestLiteralRetrySendsOnlyVisibleFeedback(t *testing.T) {
	c := fakeClient(t, func(n int, request map[string]any) (int, any) {
		raw, _ := json.Marshal(request)
		if strings.Contains(string(raw), "absent-secret") || strings.Contains(string(raw), "untrusted-prior") {
			t.Fatal("raw prior proposal disclosed")
		}
		if n == 1 {
			messages := request["messages"].([]any)
			var input map[string]json.RawMessage
			if json.Unmarshal([]byte(messages[1].(map[string]any)["content"].(string)), &input) != nil {
				t.Fatal("decode")
			}
			var feedback []literalCount
			if json.Unmarshal(input["retry_count_mismatches"], &feedback) != nil || len(feedback) != 1 || feedback[0].Required != 2 || *feedback[0].Observed != 1 {
				t.Fatal("feedback missing")
			}
			return 200, completion(`{"text":"Kit saw Kit."}`)
		}
		return 200, completion(`{"accepted":true}`)
	})
	c.profile.RewriteMode = "literal-text-v1"
	_, _, err := c.probeOne(context.Background(), gen.PrivateSurfaceRequest{Text: "Kit met Kit.", Protected: []string{"Kit"}}, 1, nil, "Kit untrusted-prior absent-secret")
	if err != nil {
		t.Fatal(err)
	}
}
