package main

import (
	"context"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Structural fixture only; never qualification evidence.
type auditFactFixture struct{}

func (auditFactFixture) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	p := universe.V13FactRenderPlan{Question: r.QuestionTemplate}
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
	}
	for _, f := range r.Facts {
		if f.Mode == "history" {
			if f.Sequence == 0 {
				p.Records[f.Record] = "Initial state: " + p.Records[f.Record]
			} else {
				p.Records[f.Record] = "Final update: " + p.Records[f.Record]
			}
		}
	}
	return p, nil
}
func (auditFactFixture) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}
func (auditFactFixture) PlanDocument(_ context.Context, r universe.V13FactDocumentRequest) (universe.V13FactDocumentPlan, error) {
	p := universe.V13FactDocumentPlan{}
	for _, rec := range r.Records {
		seen := map[string]bool{}
		for _, a := range rec.Assertions {
			for _, token := range a.Arguments {
				seen[token] = true
			}
		}
		tokens := []string{}
		for token := range seen {
			tokens = append(tokens, token)
		}
		sort.Strings(tokens)
		text := strings.Join(tokens, " ")
		if rec.InteriorFacts {
			text = strings.Repeat(" Neutral texture.", 60) + text + strings.Repeat(" Neutral texture.", 60)
		} else if rec.MinBytes > 1 {
			text += strings.Repeat(" Neutral texture.", 15)
		}
		p.Records = append(p.Records, text)
	}
	return p, nil
}
func (auditFactFixture) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	return nil
}

func TestTranscriptPrivateAuthorityReconstructed(t *testing.T) {
	wanted, err := gen.GenerateV13FactDataset(context.Background(), 42, 731, 92713, "small", auditFactFixture{})
	if err != nil {
		t.Fatal(err)
	}
	pin, raw, err := wanted.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	var lossy gen.DatasetArtifact
	if err := json.Unmarshal(raw, &lossy); err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(wanted, lossy) {
		t.Fatal("fixture did not exercise JSON-excluded authority")
	}
	restored, err := restoreTranscriptAuthority(raw, pin, 42, true, "small")
	if err != nil || !reflect.DeepEqual(restored, wanted) {
		t.Fatalf("authority reconstruction failed: %v", err)
	}
	for _, mode := range []string{"missing-pin", "missing-seed", "wrong-pin", "wrong-seed", "wrong-size", "tampered"} {
		t.Run(mode, func(t *testing.T) {
			digest, seed, provided, size, data := pin, int64(42), true, "small", append([]byte(nil), raw...)
			switch mode {
			case "missing-pin":
				digest = ""
			case "missing-seed":
				provided = false
			case "wrong-pin":
				digest = strings.Repeat("0", 64)
			case "wrong-seed":
				seed++
			case "wrong-size":
				size = "full"
			case "tampered":
				data = append(data, ' ')
			}
			if _, err := restoreTranscriptAuthority(data, digest, seed, provided, size); err == nil {
				t.Fatal("untrusted artifact accepted")
			}
		})
	}
}

func TestTranscriptPublicCannotMasqueradeAsPrivate(t *testing.T) {
	raw := []byte(`{"bench_version":13,"seed":42}`)
	if _, err := restoreTranscriptAuthority(raw, strings.Repeat("0", 64), 42, true, "small"); err == nil {
		t.Fatal("public artifact accepted as pinned private")
	}
}
