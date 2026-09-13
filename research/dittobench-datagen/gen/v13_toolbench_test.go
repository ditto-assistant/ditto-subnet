package gen

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// TestV13ArtifactPinsSeededCatalog: a v13 artifact carries the per-seed
// catalog (with the seed's decoys) and versioned fixtures, while a v12 artifact
// carries neither (its bytes are pinned by TestV12KnownVector).
func TestV13ArtifactPinsSeededCatalog(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV13)
	if !ok {
		t.Fatal("v13 has no full profile")
	}
	const seed = int64(123456789)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	if artifact.BenchVersion != protocol.BenchVersionV13 || len(artifact.ToolCases) != prof.Tools {
		t.Fatalf("artifact version=%d tools=%d", artifact.BenchVersion, len(artifact.ToolCases))
	}
	if !reflect.DeepEqual(artifact.Catalog, catalog.CatalogForSeed(protocol.BenchVersionV13, seed)) {
		t.Fatal("v13 artifact does not pin the seeded catalog")
	}
	decoys := catalog.DecoysForSeed(seed)
	names := map[string]bool{}
	for _, tool := range artifact.Catalog {
		names[tool.Name] = true
	}
	for _, d := range decoys {
		if !names[d.Name] {
			t.Errorf("artifact catalog lacks decoy %s", d.Name)
		}
	}
	if names["set_main_model"] {
		t.Error("v13 artifact catalog advertises set_main_model")
	}
	// Fixture digests come from the versioned constructor: a decoy-correct case
	// has a needle borne by its decoy.
	needles := map[string]string{}
	for _, f := range artifact.ToolFixtures {
		needles[f.CaseID] = f.Needle
	}
	decoyCases := 0
	for _, tc := range artifact.ToolCases {
		if !strings.HasPrefix(tc.Category, "decoy_") {
			continue
		}
		decoyCases++
		if needles[tc.ID] == "" {
			t.Errorf("decoy-correct case %s has no fixture needle in the artifact", tc.ID)
		}
		f := toolexec.BuildFixtureForVersion(seed, protocol.BenchVersionV13, tc)
		if f.Bearer() != tc.ExpectedTools[0].Name {
			t.Errorf("decoy-correct case %s bearer=%q", tc.ID, f.Bearer())
		}
	}
	if decoyCases < 10 {
		t.Errorf("v13 full artifact has %d decoy-correct cases, want >= 10", decoyCases)
	}

	v12prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	v12, err := GenerateDataset(seed, v12prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatal(err)
	}
	if v12.Catalog != nil {
		t.Fatal("v12 artifact must not carry a catalog")
	}
	raw, _ := v12.Marshal()
	if strings.Contains(string(raw), `"catalog"`) {
		t.Fatal("v12 artifact JSON gained a catalog key")
	}
}
