package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

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
	if !reflect.DeepEqual(s.datasetFeatures(), []string{"git_subdir", "v13-deterministic-enterprise-v1"}) {
		t.Fatal("disabled capability advertised")
	}
	s.allowPrivateDatasets = true
	if !reflect.DeepEqual(s.datasetFeatures(), []string{"git_subdir", "v13-deterministic-enterprise-v1", privateDatasetMode}) {
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
