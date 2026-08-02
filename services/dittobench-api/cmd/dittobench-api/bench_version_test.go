package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// These cross-module vectors prove that the scorer selects the same immutable
// dataset contract as the platform generator. The v2 digest predates v3 and
// must never move; v3 deliberately occupies a distinct seed domain and epoch.
func TestBenchVersionDatasetVectors(t *testing.T) {
	prof, ok := gen.ProfileFor("full")
	if !ok {
		t.Fatal("full profile missing")
	}
	for _, tc := range []struct {
		version int
		want    string
	}{
		{protocol.BenchVersionV2, "dfb4fc243d7d3e84bb4e896d5873bbc9bda114e16f5215f913c13adbfbc4a7fe"},
		{protocol.BenchVersionV3, "766183922b5a56725bdf44573fc31adf05355dc80fe9654d436935363fcdb3f2"},
	} {
		artifact, err := gen.GenerateDataset(123456789, prof, tc.version)
		if err != nil {
			t.Fatalf("generate v%d: %v", tc.version, err)
		}
		got, _, err := artifact.SHA256Hex()
		if err != nil {
			t.Fatalf("hash v%d: %v", tc.version, err)
		}
		if got != tc.want {
			t.Fatalf("v%d dataset vector changed: got %s want %s", tc.version, got, tc.want)
		}
	}
}
