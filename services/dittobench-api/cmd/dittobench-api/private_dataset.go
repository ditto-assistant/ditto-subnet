package main

import (
	"errors"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

const privateDatasetMode = "platform-private-v1"

func (s *server) datasetFeatures() []string {
	features := []string{"git_subdir", universe.V13EnterpriseRevision}
	if s.allowPrivateDatasets {
		features = append(features, privateDatasetMode)
	}
	return features
}

func validatePrivateDatasetRequest(req submitRequest, enabled bool) error {
	if req.PrivateDatasetMode == "" && len(req.PrivateDatasetBytes) == 0 {
		return nil // Existing public contracts are unchanged.
	}
	if !enabled || req.PrivateDatasetMode != privateDatasetMode || req.BenchVersion != 13 {
		return errors.New("private dataset mode unavailable")
	}
	if len(req.PrivateDatasetBytes) == 0 || len(req.PrivateDatasetBytes) > gen.MaxPrivateArtifactBytes || !canonicalSHA256(req.ExpectedDatasetSHA256) {
		return errors.New("private dataset bytes and canonical digest required")
	}
	return nil
}

// privateExecutionSurfaces feeds the VERIFIED stored artifact into projection
// and execution. Never call BuildArtifactForVersion on these surfaces: that
// would apply the public text pass again. RunAfterWave/evidence/user provenance
// stays attached, including the secondary isolation graph.
func privateExecutionSurfaces(a gen.DatasetArtifact) ([]protocol.ToolCase, []gen.StagedCase, []protocol.SeedRequest) {
	cases := make([]gen.StagedCase, len(a.MemoryCases))
	for i, c := range a.MemoryCases {
		cases[i] = gen.StagedCase{
			Case: c.MemoryCase, UserID: c.UserID, RunAfterWave: c.RunAfterWave,
			RequiredPairIDs: c.V10EvidencePairIDs, V10Provenance: c.V10Provenance,
		}
	}
	return a.ToolCases, cases, a.MemoryWaves
}
