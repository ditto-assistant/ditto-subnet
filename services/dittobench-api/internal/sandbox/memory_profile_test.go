package sandbox

import (
	"testing"
	"time"
)

// SN-500: the profile's percentiles are time-weighted, so the faster sampling
// near the cap cannot drag P50/P95 upward.
func TestMemoryProfileIsTimeWeighted(t *testing.T) {
	w := &memoryWatch{done: make(chan struct{})}
	low, high := uint64(300<<20), uint64(1900<<20)
	// 90 s at 300 MiB, then 40 quarter-interval samples (10 s) at 1900 MiB.
	for i := 0; i < 45; i++ {
		w.recordUsage(&low, 2*time.Second)
	}
	for i := 0; i < 40; i++ {
		w.recordUsage(&high, 500*time.Millisecond)
	}
	peak := uint64(1950 << 20)
	w.recordCgroup(RuntimeDiagnostics{MemoryPeakBytes: &peak})

	profile := w.profile(2048 << 20)
	if profile == nil {
		t.Fatal("no profile")
	}
	if profile.Samples != 85 || profile.SampledSeconds != 110 {
		t.Fatalf("samples=%d seconds=%v", profile.Samples, profile.SampledSeconds)
	}
	// 90 of 110 seconds sit at 300 MiB: the median is the low level even
	// though most samples were taken at the high one.
	if profile.P50Bytes != 301<<20 {
		t.Fatalf("p50 = %d MiB", profile.P50Bytes>>20)
	}
	if profile.P95Bytes != 1901<<20 {
		t.Fatalf("p95 = %d MiB", profile.P95Bytes>>20)
	}
	if profile.PeakBytes != peak || profile.LimitBytes != 2048<<20 {
		t.Fatalf("peak=%d limit=%d", profile.PeakBytes, profile.LimitBytes)
	}
}

func TestMemoryProfilePercentilesNeverExceedThePeak(t *testing.T) {
	w := &memoryWatch{done: make(chan struct{})}
	current := uint64(1000<<20) + 7
	w.recordUsage(&current, time.Second)
	w.recordCgroup(RuntimeDiagnostics{MemoryPeakBytes: &current})
	profile := w.profile(0)
	if profile.P50Bytes != current || profile.P95Bytes != current || profile.PeakBytes != current {
		t.Fatalf("percentile above the kernel peak: %+v", profile)
	}
}

func TestMemoryProfileIsNilWithoutSamples(t *testing.T) {
	var nilWatch *memoryWatch
	if nilWatch.profile(1) != nil {
		t.Fatal("nil watch produced a profile")
	}
	if (&memoryWatch{}).profile(1) != nil {
		t.Fatal("empty watch produced a profile")
	}
	if (&Handle{}).MemoryProfile() != nil {
		t.Fatal("handle without a sampler produced a profile")
	}
	var nilHandle *Handle
	if nilHandle.MemoryProfile() != nil {
		t.Fatal("nil handle produced a profile")
	}
}

func TestFrozenWatchIgnoresLaterUsage(t *testing.T) {
	w := &memoryWatch{done: make(chan struct{})}
	first, later := uint64(100<<20), uint64(4000<<20)
	w.recordUsage(&first, time.Second)
	w.freeze()
	w.recordUsage(&later, time.Second)
	if profile := w.profile(0); profile.Samples != 1 || profile.P95Bytes != 101<<20 {
		t.Fatalf("post-stop sample counted: %+v", profile)
	}
}

func TestParseRuntimeMetricsKeepsMemoryCurrent(t *testing.T) {
	var d RuntimeDiagnostics
	parseRuntimeMetrics(&d, "__memory_peak__\n900\n__memory_current__\n700\n")
	if d.MemoryCurrentBytes == nil || *d.MemoryCurrentBytes != 700 {
		t.Fatalf("current = %v", d.MemoryCurrentBytes)
	}
	if d.MemoryPeakBytes == nil || *d.MemoryPeakBytes != 900 {
		t.Fatalf("peak = %v", d.MemoryPeakBytes)
	}
}
