package protocol

import "testing"

func TestDuelingContractConstants(t *testing.T) {
	if DuelingMinBenchVersion != 16 || DuelingSliceColumns != 5 || DuelingSliceCasesPerColumn != 35 {
		t.Fatal("dueling contract constants moved; that is a new bench version")
	}
	// Each column carries 0.5 / DuelingSliceColumns of the composite.
	if DuelingColumnWeight*DuelingSliceColumns != 0.5 {
		t.Fatalf("column weight %v does not split the generator half over %d columns", DuelingColumnWeight, DuelingSliceColumns)
	}
	// A floor, never an enumeration.
	for v, want := range map[int]bool{DuelingMinBenchVersion - 1: false, DuelingMinBenchVersion: true, DuelingMinBenchVersion + 7: true} {
		if DuelingBenchVersion(v) != want {
			t.Fatalf("DuelingBenchVersion(%d) = %v", v, !want)
		}
	}
}
