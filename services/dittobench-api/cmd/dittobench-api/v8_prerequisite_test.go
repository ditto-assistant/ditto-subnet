package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestToolPrerequisiteWaveCollectsOnlyPrivatePairs(t *testing.T) {
	cases := []protocol.ToolCase{
		{ID: "ordinary"},
		{ID: "routed-a", PrerequisitePairs: []protocol.MemoryPair{{PairID: "p-a"}}},
		{ID: "routed-b", PrerequisitePairs: []protocol.MemoryPair{{PairID: "p-b"}}},
	}
	wave, err := toolPrerequisiteWave(cases)
	if err != nil {
		t.Fatal(err)
	}
	if wave.UserID != "miner" || len(wave.Pairs) != 2 || wave.Pairs[0].PairID != "p-a" || wave.Pairs[1].PairID != "p-b" {
		t.Fatalf("unexpected prerequisite wave: %+v", wave)
	}
}

func TestToolPrerequisiteWaveRejectsDuplicateIdentity(t *testing.T) {
	cases := []protocol.ToolCase{
		{PrerequisitePairs: []protocol.MemoryPair{{PairID: "same"}}},
		{PrerequisitePairs: []protocol.MemoryPair{{PairID: "same"}}},
	}
	if _, err := toolPrerequisiteWave(cases); err == nil {
		t.Fatal("duplicate prerequisite pair accepted")
	}
}

func TestV8GeneratedEvidenceIsAvailableThroughOrderedSeedBoundary(t *testing.T) {
	const seed int64 = 123456789
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV8)
	rng, err := gen.NewRNGForVersion(seed, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	tools, _ := gen.GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV8)
	memory, err := gen.GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	isolation, err := gen.GenerateIsolationForVersion(seed, prof.Mem, prof.Waves, prof.IsoCases, protocol.BenchVersionV8)
	if err != nil {
		t.Fatal(err)
	}
	memory.Cases = append(memory.Cases, isolation.Cases...)
	waves := gen.MergeMemoryWaves(memory.Waves, isolation.SecondaryWave)
	if err := validateV8EvidenceAvailability(tools, memory.Cases, waves); err != nil {
		t.Fatal(err)
	}

	// A missing record and the same record under the wrong user graph both fail
	// closed instead of producing a score against an unanswerable case.
	required := memory.Cases[0].RequiredPairIDs
	for len(required) == 0 {
		memory.Cases = memory.Cases[1:]
		required = memory.Cases[0].RequiredPairIDs
	}
	broken := append([]gen.StagedCase(nil), memory.Cases...)
	broken[0].RequiredPairIDs = []string{"missing-pair"}
	if err := validateV8EvidenceAvailability(tools, broken, waves); err == nil {
		t.Fatal("missing V8 evidence was accepted")
	}
	wrongGraphTools := []protocol.ToolCase{{PrerequisitePairs: []protocol.MemoryPair{{PairID: "primary-only"}}}}
	wrongGraphCases := []gen.StagedCase{{UserID: "colleague", RequiredPairIDs: []string{"primary-only"}}}
	if err := validateV8EvidenceAvailability(wrongGraphTools, wrongGraphCases, nil); err == nil {
		t.Fatal("evidence from the wrong user graph was accepted")
	}
}
