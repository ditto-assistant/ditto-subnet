package privatesurface

import (
	"context"
	"encoding/json"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"strings"
	"testing"
)

func TestRecoverCompletedDiagnostics(t *testing.T) {
	c := fakeClient(t, func(_ int, request map[string]any) (int, any) {
		if request["model"] == "validator-v1" {
			return 200, completion(`{"accepted":true}`)
		}
		messages := request["messages"].([]any)
		var source map[string]any
		_ = json.Unmarshal([]byte(messages[1].(map[string]any)["content"].(string)), &source)
		raw, _ := json.Marshal(map[string]any{"text": source["text"].(string) + "\n"})
		return 200, completion(string(raw))
	})
	profile, _ := gen.ProfileForVersion("small", 13)
	base, err := gen.GenerateDatasetWithSurface(4242, profile, 13, gen.SurfaceOptions{Salt: 91})
	if err != nil {
		t.Fatal(err)
	}
	var rows []Diagnostic
	want, _, err := c.ProduceWithDiagnostics(context.Background(), base, 4, func(d Diagnostic) { rows = append(rows, d) })
	if err != nil {
		t.Fatal(err)
	}
	got, receipt, err := RecoverCompletedDiagnostics(context.Background(), base, testProfile(), rows)
	if err != nil || string(got) != string(want) || len(receipt) == 0 {
		t.Fatalf("recovery failed: %v", err)
	}
	for _, mutate := range []func([]Diagnostic) []Diagnostic{
		func(r []Diagnostic) []Diagnostic { return r[:len(r)-1] },
		func(r []Diagnostic) []Diagnostic { r[1] = r[0]; return r },
		func(r []Diagnostic) []Diagnostic { r[0].Error = "rejected"; return r },
		func(r []Diagnostic) []Diagnostic { r[0].After += "tamper"; return r },
		func(r []Diagnostic) []Diagnostic { r[0].Receipt.Validation = CompletionReceipt{}; return r },
		func(r []Diagnostic) []Diagnostic { r[0].Receipt.ValidationMethod = "exact-byte-identity-v1"; return r },
	} {
		changed := mutate(append([]Diagnostic(nil), rows...))
		if data, proof, err := RecoverCompletedDiagnostics(context.Background(), base, testProfile(), changed); err == nil || data != nil || proof != nil {
			t.Fatal("accepted inconsistent checkpoint")
		}
	}
}

func TestFullRetryReceiptFitsBound(t *testing.T) {
	c := CompletionReceipt{ID: strings.Repeat("i", 256), Model: strings.Repeat("m", 256), Provider: strings.Repeat("p", 256), RequestSHA256: digest(nil), ResponseSHA256: digest(nil)}
	s := SurfaceReceipt{LocationSHA256: digest(nil), BeforeSHA256: digest(nil), AfterSHA256: digest(nil), Rewrite: c, Validation: c}
	for i := 0; i < 4; i++ {
		s.Rejected = append(s.Rejected, SurfaceReceipt{Rewrite: c, Validation: c})
		s.RejectedReasons = append(s.RejectedReasons, "private producer: semantic validation rejected")
	}
	r := Receipt{Surfaces: make([]SurfaceReceipt, 1706)}
	for i := range r.Surfaces {
		r.Surfaces[i] = s
	}
	raw, err := json.Marshal(r)
	if err != nil || len(raw) <= 4<<20 || len(raw) > MaxReceiptBytes {
		t.Fatalf("receipt bytes=%d error=%v", len(raw), err)
	}
}
