package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

type runtimeFactFixture struct{}

func (runtimeFactFixture) PlanDocument(_ context.Context, r universe.V13FactDocumentRequest) (universe.V13FactDocumentPlan, error) {
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
		p.Records = append(p.Records, strings.Repeat(" Neutral texture.", 60)+strings.Join(tokens, " ")+strings.Repeat(" Neutral texture.", 60))
	}
	return p, nil
}
func (runtimeFactFixture) CheckDocument(context.Context, universe.V13FactDocumentRequest, universe.V13FactDocumentPlan) error {
	return nil
}

func (runtimeFactFixture) Plan(_ context.Context, r universe.V13FactRenderRequest) (universe.V13FactRenderPlan, error) {
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
func (runtimeFactFixture) Check(context.Context, universe.V13FactRenderRequest, universe.V13FactRenderPlan) error {
	return nil
}

func TestPrivateFactWorldFixturesMatchVerifiedArtifact(t *testing.T) {
	a, err := gen.GenerateV13FactDataset(context.Background(), 42, 731, 92713, "small", runtimeFactFixture{})
	if err != nil {
		t.Fatal(err)
	}
	pin, raw, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	verified, err := gen.DecodePrivateArtifact(raw, pin, 42, "small")
	if err != nil {
		t.Fatal(err)
	}
	fixtures := executionToolFixtures(verified)
	if len(fixtures) != len(a.ToolCases) {
		t.Fatal("wrong case identity map")
	}
	differentFromLease := false
	for _, c := range a.ToolCases {
		want := toolexec.BuildFixtureForVersion(731, c, 13)
		wrong := toolexec.BuildFixtureForVersion(42, c, 13)
		if !reflect.DeepEqual(fixtures[c.ID], want) {
			t.Fatal("fixture used wrong world")
		}
		differentFromLease = differentFromLease || !reflect.DeepEqual(want, wrong)
	}
	if !differentFromLease {
		t.Fatal("fixture does not exercise private-world distinction")
	}
	for _, f := range verified.ToolFixtures {
		if fixtures[f.CaseID].NeedleText() != f.Needle {
			t.Fatal("served fixture differs from artifact pin")
		}
	}
}

func TestPrivateDatasetAdmission(t *testing.T) {
	valid := submitRequest{BenchVersion: 13, PrivateDatasetMode: privateDatasetMode, PrivateDatasetBytes: []byte("{}"), ExpectedDatasetSHA256: strings.Repeat("a", 64)}
	if err := validatePrivateDatasetRequest(valid, true); err != nil {
		t.Fatal(err)
	}
	if err := validatePrivateDatasetRequest(submitRequest{BenchVersion: 12}, false); err != nil {
		t.Fatal("public compatibility changed")
	}
	for _, edit := range []func(*submitRequest){
		func(r *submitRequest) { r.PrivateDatasetMode = "" },
		func(r *submitRequest) { r.PrivateDatasetBytes = nil },
		func(r *submitRequest) { r.PrivateDatasetMode = "unknown" },
		func(r *submitRequest) { r.BenchVersion = 12 },
		func(r *submitRequest) { r.ExpectedDatasetSHA256 = "" },
	} {
		r := valid
		edit(&r)
		if validatePrivateDatasetRequest(r, true) == nil {
			t.Fatal("invalid private contract admitted")
		}
	}
	if validatePrivateDatasetRequest(valid, false) == nil {
		t.Fatal("disabled private path admitted")
	}
	s := &server{}
	if !reflect.DeepEqual(s.datasetFeatures(), []string{"git_subdir"}) {
		t.Fatal("disabled capability advertised")
	}
	s.allowPrivateDatasets = true
	if !reflect.DeepEqual(s.datasetFeatures(), []string{"git_subdir", privateDatasetMode}) {
		t.Fatal("enabled capability missing")
	}
}

func TestPrivateDatasetExactBytesReachProjection(t *testing.T) {
	profile, _ := gen.ProfileForVersion("small", 13)
	a, err := gen.GenerateDatasetWithSurface(4242, profile, 13, gen.SurfaceOptions{Salt: 99})
	if err != nil {
		t.Fatal(err)
	}
	// This is a synthetic delivery fixture, never qualification evidence.
	// Preserve protected values and context bindings: the decoder must not
	// accept the old fixture, which replaced entire requests with empty facts.
	a.ToolCases[0].Prompt = "A unique private tool presentation. " + a.ToolCases[0].Prompt
	a.MemoryCases[0].Question = "A unique private memory presentation. " + a.MemoryCases[0].Question
	a.MemoryWaves[0].Pairs[0].Prompt = "A unique private seed presentation. " + a.MemoryWaves[0].Pairs[0].Prompt
	raw, err := a.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	// Base64 transport must preserve even trailing whitespace under the pin.
	raw = append(raw, '\n')
	digest := sha256.Sum256(raw)
	wire, _ := json.Marshal(submitRequest{PrivateDatasetBytes: raw})
	var received submitRequest
	if err := json.Unmarshal(wire, &received); err != nil || !bytes.Equal(raw, received.PrivateDatasetBytes) {
		t.Fatal("trusted transport changed artifact bytes")
	}
	verified, err := gen.DecodePrivateArtifact(received.PrivateDatasetBytes, hex.EncodeToString(digest[:]), 4242, "small")
	if err != nil {
		t.Fatal(err)
	}
	tools, cases, waves := privateExecutionSurfaces(verified)
	if cases[0].Case.Question != a.MemoryCases[0].Question || cases[0].RunAfterWave != a.MemoryCases[0].RunAfterWave || !reflect.DeepEqual(cases[0].RequiredPairIDs, a.MemoryCases[0].V10EvidencePairIDs) {
		t.Fatal("staged memory artifact lost text or evidence")
	}
	projection, err := gen.BuildHarnessProjection(4242, bytes.Repeat([]byte{42}, 32), 13, tools, cases, waves)
	if err != nil {
		t.Fatal(err)
	}
	foundTool, foundQuestion, foundSeed := false, false, false
	for _, c := range projection.ToolCases {
		foundTool = foundTool || c.Prompt == a.ToolCases[0].Prompt
	}
	for _, c := range projection.MemoryCases {
		foundQuestion = foundQuestion || c.Case.Question == a.MemoryCases[0].Question
	}
	for _, w := range projection.Waves {
		for _, p := range w.Pairs {
			foundSeed = foundSeed || p.Prompt == a.MemoryWaves[0].Pairs[0].Prompt
		}
	}
	if !foundTool || !foundQuestion || !foundSeed {
		t.Fatal("private surface was replaced or regenerated before harness projection")
	}
}
