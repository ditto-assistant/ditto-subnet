package main

import (
	"net/http"
	"strconv"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Public dataset sampler (task #52).
//
// Serves a full, real run-size DatasetArtifact from a reserved public seed so the
// community can inspect exactly what a validator scores, answer keys included.
// This is the same artifact the open generator (dittobench-datagen) produces for
// the same seed, offered over HTTP so no Go toolchain is needed for a quick look.
//
// It is not redacted. Redaction only made sense while the generator was secret;
// now that the generator is public, the full dataset for any seed is derivable by
// anyone, so hiding answer keys here would add complexity without protecting
// anything. Anti-overfit does not rely on secrecy: the seed for a scored
// submission is derived on-chain after the miner commits, so it cannot be
// predicted, and these sample seeds are drawn from a reserved NEGATIVE range that
// never collides with a real (non-negative) scored seed. The sampler still never
// accepts a caller-supplied raw seed, so it cannot be pointed at a specific
// submission.
const publicSampleSeedBase int64 = -1_000_000_007

// publicSampleSeed maps a sample index to its reserved-namespace public seed.
func publicSampleSeed(index int) int64 {
	return publicSampleSeedBase - int64(index)
}

// How many distinct public samples an index may select (0..maxSampleIndex).
const maxSampleIndex = 9

// handleSample serves a full run-size dataset from a reserved public seed. Query:
// ?run_size=small|medium|full (default small), ?sample=<0..maxSampleIndex>
// (default 0), ?bench_version=8|9 (default v9).
// No key required, generation is deterministic and LLM-free.
func (s *server) handleSample(w http.ResponseWriter, r *http.Request) {
	runSize := r.URL.Query().Get("run_size")
	if runSize == "" {
		runSize = "small"
	}
	index := parseIntDefault(r.URL.Query().Get("sample"), 0)
	if index < 0 || index > maxSampleIndex {
		writeError(w, http.StatusBadRequest, "sample must be between 0 and 9")
		return
	}
	version := protocol.BenchVersionV9
	if rawVersion := r.URL.Query().Get("bench_version"); rawVersion != "" {
		parsed, err := strconv.Atoi(rawVersion)
		if err != nil {
			writeError(w, http.StatusBadRequest, "bench_version must be an integer (supported: 8, 9, 10)")
			return
		}
		version = parsed
	}
	version, msg := requestedBenchVersion(version)
	if msg != "" {
		writeError(w, http.StatusBadRequest, msg)
		return
	}
	prof, ok := gen.ProfileForVersion(runSize, version)
	if !ok {
		writeError(w, http.StatusBadRequest, "run_size must be one of small|medium|full")
		return
	}
	art, err := gen.GenerateDataset(publicSampleSeed(index), prof, version)
	if err != nil {
		writeError(w, http.StatusBadRequest, "unsupported bench_version")
		return
	}
	writeJSON(w, http.StatusOK, art)
}
