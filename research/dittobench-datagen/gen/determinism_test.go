package gen

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// marshal renders a value to canonical JSON for byte-level comparison.
func marshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

// TestMemoryPlanByteIdenticalPerSeed is the golden test: with wall-clock time
// removed (protocol.DatasetEpoch) and map iteration sorted, the memory suite
// (the /seed waves + the memory cases) is a pure function of the seed. Two
// GenerateMemorySuite runs at the same seed must be byte-identical.
func TestMemoryPlanByteIdenticalPerSeed(t *testing.T) {
	const seed = 20260706
	suiteA := GenerateMemorySuite(NewRNG(seed), seed, 8, 2, 0.25)
	suiteB := GenerateMemorySuite(NewRNG(seed), seed, 8, 2, 0.25)

	if got, want := marshal(t, suiteA.Waves), marshal(t, suiteB.Waves); got != want {
		t.Fatalf("seed waves not byte-identical for the same seed:\n a=%s\n b=%s", got, want)
	}
	if got, want := marshal(t, suiteA.Cases), marshal(t, suiteB.Cases); got != want {
		t.Fatalf("MemoryCases not byte-identical for the same seed:\n a=%s\n b=%s", got, want)
	}

	// The haystack must be non-trivial, otherwise byte-identity proves nothing.
	nonEmpty := false
	for _, w := range suiteA.Waves {
		if len(w.Pairs) > 0 {
			nonEmpty = true
		}
	}
	if !nonEmpty {
		t.Fatal("expected a non-empty haystack")
	}
}

// TestMemoryPlanVariesAcrossSeeds guards the flip side: determinism must not
// collapse the datasets to a constant: different seeds still differ.
func TestMemoryPlanVariesAcrossSeeds(t *testing.T) {
	suiteA := GenerateMemorySuite(NewRNG(1), 1, 8, 1, 0)
	suiteB := GenerateMemorySuite(NewRNG(2), 2, 8, 1, 0)
	if marshal(t, suiteA.Waves) == marshal(t, suiteB.Waves) {
		t.Fatal("seeds 1 and 2 produced identical haystacks")
	}
}

// TestDatasetEpochPinned pins the epoch's rendered form so an accidental change
// (which would silently break score comparability across the ledger) trips a
// test rather than shipping.
func TestDatasetEpochPinned(t *testing.T) {
	if !strings.HasPrefix(protocol.DatasetEpochRFC3339, "2026-01-01T00:00:00") {
		t.Fatalf("DatasetEpoch changed unexpectedly: %q — bump bench_version deliberately if intended",
			protocol.DatasetEpochRFC3339)
	}
}
