package protocol

import "testing"

// RotateSeed must be a deterministic pure function so any auditor recomputes the
// same generation stream from (seed, bench_version).
func TestRotateSeedDeterministic(t *testing.T) {
	for _, s := range []int64{0, 1, -1, 123456789, 1 << 40} {
		if RotateSeed(s) != RotateSeed(s) {
			t.Fatalf("RotateSeed(%d) is not deterministic", s)
		}
	}
}

// The rotation must actually perturb the seed (otherwise it is a no-op and the
// per-version treadmill does nothing), and distinct seeds must stay distinct.
func TestRotateSeedActiveAndInjective(t *testing.T) {
	if RotateSeed(123456789) == 123456789 {
		t.Fatal("RotateSeed is the identity; the version rotation is inert")
	}
	seen := map[int64]int64{}
	for _, s := range []int64{0, 1, 2, 3, 100, 123456789, -42, 1 << 50} {
		r := RotateSeed(s)
		if prev, ok := seen[r]; ok {
			t.Fatalf("RotateSeed collided: %d and %d both map to %d", prev, s, r)
		}
		seen[r] = s
	}
}

func TestVersionedRotationAndEpoch(t *testing.T) {
	const seed = int64(123456789)
	v2, err := RotateSeedForVersion(seed, BenchVersionV2)
	if err != nil {
		t.Fatal(err)
	}
	v3, err := RotateSeedForVersion(seed, BenchVersionV3)
	if err != nil {
		t.Fatal(err)
	}
	if v2 != RotateSeed(seed) {
		t.Fatal("legacy v2 rotation changed")
	}
	if v2 == v3 {
		t.Fatal("version bump did not rotate seed")
	}
	e2, _ := DatasetEpochForVersion(BenchVersionV2)
	e3, _ := DatasetEpochForVersion(BenchVersionV3)
	e7, _ := DatasetEpochForVersion(BenchVersionV7)
	e8, _ := DatasetEpochForVersion(BenchVersionV8)
	e9, _ := DatasetEpochForVersion(BenchVersionV9)
	e10, _ := DatasetEpochForVersion(BenchVersionV10)
	if !e3.After(e2) {
		t.Fatalf("v3 epoch %s must follow v2 %s", e3, e2)
	}
	if !e7.After(e3) || !e8.After(e7) || !e9.After(e8) || !e10.After(e9) || CurrentBenchVersion != BenchVersionV8 {
		t.Fatalf("v10 generation scaffold or v8 activation boundary invalid: v7=%s v8=%s v9=%s v10=%s current=%d", e7, e8, e9, e10, CurrentBenchVersion)
	}
	if _, err := RotateSeedForVersion(seed, 99); err == nil {
		t.Fatal("unsupported rotation version accepted")
	}
	if _, err := DatasetEpochForVersion(99); err == nil {
		t.Fatal("unsupported epoch version accepted")
	}
}
