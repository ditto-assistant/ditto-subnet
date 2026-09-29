package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V14 carries the typed-semantic generation surface and grader forward. Its
// own seed domain, epoch, and artifact version make it independently replayable.
func TestV14KnownVector(t *testing.T) {
	prof, ok := ProfileForVersion("full", protocol.BenchVersionV14)
	if !ok {
		t.Fatal("v14 full profile missing")
	}
	a, err := GenerateDataset(123456789, prof, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	digest, _, err := a.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	const want = "8a08dfe713fd6df2d67ece92148118a3fbd64b5d0f6a90f78dccb9853e329d50"
	if digest != want {
		t.Fatalf("v14 known vector: got %s, want %s", digest, want)
	}
	if a.BenchVersion != 14 || a.GeneratedAt != "2027-06-01T00:00:00Z" {
		t.Fatalf("v14 envelope: version=%d epoch=%s", a.BenchVersion, a.GeneratedAt)
	}
	if protocol.CurrentBenchVersion != protocol.BenchVersionV8 {
		t.Fatal("preactivation default moved")
	}
}
