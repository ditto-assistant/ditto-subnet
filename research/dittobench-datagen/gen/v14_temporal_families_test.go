package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v14TemporalOps are the provenance programs of the SN-355 and SN-358
// families.
var v14TemporalOps = map[string]bool{"as_of": true, "nth_by_date": true, "before_by_date": true, "after_by_date": true}

// TestV14TemporalFamiliesReachTheScoredArtifact: across full-profile v14
// seeds the SN-355 (as-of) and SN-358 (event-sequence) families reach the
// scored artifact through the temporal slot, stamped with the run's contract,
// and survive the salted surface pass: the expected value stays verbatim in
// the case's evidence and the honest reply still scores 1. v13 artifacts
// carry neither family.
func TestV14TemporalFamiliesReachTheScoredArtifact(t *testing.T) {
	seen := map[string]int{}
	for seed := int64(1); seed <= 12; seed++ {
		prof, _ := ProfileForVersion("full", protocol.BenchVersionV14)
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		pairs := map[string]string{}
		for _, w := range a.MemoryWaves {
			for _, p := range w.Pairs {
				pairs[p.PairID] = p.Prompt
			}
		}
		for _, c := range a.MemoryCases {
			if c.V10Provenance == nil || !v14TemporalOps[c.V10Provenance.Program.Op] {
				continue
			}
			seen[c.V10Provenance.Program.Op]++
			if c.BenchVersion != protocol.BenchVersionV14 {
				t.Fatalf("seed %d case %s: bench version %d", seed, c.ID, c.BenchVersion)
			}
			found := false
			for _, id := range c.V10EvidencePairIDs {
				if strings.Contains(strings.ToLower(pairs[id]), strings.ToLower(c.ExpectedAnswer)) {
					found = true
				}
			}
			if !found {
				t.Fatalf("seed %d case %s: expected %q is not verbatim in its evidence", seed, c.ID, c.ExpectedAnswer)
			}
			if v := grade.Memory(c.MemoryCase, protocol.RunResponse{FinalText: "It was the " + c.ExpectedAnswer + "."}); v.Score != 1 {
				t.Fatalf("seed %d case %s: honest reply scored %.2f (%v)", seed, c.ID, v.Score, v.Notes)
			}
		}
	}
	if seen["as_of"] == 0 || seen["nth_by_date"]+seen["before_by_date"]+seen["after_by_date"] == 0 {
		t.Fatalf("v14 temporal families missing from 12 full seeds: %v", seen)
	}
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	a, err := GenerateDataset(1, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range a.MemoryCases {
		if c.V10Provenance != nil && v14TemporalOps[c.V10Provenance.Program.Op] {
			t.Fatalf("v13 artifact carries v14 temporal case %s", c.ID)
		}
		if c.QuestionType == universe.V13ProgramQuestionType && len(c.V10EvidencePairIDs) != 6 {
			t.Fatalf("v13 business case %s has %d evidence pairs, want 6", c.ID, len(c.V10EvidencePairIDs))
		}
	}
}
