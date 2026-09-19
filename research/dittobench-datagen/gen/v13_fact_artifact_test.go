package gen

import (
	"context"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Structural fixture only; never semantic or private qualification evidence.
type artifactFactRenderer struct{}

func (artifactFactRenderer) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	var p universe.V13FactRenderPlan
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
	}
	p.Question = "Question about " + r.Subject
	if r.QuestionTemplate != "" {
		p.Question = r.QuestionTemplate
	}
	for _, f := range r.Facts {
		if f.Mode == "history" && f.Sequence == 0 {
			p.Records[f.Record] = "Initial state: " + p.Records[f.Record]
		}
		if f.Mode == "history" && f.Sequence == 1 {
			p.Records[f.Record] = "Final update: " + p.Records[f.Record]
		}
	}
	return p, nil
}
func (artifactFactRenderer) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}

func TestV13FactArtifactRoundTrip(t *testing.T) {
	for _, size := range []string{"small", "medium", "full"} {
		t.Run(size, func(t *testing.T) {
			want, err := GenerateV13FactDataset(context.Background(), 42, 731, 92713, size, artifactFactRenderer{})
			if err != nil {
				t.Fatal(err)
			}
			pin, raw, err := want.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			got, err := DecodePrivateArtifact(raw, pin, 42, size)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(want, got) {
				t.Fatal("roundtrip lost grading authority")
			}
			if got.ExecutionWorldSeed() != 731 || got.Seed != 42 || got.SurfaceSalt != 0 {
				t.Fatal("world/lease identity or surface contract drift")
			}
		})
	}
}

func TestV13FactArtifactRejectsTampering(t *testing.T) {
	for _, mode := range []string{"answer", "surface", "world", "transcript", "revision", "legacy-salt"} {
		t.Run(mode, func(t *testing.T) {
			a, err := GenerateV13FactDataset(context.Background(), 42, 731, 92713, "small", artifactFactRenderer{})
			if err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "answer":
				a.MemoryCases[0].ExpectedAnswer = "forged"
			case "surface":
				a.MemoryCases[0].Question = "forged"
			case "world":
				a.FactGeneration.WorldSeed++
			case "transcript":
				a.FactGeneration.Events = a.FactGeneration.Events[:1]
			case "revision":
				a.FactGeneration.Revision = "unknown"
			case "legacy-salt":
				a.SurfaceSalt = 3
			}
			pin, raw, err := a.SHA256Hex()
			if err != nil {
				t.Fatal(err)
			}
			if _, err := DecodePrivateArtifact(raw, pin, 42, "small"); err == nil {
				t.Fatal("tampering accepted despite new object digest")
			}
		})
	}
}
