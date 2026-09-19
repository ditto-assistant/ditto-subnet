package main

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/parserprobe"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Structural fixture only, not semantic validation or rollout evidence.
type structuralRenderer struct{}

func (structuralRenderer) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
	p := universe.V13FactRenderPlan{Question: "Question about " + r.Subject}
	for i := range p.Records {
		p.Records[i] = strings.Join(r.Required[i], " ")
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
