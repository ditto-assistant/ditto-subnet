package main

import (
	"context"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/parserprobe"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Structural fixture only, not semantic validation or rollout evidence.
type structuralRenderer struct{}

func (structuralRenderer) PlanDocument(_ context.Context, r universe.V13FactDocumentRequest) (universe.V13FactDocumentPlan, error) {
	p := universe.V13FactDocumentPlan{}
	for _, record := range r.Records {
		unique := map[string]bool{}
		for _, a := range record.Assertions {
			for _, token := range a.Arguments {
				unique[token] = true
			}
		}
		tokens := make([]string, 0, len(unique))
		for token := range unique {
			tokens = append(tokens, token)
		}
		sort.Strings(tokens)
		text := strings.Join(tokens, " ")
		if record.InteriorFacts {
			text = strings.Repeat(" Neutral texture.", 60) + text + strings.Repeat(" Neutral texture.", 60)
		} else if record.MinBytes > 1 {
			text += strings.Repeat(" Neutral texture.", 15)
		}
		p.Records = append(p.Records, text)
	}
	return p, nil
}
func (structuralRenderer) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	return nil
}

func (structuralRenderer) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	p := universe.V13FactRenderPlan{Question: "Question about " + r.Subject}
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
	}
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

func TestFactProbeUsesWorldFixtures(t *testing.T) {
	a, err := gen.GenerateV13FactDataset(context.Background(), 42, 731, 92713, "full", structuralRenderer{})
	if err != nil {
		t.Fatal(err)
	}
	// Control has identical cases and known world identity. Only the public
	// lease identity differs; changing it must not change tool execution.
	control := a
	control.Seed = a.ExecutionWorldSeed()
	control.FactGeneration = nil
	probe := func(a gen.DatasetArtifact) parserprobe.Report {
		r, err := parserprobe.Run(parserprobe.Options{BenchVersion: 13, RunSize: "full", Seeds: 1, Artifacts: []gen.DatasetArtifact{a}})
		if err != nil {
			t.Fatal(err)
		}
		return r
	}
	got, want := probe(a), probe(control)
	if !reflect.DeepEqual(got.GIH.Tool, want.GIH.Tool) {
		t.Fatal("private probe used lease seed instead of world fixtures")
	}
}
func (structuralRenderer) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}

func TestRestoreFactProbeAuthority(t *testing.T) {
	want, err := gen.GenerateV13FactDataset(context.Background(), 42, 731, 92713, "small", structuralRenderer{})
	if err != nil {
		t.Fatal(err)
	}
	raw, err := want.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	var plain gen.DatasetArtifact
	if err := json.Unmarshal(raw, &plain); err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(want, plain) {
		t.Fatal("fixture does not exercise omitted grading authority")
	}
	got, err := restoreProbeAuthority(raw, plain, "small")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(want, got) {
		t.Fatal("probe lost hidden grading authority")
	}
	plain.FactGeneration.Revision = "unsupported"
	bad, err := plain.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := restoreProbeAuthority(bad, plain, "small"); err == nil {
		t.Fatal("unsupported contract accepted")
	}
}
