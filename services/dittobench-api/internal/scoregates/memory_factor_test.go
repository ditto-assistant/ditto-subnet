package scoregates

import "testing"

const mib = uint64(1) << 20

func TestMemoryFactorCurve(t *testing.T) {
	for _, tc := range []struct {
		name string
		p95  uint64
		want int
	}{
		{"well under the allowance", 175 * mib, 10000},
		{"at the allowance", 512 * mib, 10000},
		{"a quarter of the band", 896 * mib, 9750},
		{"half of the band", 1280 * mib, 9500},
		{"at the cap", 2048 * mib, 9000},
		{"past the cap never goes below the floor", 4096 * mib, 9000},
	} {
		if got := MemoryFactorBPS(tc.p95, 512*mib, 2048*mib, 9000); got != tc.want {
			t.Fatalf("%s: got %d want %d", tc.name, got, tc.want)
		}
	}
	// Half a basis point rounds away from zero, as the Python mirror does.
	if got := MemoryFactorBPS(1, 0, 2000, 9000); got != 9999 {
		t.Fatalf("half-bps rounding = %d", got)
	}
	// A cap inside the allowance leaves no penalised band.
	if got := MemoryFactorBPS(900*mib, 1024*mib, 863*mib, 9000); got != 10000 {
		t.Fatalf("cap below allowance = %d", got)
	}
}

func TestShadowRecordsButNeverApplies(t *testing.T) {
	summary := BuildMemoryFactor(DefaultMemoryFactorPolicy(), 1280*mib, 1400*mib, 2048*mib, 300)
	if summary == nil || summary.Posture != MemoryFactorPostureShadow || summary.Applied {
		t.Fatalf("shadow summary = %+v", summary)
	}
	if summary.FactorBPS != 9500 || summary.AppliedFactorBPS != 10000 || summary.Result != MemoryFactorResultMeasured {
		t.Fatalf("shadow factor = %+v", summary)
	}
	if MemoryFactorMultiplier(summary) != 1 {
		t.Fatal("shadow changed the composite")
	}
	// An unknown posture reads as shadow, never as enforce.
	odd := BuildMemoryFactor(MemoryFactorPolicy{Posture: "bogus", FreeMiB: 512, FloorBPS: 9000}, 2048*mib, 2048*mib, 2048*mib, 1)
	if odd.Posture != MemoryFactorPostureShadow || odd.Applied || MemoryFactorMultiplier(odd) != 1 {
		t.Fatalf("unknown posture applied: %+v", odd)
	}
}

func TestEnforceAppliesTheFactor(t *testing.T) {
	policy := MemoryFactorPolicy{Posture: MemoryFactorPostureEnforce, FreeMiB: 512, FloorBPS: 9000}
	summary := BuildMemoryFactor(policy, 2048*mib, 2048*mib, 2048*mib, 300)
	if !summary.Applied || summary.AppliedFactorBPS != 9000 || MemoryFactorMultiplier(summary) != 0.9 {
		t.Fatalf("enforce summary = %+v", summary)
	}
}

func TestUnmeasuredRunsKeepTheirComposite(t *testing.T) {
	policy := MemoryFactorPolicy{Posture: MemoryFactorPostureEnforce, FreeMiB: 512, FloorBPS: 9000}
	none := BuildMemoryFactor(policy, 0, 0, 2048*mib, 0)
	if none.Result != MemoryFactorResultNoTelemetry || none.Applied || MemoryFactorMultiplier(none) != 1 {
		t.Fatalf("unmeasured run penalised: %+v", none)
	}
	noCap := BuildMemoryFactor(policy, 900*mib, 900*mib, 0, 10)
	if noCap.Result != MemoryFactorResultNoTelemetry || MemoryFactorMultiplier(noCap) != 1 {
		t.Fatalf("unknown cap penalised: %+v", noCap)
	}
}

func TestOffRecordsNothing(t *testing.T) {
	if BuildMemoryFactor(MemoryFactorPolicy{Posture: MemoryFactorPostureOff}, 2048*mib, 2048*mib, 2048*mib, 1) != nil {
		t.Fatal("off posture recorded a block")
	}
	if MemoryFactorMultiplier(nil) != 1 {
		t.Fatal("nil block changed the composite")
	}
}
