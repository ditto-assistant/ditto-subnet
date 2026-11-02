package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V15 carries the v14 generation surface and grader forward (plus the v15
// atomic-value grading floor) and advertises the production-scale tool
// catalog. Its own seed domain, epoch, and artifact version make it
// independently replayable.
func TestV15KnownVector(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV15)
	if !ok {
		t.Fatal("v15 full profile missing")
	}
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV15)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	const want = "33232bf35e829cbd08efc56777988d1e8784760487837aaa2f185c6f2077783d"
	if digest != want {
		t.Fatalf("v15 known vector: got %s, want %s", digest, want)
	}
	if a.BenchVersion != 15 || a.GeneratedAt != "2027-07-01T00:00:00Z" {
		t.Fatalf("v15 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatal("preactivation default moved")
	}
}

// The v15 artifact carries the production-scale catalog; no generated tool
// case expects a connector tool.
func TestV15ArtifactCarriesConnectorsNoCaseExpectsOne(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV15)
	if !ok {
		t.Fatal("v15 full profile missing")
	}
	for _, seed := range []int64{1, 7, 123456789} {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV15)
		if err != nil {
			t.Fatal(err)
		}
		connectors := 0
		for _, tool := range a.Catalog {
			if _, ok := catalog.IsConnectorTool(tool.Name); ok {
				connectors++
			}
		}
		if connectors != catalog.ConnectorToolsPerSeed {
			t.Fatalf("seed %d: artifact carries %d connector tools, want %d", seed, connectors, catalog.ConnectorToolsPerSeed)
		}
		for _, c := range a.ToolCases {
			for _, spec := range c.ExpectedTools {
				if _, ok := catalog.IsConnectorTool(spec.Name); ok {
					t.Fatalf("seed %d: case %s expects connector tool %q", seed, c.ID, spec.Name)
				}
			}
		}
	}
}
