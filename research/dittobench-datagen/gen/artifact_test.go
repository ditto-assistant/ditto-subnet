package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// artifactFor builds the full DatasetArtifact for (seed, n) via the production
// GenerateDataset entry point (the same deterministic pipeline the generate
// service and the run path use), so the reproducibility tests exercise the real
// assembly, not a test-local copy.
func artifactFor(seed int64, n int) DatasetArtifact {
	artifact, err := GenerateDataset(seed, Profile{Tools: n, Mem: n, Waves: 2, RawPairsFrac: 0.3, IsoCases: 4}, protocol.BenchVersionV2)
	if err != nil {
		panic(err)
	}
	return artifact
}

// TestDatasetHashStable checks a hash is stable across repeated hashing of the
// same artifact bytes (the trivial dispute-replay direction).
func TestDatasetHashStable(t *testing.T) {
	a := artifactFor(1234, 20)
	h1, b1, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	h2, b2, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if h1 != h2 || string(b1) != string(b2) {
		t.Fatal("hash/bytes not stable across repeated hashing")
	}
	if len(h1) != 64 {
		t.Fatalf("expected 64-hex-char sha256, got %d", len(h1))
	}
}

// TestDatasetHashReproducibleFromSeed is the deterministic-render side:
// same (seed, bench_version) with no LLM surface variation ⇒ identical
// dataset ⇒ identical dataset_sha256.
func TestDatasetHashReproducibleFromSeed(t *testing.T) {
	h1, _, _ := artifactFor(999, 30).SHA256Hex()
	h2, _, _ := artifactFor(999, 30).SHA256Hex()
	if h1 != h2 {
		t.Fatal("same seed produced different dataset hashes")
	}
	// A different seed must (overwhelmingly) produce a different hash.
	h3, _, _ := artifactFor(1000, 30).SHA256Hex()
	if h1 == h3 {
		t.Fatal("distinct seeds produced identical dataset hashes")
	}
}
