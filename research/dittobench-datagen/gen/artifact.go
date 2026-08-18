package gen

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/internal/assistantvoice"
	v2gen "github.com/ditto-assistant/dittobench-datagen/internal/v2gen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
	"github.com/ditto-assistant/dittobench-datagen/universe"
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
	UserID             string                      `json:"user_id,omitempty"`
	RunAfterWave       int                         `json:"run_after_wave,omitempty"`
	V10EvidencePairIDs []string                    `json:"v10_evidence_pair_ids,omitempty"`
	V10Provenance      *universe.V10CaseProvenance `json:"v10_provenance,omitempty"`
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
	// v2 is a FROZEN contract, served from a snapshot of the generator as it
	// stood when v2 shipped (internal/v2gen). It is not a conditional through
	// the live generator: the v3 anti-gaming work perturbs the shared rng stream
	// globally -- it moved 50 of 54 memory cases, the haystack epoch, and 18 tool
	// fixtures -- so version conditionals through the live path would have to
	// cover essentially every generation site and would silently rot the first
	// time one was missed.
	//
	// The snapshot is never edited. Both versions have to come from one binary
	// because the scoring engine advertises supported_bench_versions [2,3] and
	// selects per request, which is what lets a validator that has migrated to
	// the v3 stack keep serving v2 while the v3 cohort fills.
	if benchVersion == protocol.BenchVersionV2 {
		return generateV2Frozen(seed, prof)
	}
	rng, err := NewRNGForVersion(seed, benchVersion)
	if err != nil {
		return DatasetArtifact{}, err
	}
	toolCases, _ := GenerateToolsForVersion(rng, seed, prof.Tools, benchVersion)
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
		artifactCase := ArtifactCase{MemoryCase: sc.Case, UserID: sc.UserID, RunAfterWave: sc.RunAfterWave, V10Provenance: sc.V10Provenance}
		if benchVersion >= protocol.BenchVersionV10 {
			artifactCase.V10EvidencePairIDs = append([]string(nil), sc.RequiredPairIDs...)
		}
		flat = append(flat, artifactCase)
	}
	fixtures := make([]FixtureDigest, 0, len(toolCases))
	for _, c := range toolCases {
		f := toolexec.BuildFixture(seed, c)
		fixtures = append(fixtures, FixtureDigest{CaseID: c.ID, Needle: f.NeedleText()})
	}
	sort.Slice(fixtures, func(i, j int) bool { return fixtures[i].CaseID < fixtures[j].CaseID })
	artifact := DatasetArtifact{
		Seed:         seed,
		BenchVersion: benchVersion,
		GeneratedAt:  epoch.UTC().Format("2006-01-02T15:04:05Z07:00"),
		ToolCases:    toolCases,
		MemoryWaves:  memWaves,
		MemoryCases:  flat,
		ToolFixtures: fixtures,
	}
	if benchVersion >= protocol.BenchVersionV8 {
		artifact.ToolCases, artifact.MemoryWaves = cloneV8TranscriptSurfaces(toolCases, memWaves)
		if err := boundV8AssistantResponseCopies(seed, artifact.ToolCases, artifact.MemoryWaves, 2); err != nil {
			return DatasetArtifact{}, err
		}
	}
	// v12 supersedes the v11 surface pass with compositional stored-directive
	// markers; both leave every value byte-exact, so grading is unaffected.
	if benchVersion >= protocol.BenchVersionV12 {
		V12ApplyArtifactSurfaceNoise(seed, benchVersion, &artifact)
	} else {
		V11ApplyArtifactSurfaceNoise(seed, benchVersion, &artifact)
	}
	return artifact, nil
}

func cloneV8TranscriptSurfaces(toolCases []protocol.ToolCase, memWaves []protocol.SeedRequest) ([]protocol.ToolCase, []protocol.SeedRequest) {
	tools := append([]protocol.ToolCase(nil), toolCases...)
	for i := range tools {
		tools[i].PrerequisitePairs = append([]protocol.MemoryPair(nil), tools[i].PrerequisitePairs...)
	}
	waves := append([]protocol.SeedRequest(nil), memWaves...)
	for i := range waves {
		waves[i].Pairs = append([]protocol.MemoryPair(nil), waves[i].Pairs...)
	}
	return tools, waves
}

// boundV8AssistantResponseCopies keeps the transcript human without making its
// voice an answer table. A repeated attachment of the same pair is one logical
// record and remains byte-identical everywhere; distinct records may share one
// response at most maxCopies times. The pass runs only after every V8 surface is
// assembled, so tool prerequisites and both user graphs share one invariant.
func boundV8AssistantResponseCopies(seed int64, toolCases []protocol.ToolCase, memWaves []protocol.SeedRequest, maxCopies int) error {
	if maxCopies < 1 {
		return fmt.Errorf("v8 assistant response copy limit must be positive")
	}
	type record struct {
		key      string
		response string
		refs     []*protocol.MemoryPair
	}
	recordsByKey := map[string]*record{}
	ordered := make([]*record, 0)
	add := func(userID string, pair *protocol.MemoryPair) error {
		key := userID + "\x00" + pair.PairID
		if existing, ok := recordsByKey[key]; ok {
			if existing.response != pair.Response {
				return fmt.Errorf("v8 pair %s has conflicting assistant responses", pair.PairID)
			}
			existing.refs = append(existing.refs, pair)
			return nil
		}
		rec := &record{key: key, response: pair.Response, refs: []*protocol.MemoryPair{pair}}
		recordsByKey[key] = rec
		ordered = append(ordered, rec)
		return nil
	}
	for i := range toolCases {
		for j := range toolCases[i].PrerequisitePairs {
			if err := add(PrimaryUser, &toolCases[i].PrerequisitePairs[j]); err != nil {
				return err
			}
		}
	}
	for i := range memWaves {
		userID := memWaves[i].UserID
		if userID == "" {
			userID = PrimaryUser
		}
		for j := range memWaves[i].Pairs {
			if err := add(userID, &memWaves[i].Pairs[j]); err != nil {
				return err
			}
		}
	}

	counts := map[string]int{}
	for _, rec := range ordered {
		response := rec.response
		if counts[response] >= maxCopies {
			found := false
			for attempt := 1; attempt <= 32; attempt++ {
				candidate := assistantvoice.DiversifyDuplicate(seed, rec.key, rec.response, attempt)
				if counts[candidate] < maxCopies {
					response = candidate
					found = true
					break
				}
			}
			if !found {
				return fmt.Errorf("v8 assistant response variation exhausted for pair %s", rec.key)
			}
		}
		for _, ref := range rec.refs {
			ref.Response = response
		}
		counts[response]++
	}
	return nil
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

// generateV2Frozen produces the immutable v2 dataset from the frozen snapshot.
// The artifact types are structurally identical by construction (both are built
// from the shared protocol types), so this is a field copy rather than a
// re-encode; the golden v2 vector guards the result.
func generateV2Frozen(seed int64, prof Profile) (DatasetArtifact, error) {
	a, err := v2gen.GenerateDataset(seed, v2gen.Profile{
		Tools:        prof.Tools,
		Mem:          prof.Mem,
		Waves:        prof.Waves,
		RawPairsFrac: prof.RawPairsFrac,
		IsoCases:     prof.IsoCases,
	}, protocol.BenchVersionV2)
	if err != nil {
		return DatasetArtifact{}, err
	}
	out := DatasetArtifact{
		Seed:         a.Seed,
		BenchVersion: a.BenchVersion,
		GeneratedAt:  a.GeneratedAt,
		ToolCases:    a.ToolCases,
		MemoryWaves:  a.MemoryWaves,
	}
	for _, c := range a.MemoryCases {
		out.MemoryCases = append(out.MemoryCases, ArtifactCase{
			MemoryCase: c.MemoryCase, UserID: c.UserID, RunAfterWave: c.RunAfterWave,
		})
	}
	for _, f := range a.ToolFixtures {
		out.ToolFixtures = append(out.ToolFixtures, FixtureDigest(f))
	}
	return out, nil
}
