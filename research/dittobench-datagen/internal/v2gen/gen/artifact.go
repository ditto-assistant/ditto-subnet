package gen

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/internal/v2gen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// DatasetArtifact is the canonical, hashable snapshot of a fully-rendered run
// dataset. Its SHA-256 goes into ScoreReport.details as dataset_sha256 and pins
// the exact bytes a dispute re-scores. Field order is fixed and the payload is
// built from structs and sorted slices (no Go map is ranged into it), so
// json.Marshal is byte-stable: hashing the same artifact always yields the same
// digest, and with no LLM surface variation the same (seed, bench_version) does
// too. (Any map that json.Marshal touches, e.g. a ToolSpec's required_args, is
// key-sorted by encoding/json, so it too is byte-stable.)
type DatasetArtifact struct {
	Seed         int64                  `json:"seed"`
	BenchVersion int                    `json:"bench_version"`
	GeneratedAt  string                 `json:"generated_at"`
	ToolCases    []protocol.ToolCase    `json:"tool_cases"`
	MemoryWaves  []protocol.SeedRequest `json:"memory_waves"`
	MemoryCases  []ArtifactCase         `json:"memory_cases"`
	// ToolFixtures pins the mock-tool content the validator served per case. The
	// content is a pure function of (seed, case), but recording the coined
	// "needle" facts makes the served world explicit in the dispute artifact so a
	// re-score sees identical tool results. Sorted by CaseID (no Go map) so the
	// JSON stays byte-stable. Omitted when no tool endpoint was served.
	ToolFixtures []FixtureDigest `json:"tool_fixtures,omitempty"`
}

// ArtifactCase is a memory case as it enters the hashed artifact: the case plus
// the scoping that changes how it is scored, namely the memory graph it must be
// answered under (UserID) and the seeding wave that unlocks it (RunAfterWave).
// Without these two, an isolation case's graph scoping would not be pinned and
// two datasets differing only in a case's user_id would hash identically.
// (ForbiddenAnswer rides on the embedded MemoryCase.)
type ArtifactCase struct {
	protocol.MemoryCase
	UserID       string `json:"user_id,omitempty"`
	RunAfterWave int    `json:"run_after_wave,omitempty"`
}

// FixtureDigest is the hashable snapshot of one case's mock-tool environment: the
// case id and the coined needle its content tools embed (empty for cases whose
// served tools return only a confirmation).
type FixtureDigest struct {
	CaseID string `json:"case_id"`
	Needle string `json:"needle,omitempty"`
}

// GenerateDataset runs the full deterministic generation pipeline for
// (seed, prof) and returns the canonical, hashable DatasetArtifact. It is the
// generate service's single entry point: because generation is non-LLM, the
// artifact is byte-reproducible from (seed, bench_version), so the platform can
// ship it to a validator and a dispute can regenerate the identical bytes. The
// run path (runSizeJob) generates the same pieces (it also needs the live
// fixtures + staged suite) and calls BuildArtifact with them, so both paths
// produce an identical artifact for a given seed.
func GenerateDataset(seed int64, prof Profile, benchVersion int) (DatasetArtifact, error) {
	rng, err := NewRNGForVersion(seed, benchVersion)
	if err != nil {
		return DatasetArtifact{}, err
	}
	toolCases, _ := GenerateTools(rng, seed, prof.Tools)
	suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, benchVersion)
	if err != nil {
		return DatasetArtifact{}, err
	}
	iso, err := GenerateIsolationForVersion(seed, prof.Mem, prof.Waves, prof.IsoCases, benchVersion)
	if err != nil {
		return DatasetArtifact{}, err
	}
	suite.Cases = append(suite.Cases, iso.Cases...)
	memWaves := MergeMemoryWaves(suite.Waves, iso.SecondaryWave)
	return BuildArtifactForVersion(seed, benchVersion, toolCases, suite.Cases, memWaves)
}

// MergeMemoryWaves appends the secondary isolation graph's seeding wave to the
// primary waves when present, so the hashed artifact (and a dispute re-score)
// covers the exact multi-graph seeding. A no-op copy when there is no secondary
// graph. Kept as one helper so the run path and generate service agree.
func MergeMemoryWaves(primary []protocol.SeedRequest, secondary protocol.SeedRequest) []protocol.SeedRequest {
	if len(secondary.Pairs) == 0 {
		return primary
	}
	return append(append([]protocol.SeedRequest{}, primary...), secondary)
}

// BuildArtifact assembles the canonical DatasetArtifact from already-generated
// pieces. It is pure (no generation), so the run path and GenerateDataset share one
// assembly and can never drift into different bytes for the same seed. Tool
// fixtures are rebuilt from (seed, case) and sorted by CaseID so the JSON is
// byte-stable.
func BuildArtifact(seed int64, toolCases []protocol.ToolCase, memCases []StagedCase, memWaves []protocol.SeedRequest) DatasetArtifact {
	artifact, _ := BuildArtifactForVersion(seed, protocol.BenchVersionV2, toolCases, memCases, memWaves)
	return artifact
}

// BuildArtifactForVersion assembles an artifact under an explicit benchmark
// contract. It rejects unknown versions instead of silently using the current
// release.
func BuildArtifactForVersion(seed int64, benchVersion int, toolCases []protocol.ToolCase, memCases []StagedCase, memWaves []protocol.SeedRequest) (DatasetArtifact, error) {
	epoch, err := protocol.DatasetEpochForVersion(benchVersion)
	if err != nil {
		return DatasetArtifact{}, err
	}
	flat := make([]ArtifactCase, 0, len(memCases))
	for _, sc := range memCases {
		flat = append(flat, ArtifactCase{MemoryCase: sc.Case, UserID: sc.UserID, RunAfterWave: sc.RunAfterWave})
	}
	fixtures := make([]FixtureDigest, 0, len(toolCases))
	for _, c := range toolCases {
		f := toolexec.BuildFixture(seed, c)
		fixtures = append(fixtures, FixtureDigest{CaseID: c.ID, Needle: f.NeedleText()})
	}
	sort.Slice(fixtures, func(i, j int) bool { return fixtures[i].CaseID < fixtures[j].CaseID })
	return DatasetArtifact{
		Seed:         seed,
		BenchVersion: benchVersion,
		GeneratedAt:  epoch.UTC().Format("2006-01-02T15:04:05Z07:00"),
		ToolCases:    toolCases,
		MemoryWaves:  memWaves,
		MemoryCases:  flat,
		ToolFixtures: fixtures,
	}, nil
}

// Marshal returns the canonical JSON bytes of the artifact.
func (a DatasetArtifact) Marshal() ([]byte, error) {
	return json.Marshal(a)
}

// SHA256Hex returns the hex SHA-256 of the artifact's canonical JSON, plus those
// bytes (for optional persistence/upload). A marshal error is returned rather
// than panicking so hashing never crashes a sweep.
func (a DatasetArtifact) SHA256Hex() (string, []byte, error) {
	b, err := a.Marshal()
	if err != nil {
		return "", nil, err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), b, nil
}
