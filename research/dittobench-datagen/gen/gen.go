// Package gen produces fresh, anti-overfit benchmark datasets for a single
// submission. The permutation space is large enough that a miner cannot
// precompute a lookup table or overfit to a fixed dataset.
//
// # Anti-cheat combinatorics
//
// Each submission draws a fresh crypto-random seed (see FreshSeed, logged with
// the run). Every layer below multiplies the entropy:
//
// Tool cases:
//   - category × template × entity are sampled per case from large pools
//     (datagen); the prompt is one of the category's hand-written phrasing
//     variants, chosen by the seeded rng, so the surface wording varies per seed
//     while the deterministic ground truth (expected_tools + expected_behavior)
//     is preserved: scoring stays exact and the dataset is reproducible.
//
// Memory cases (persona engine):
//   - a procedural persona universe is built from the seed; its beats render to a
//     haystack from deterministic template surfaces (seeded phrasing variants),
//     and questions are derived from ground truth with per-(seed, question)
//     phrasing variants.
//   - every pair gets a FRESH timestamp anchored to the dataset epoch (per-session
//     day offsets + per-pair spacing), so temporal structure differs each run.
//
// Generation is fully non-LLM and byte-reproducible from (seed, bench_version):
// the anti-cheat entropy is the seed-driven combinatorics (persona facts,
// template-variant selection, timestamps, sampling) plus a fresh per-submission
// crypto-random seed, not a post-hoc paraphrase. Even the small profile is far
// beyond any feasible lookup table; medium and full are far larger.
package gen

import (
	cryptorand "crypto/rand"
	"encoding/binary"
	"fmt"
	"math/rand"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Profile is the size knobs for a run_size.
type Profile struct {
	Tools int // number of tool cases
	Mem   int // number of memory cases
	// Waves is the number of staged Tier-C seeding waves (1 = single seed).
	// RawPairsFrac is the fraction of memory questions seeded pairs-only (Tier B).
	// small stays a single Tier-A wave so the smoke path is cheap + simple.
	Waves        int
	RawPairsFrac float64
	// IsoCases is the number of multi-graph isolation cases: a second
	// persona is seeded under a different user_id and these cases query one user
	// while the other holds a conflicting value. 0 keeps the small/smoke path
	// single-user and cheap.
	IsoCases int
}

// Profiles maps run_size → counts (v2/v3/v4). Frozen: changing these changes
// those contracts' dataset bytes. v5 uses profilesV5 via ProfileForVersion.
var Profiles = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 20, Mem: 20, Waves: 2, RawPairsFrac: 0.3, IsoCases: 2},
	"full":   {Tools: 60, Mem: 50, Waves: 2, RawPairsFrac: 0.35, IsoCases: 4},
}

// profilesV5 scales the bench_version 5 run sizes up for comprehensiveness (v5
// plan 4.3: scale the store so retrieval recall is the bottleneck, not parsing).
// Relative to v4: more memory cases and tool cases, DEEPER multi-session staging
// (more waves, so knowledge-update / point-in-time / longitudinal chains span
// more of the timeline), a larger raw-pairs (Tier-B) share, and more multi-graph
// isolation personas. The bigger memory-case count also grows the procedural
// persona (personaOptsFor), so the haystack and distractor density rise with it.
// small stays a cheap single-wave smoke path. Only v5 uses these, so v2/v3/v4
// bytes are untouched.
// Counts are chosen with the between-seed DIFFICULTY variance in mind: more cases
// at the FIXED stratified per-category mix lowers tool_mean variance (measured with
// cmd/benchcal --version 5: tool σ falls from ~0.036 at n=80 to ~0.026 at n=120),
// so scaling up adds coverage AND tightens difficulty rather than widening it.
var profilesV5 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 40, Mem: 40, Waves: 3, RawPairsFrac: 0.35, IsoCases: 3},
	"full":   {Tools: 110, Mem: 85, Waves: 4, RawPairsFrac: 0.4, IsoCases: 6},
}

// profilesV7 scales bench_version 7 for the difficulty release (see
// docs/bench-versions.md). Relative to v5/v6: more memory and tool cases, one
// more staging wave (so the deep write chains have room for their
// save -> update -> update -> read/delete sequence), a larger Tier-B raw-pairs
// share (more of the store must be self-indexed), and more multi-graph
// isolation cases. The larger Mem also selects a denser persona
// (personaOptsFor), raising the distractor-to-needle ratio. small stays the
// cheap single-wave smoke path. Only v7 uses these, so v2..v6 bytes are
// untouched. Generation stays non-LLM and fast (a full v7 dataset renders in
// well under a second), so the per-submission `full` path is unaffected.
var profilesV7 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 40, Mem: 52, Waves: 4, RawPairsFrac: 0.45, IsoCases: 4},
	"full":   {Tools: 84, Mem: 185, Waves: 5, RawPairsFrac: 0.5, IsoCases: 10},
}

// profilesV8 spends a modestly larger scored envelope on broad typo/domain
// coverage and additional composed queries over the same seeded universe. Small
// remains the compatibility smoke path; v7 and its historical costs are frozen.
var profilesV8 = map[string]Profile{
	"small": {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	// V8 uses only read-only seeded contamination for graph isolation. These
	// quotas replace the five synthetic cross-user write/delete turns while
	// preserving the exact public case envelope.
	"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
	"full":   {Tools: 100, Mem: 225, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
}

// profilesV9 is a separately pinned pre-activation cost envelope. Its counts
// deliberately equal v8 while v9 changes the scored family distribution, but
// it must not inherit through a >= comparison: a future version needs an
// explicit profile decision before generation can be supported.
var profilesV9 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
	"full":   {Tools: 100, Mem: 225, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
}

// profilesV10 is explicit even though its ordinary-score envelope initially
// matches v9. The difficult deep-history dimension is a separate, bounded
// confirmation profile; silently inheriting a future version here would make a
// half-built contract runnable without review.
var profilesV10 = map[string]Profile{
	"small":  {Tools: 6, Mem: 6, Waves: 1, RawPairsFrac: 0, IsoCases: 0},
	"medium": {Tools: 48, Mem: 64, Waves: 4, RawPairsFrac: 0.45, IsoCases: 5},
	"full":   {Tools: 100, Mem: 225, Waves: 5, RawPairsFrac: 0.5, IsoCases: 9},
}

// ProfileFor returns the Profile for a run_size, defaulting to small. Uses the
// historical (v2/v3/v4) sizes; canonical versioned callers use ProfileForVersion.
func ProfileFor(runSize string) (Profile, bool) {
	p, ok := Profiles[runSize]
	if !ok {
		return Profiles["small"], false
	}
	return p, true
}

// ProfileForVersion returns the Profile for a (run_size, bench_version). v5 uses
// the scaled-up profilesV5; earlier versions use the frozen historical sizes, so
// their dataset bytes are unchanged. Deterministic, so any third party holding
// (seed, run_size, bench_version) regenerates the identical dataset.
func ProfileForVersion(runSize string, benchVersion int) (Profile, bool) {
	var profiles map[string]Profile
	switch benchVersion {
	case protocol.BenchVersionV2, protocol.BenchVersionV3, protocol.BenchVersionV4:
		profiles = Profiles
	case protocol.BenchVersionV5, protocol.BenchVersionV6:
		profiles = profilesV5
	case protocol.BenchVersionV7:
		profiles = profilesV7
	case protocol.BenchVersionV8:
		profiles = profilesV8
	case protocol.BenchVersionV9:
		profiles = profilesV9
	case protocol.BenchVersionV10, protocol.BenchVersionV11:
		// v11 keeps v10's run-size envelopes; its difficulty comes from the
		// generator contract, not from scale.
		profiles = profilesV10
	default:
		return Profile{}, false
	}
	p, ok := profiles[runSize]
	if !ok {
		return profiles["small"], false
	}
	return p, true
}

// FreshSeed returns a cryptographically-random int64 seed for a submission. The
// caller logs it with the run so a dispute can reproduce the exact dataset.
func FreshSeed() int64 {
	var b [8]byte
	if _, err := cryptorand.Read(b[:]); err != nil {
		// Extremely unlikely; fall back to a non-zero constant-ish value.
		return int64(1)
	}
	// Mask to a non-negative int64 for clean logging/JSON.
	return int64(binary.LittleEndian.Uint64(b[:]) >> 1)
}

// NewRNG returns a math/rand source for a run. It seeds from the version-rotated
// seed (protocol.RotateSeed) so the generated surface rotates per bench_version;
// determinism per (seed, bench_version) is what lets a logged run be reproduced
// exactly.
func NewRNG(seed int64) *rand.Rand {
	return rand.New(rand.NewSource(protocol.RotateSeed(seed)))
}

// NewRNGForVersion returns the generation stream for an explicit immutable
// benchmark contract.
func NewRNGForVersion(seed int64, benchVersion int) (*rand.Rand, error) {
	rotated, err := protocol.RotateSeedForVersion(seed, benchVersion)
	if err != nil {
		return nil, err
	}
	return rand.New(rand.NewSource(rotated)), nil
}

// fmtErr wraps a stage error with package context.
func fmtErr(stage string, err error) error {
	return fmt.Errorf("gen %s: %w", stage, err)
}
