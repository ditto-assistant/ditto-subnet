package protocol

import (
	"strings"
	"testing"
	"time"
)

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
	e12, _ := DatasetEpochForVersion(BenchVersionV12)
	e13, _ := DatasetEpochForVersion(BenchVersionV13)
	if !e12.After(e10) || !e13.After(e12) || CurrentBenchVersion != BenchVersionV8 {
		t.Fatalf("v13 generation scaffold or v8 activation boundary invalid: v12=%s v13=%s current=%d", e12, e13, CurrentBenchVersion)
	}
	if _, err := RotateSeedForVersion(seed, 99); err == nil {
		t.Fatal("unsupported rotation version accepted")
	}
	if _, err := DatasetEpochForVersion(99); err == nil {
		t.Fatal("unsupported epoch version accepted")
	}
}

// TestSupportedBenchVersionsAreOneDerivedList pins the bump discipline: every
// enumeration this module exposes derives from one ordered list, the newest
// entry is what probes default to, the error strings render the same list, and
// the version just past the newest is rejected everywhere.
func TestSupportedBenchVersionsAreOneDerivedList(t *testing.T) {
	versions := SupportedBenchVersions()
	if len(versions) == 0 || versions[0] != BenchVersionV2 {
		t.Fatalf("supported versions must start at v2: %v", versions)
	}
	for i := 1; i < len(versions); i++ {
		if versions[i] != versions[i-1]+1 {
			t.Fatalf("supported versions must be contiguous: %v", versions)
		}
	}
	newest := NewestSupportedBenchVersion()
	if newest != versions[len(versions)-1] || newest != BenchVersionV16 {
		t.Fatalf("newest supported version = %d, want %d (last of %v)", newest, BenchVersionV16, versions)
	}
	for _, v := range versions {
		if !SupportedBenchVersion(v) {
			t.Fatalf("listed version %d not supported", v)
		}
		if _, err := DatasetEpochForVersion(v); err != nil {
			t.Fatalf("listed version %d has no epoch: %v", v, err)
		}
		if _, err := RotateSeedForVersion(1, v); err != nil {
			t.Fatalf("listed version %d cannot rotate: %v", v, err)
		}
	}
	if SupportedBenchVersion(newest + 1) {
		t.Fatalf("v%d accepted before it has a contract", newest+1)
	}
	// The first version past every staged contract has no contract at all.
	beyond := NewestGeneratableBenchVersion() + 1
	want := "supported: " + SupportedBenchVersionList()
	if _, err := DatasetEpochForVersion(beyond); err == nil || !strings.Contains(err.Error(), want) {
		t.Fatalf("epoch error must render the derived list %q: %v", want, err)
	}
	if _, err := RotateSeedForVersion(1, beyond); err == nil || !strings.Contains(err.Error(), want) {
		t.Fatalf("rotation error must render the derived list %q: %v", want, err)
	}
	if got := SupportedBenchVersionList(); got != "2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16" {
		t.Fatalf("SupportedBenchVersionList() = %q", got)
	}
	// Epochs advance monotonically with the version so no two contracts share a
	// reference instant.
	for i := 1; i < len(versions); i++ {
		prev, _ := DatasetEpochForVersion(versions[i-1])
		cur, _ := DatasetEpochForVersion(versions[i])
		if !cur.After(prev) {
			t.Fatalf("v%d epoch %s does not follow v%d epoch %s", versions[i], cur, versions[i-1], prev)
		}
	}
}

// TestStagedBenchVersionsAreGeneratableNotSupported pins the staging rule: a
// staged contract continues the supported list contiguously, generates with
// its own epoch and rotate seed, and is invisible to every supported-set
// helper until the cross-layer PR promotes it into supportedBenchVersions.
// v16 was promoted by the flip that raised the shared ceiling to 16; its
// epoch and rotation are pinned here so promotion provably moved no byte.
func TestStagedBenchVersionsAreGeneratableNotSupported(t *testing.T) {
	staged := StagedBenchVersions()
	prev := NewestSupportedBenchVersion()
	prevEpoch, _ := DatasetEpochForVersion(prev)
	for _, v := range staged {
		if v != prev+1 {
			t.Fatalf("staged v%d does not continue v%d", v, prev)
		}
		if SupportedBenchVersion(v) || !StagedBenchVersion(v) || !GeneratableBenchVersion(v) {
			t.Fatalf("v%d must be staged and generatable but not supported", v)
		}
		epoch, err := DatasetEpochForVersion(v)
		if err != nil || !epoch.After(prevEpoch) {
			t.Fatalf("staged v%d epoch %s (err %v) must follow %s", v, epoch, err, prevEpoch)
		}
		rotated, err := RotateSeedForVersion(123456789, v)
		if err != nil {
			t.Fatalf("staged v%d cannot rotate: %v", v, err)
		}
		for _, s := range SupportedBenchVersions() {
			other, _ := RotateSeedForVersion(123456789, s)
			if other == rotated {
				t.Fatalf("staged v%d shares a rotate seed with v%d", v, s)
			}
		}
		prev, prevEpoch = v, epoch
	}
	if len(staged) == 0 && NewestGeneratableBenchVersion() != NewestSupportedBenchVersion() {
		t.Fatalf("nothing staged, yet newest generatable %d != newest supported %d", NewestGeneratableBenchVersion(), NewestSupportedBenchVersion())
	}
	// v16 left staging with the epoch it was staged with.
	if !SupportedBenchVersion(BenchVersionV16) || StagedBenchVersion(BenchVersionV16) {
		t.Fatal("v16 must be supported and no longer staged")
	}
	if got, _ := DatasetEpochForVersion(BenchVersionV16); got.Format(time.RFC3339) != "2027-08-01T00:00:00Z" {
		t.Fatalf("v16 epoch = %s", got)
	}
}
