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
	// From bench 19 the counted columns carry 0.75 and the datagen half 0.25.
	if DuelingHeldOutColumnWeight*DuelingSliceColumns != 0.75 || DuelingDatagenQualifierFloor != 0.8 {
		t.Fatal("bench 19 weighting constants moved; that is a new bench version")
	}
	for v, want := range map[int]float64{DuelingMinBenchVersion: 0.1, 17: 0.1, BenchVersionV18: 0.1, BenchVersionV19: 0.15, BenchVersionV19 + 4: 0.15} {
		if got := DuelingColumnWeightForVersion(v); got != want {
			t.Fatalf("DuelingColumnWeightForVersion(%d) = %v, want %v", v, got, want)
		}
	}
	for _, raw := range []float64{0, 0.5, 0.8, 1} {
		if DuelingDatagenQualifier(BenchVersionV18, raw) != 1 {
			t.Fatalf("bench 18 qualifier at %v must be 1", raw)
		}
	}
	for raw, want := range map[float64]float64{0: 0, 0.4: 0.5, 0.8: 1, 0.99: 1} {
		if got := DuelingDatagenQualifier(BenchVersionV19, raw); got != want {
			t.Fatalf("bench 19 qualifier at %v = %v, want %v", raw, got, want)
		}
	}
	// A floor, never an enumeration.
	for v, want := range map[int]bool{DuelingMinBenchVersion - 1: false, DuelingMinBenchVersion: true, DuelingMinBenchVersion + 7: true} {
		if DuelingBenchVersion(v) != want {
			t.Fatalf("DuelingBenchVersion(%d) = %v", v, !want)
		}
	}
}
