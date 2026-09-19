package gen

import (
	"context"
	"errors"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

const V13FactGenerationRevision = "v13-fact-generation-v5"

// V13FactGeneration stays inside the trusted private artifact. WorldSeed is
// independent of the public lease seed and controls both memory and fixtures.
// Transcript hashes attest reconstruction, not semantic qualification.
type V13FactGeneration struct {
	Revision         string                        `json:"revision"`
	WorldSeed        int64                         `json:"world_seed"`
	PresentationSeed int64                         `json:"presentation_seed"`
	Events           []universe.V13FactRenderEvent `json:"events"`
}

// GenerateV13FactDataset is the opt-in private fact producer. Business and
// personal programs, stories and ordinary world records are authored from typed facts; remaining families retain
// their world-derived generators, under private world entropy. No legacy
// typo or text-to-text rewrite pass runs. This API alone does not qualify a
// dataset or authorize a production lease.
func GenerateV13FactDataset(ctx context.Context, leaseSeed, worldSeed, presentationSeed int64, runSize string, renderer universe.V13FactRenderer) (DatasetArtifact, error) {
	if renderer == nil || worldSeed == 0 || worldSeed == leaseSeed {
		return DatasetArtifact{}, errors.New("fact dataset: independent private world and renderer required")
	}
	prof, ok := ProfileForVersion(runSize, 13)
	if !ok {
		return DatasetArtifact{}, errors.New("fact dataset: unknown profile")
	}
	recorder := &universe.V13RecordingFactRenderer{Renderer: renderer}
	rng, err := NewRNGForVersion(worldSeed, 13)
	if err != nil {
		return DatasetArtifact{}, err
	}
	tools, _ := GenerateToolsForVersion(rng, worldSeed, prof.Tools, 13)
	tools, err = renderToolDecisionFacts(ctx, tools, recorder)
	if err != nil {
		return DatasetArtifact{}, err
	}
	tools, err = renderMutationRequests(ctx, tools, recorder)
	if err != nil {
		return DatasetArtifact{}, err
	}
	tools, err = renderToolRequests(ctx, tools, recorder)
	if err != nil {
		return DatasetArtifact{}, err
	}
	suite, err := generateV13WorldMemorySuiteWithFacts(ctx, worldSeed, prof.Mem, prof.Waves, 13, presentationSeed, recorder)
	if err != nil {
		return DatasetArtifact{}, err
	}
	iso, err := generateWorldIsolationWithFacts(ctx, worldSeed, prof.Mem, prof.IsoCases, 13, recorder)
	if err != nil {
		return DatasetArtifact{}, err
	}
	suite.Cases = append(suite.Cases, iso.Cases...)
	waves := MergeMemoryWaves(suite.Waves, iso.SecondaryWave)
	checkedGraph := append([]protocol.SeedRequest{{UserID: PrimaryUser, Pairs: suite.FactWorldPairs}}, waves...)
	tools, err = reconcileFactPrerequisites(tools, checkedGraph)
	if err != nil {
		return DatasetArtifact{}, err
	}
	artifact, err := BuildArtifactForVersionWithSurface(worldSeed, 13, tools, suite.Cases, waves, SurfaceOptions{factGrounded: true})
	if err != nil {
		return DatasetArtifact{}, err
	}
	artifact.Seed = leaseSeed
	artifact.FactGeneration = &V13FactGeneration{Revision: V13FactGenerationRevision, WorldSeed: worldSeed, PresentationSeed: presentationSeed, Events: recorder.Events()}
	return artifact, nil
}

// Tool prerequisites and memory waves seed one shared graph. Use the same
// checked text for shared identities without moving a future correction into
// the earlier tool phase or replacing tool-only decision-twin records.
func reconcileFactPrerequisites(tools []protocol.ToolCase, waves []protocol.SeedRequest) ([]protocol.ToolCase, error) {
	shared := map[string]protocol.MemoryPair{}
	for _, wave := range waves {
		if wave.UserID != "" && wave.UserID != PrimaryUser {
			continue
		}
		for _, pair := range wave.Pairs {
			if old, ok := shared[pair.PairID]; ok && old != pair {
				return nil, errors.New("fact dataset: inconsistent shared memory identity")
			}
			shared[pair.PairID] = pair
		}
	}
	out := append([]protocol.ToolCase(nil), tools...)
	for i := range out {
		out[i].PrerequisitePairs = append([]protocol.MemoryPair(nil), tools[i].PrerequisitePairs...)
		for j, pair := range out[i].PrerequisitePairs {
			if checked, ok := shared[pair.PairID]; ok {
				if pair.SessionID != checked.SessionID || pair.Timestamp != checked.Timestamp {
					return nil, errors.New("fact dataset: prerequisite identity mismatch")
				}
				out[i].PrerequisitePairs[j] = checked
			}
		}
	}
	return out, nil
}

// ExecutionWorldSeed must be used by trusted runtime fixture construction.
// The public Seed remains the lease identity, not the private world entropy.
func (a DatasetArtifact) ExecutionWorldSeed() int64 {
	if a.FactGeneration != nil {
		return a.FactGeneration.WorldSeed
	}
	return a.Seed
}
