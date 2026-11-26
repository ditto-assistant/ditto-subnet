package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V19 is staged: this module generates it but no consumer negotiates it until
// the cross-layer PR raises the shared ceiling. It keeps v18's run size; the
// generalization levers (DITTO-2736) land on top, each gated >= 19, and pin a
// v19 known vector once the datagen half is final.
func TestV19IsStagedWithTheV18RunSize(t *testing.T) {
	if protocol.SupportedBenchVersion(protocol.BenchVersionV19) || !protocol.StagedBenchVersion(protocol.BenchVersionV19) {
		t.Fatal("v19 must be staged and not supported")
	}
	for _, runSize := range []string{"small", "medium", "full"} {
		v18, ok18 := ProfileForVersion(runSize, protocol.BenchVersionV18)
		v19, ok19 := ProfileForVersion(runSize, protocol.BenchVersionV19)
		if !ok18 || !ok19 || v18 != v19 {
			t.Fatalf("%s: v19 profile %+v (ok %v) differs from v18 %+v (ok %v)", runSize, v19, ok19, v18, ok18)
		}
	}
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV19)
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV19)
	if err != nil {
		t.Fatal(err)
	}
	if a.BenchVersion != 19 || a.GeneratedAt != "2027-11-01T00:00:00Z" {
		t.Fatalf("v19 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if len(a.ToolCases) != 50 || len(a.MemoryCases) != 125 {
		t.Fatalf("v19 datagen half: %d tool + %d memory cases, want 50 + 125", len(a.ToolCases), len(a.MemoryCases))
	}
}
