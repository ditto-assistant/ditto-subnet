package sandbox

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// defaultMemoryWatchInterval paces the sampler on a production scorer. Near
// the cap it samples four times as often, because the evidence that decides an
// OOM verdict is the last snapshot before the kernel removes the cgroup.
const defaultMemoryWatchInterval = 2 * time.Second

// hostSampleWindow is how many recent host MemAvailable readings bound the
// "at kill time" minimum: about 4-16 seconds before the container stopped.
const hostSampleWindow = 8

// memoryWatch keeps the newest cgroup snapshot and the recent host
// MemAvailable readings for one running container. It freezes when the
// container stops, so a reading taken after the kernel freed the killed
// harness's memory can never stand in for the host state at the kill.
type memoryWatch struct {
	mu       sync.Mutex
	events   map[string]uint64
	peak     *uint64
	host     []uint64
	frozen   bool
	done     chan struct{}
	stopOnce sync.Once
	// usage is a time-weighted histogram of memory.current in MiB buckets:
	// seconds spent at each level (SN-500). Weighting by the interval that
	// followed each sample keeps the faster sampling near the cap from
	// skewing P50/P95 upward. Bounded by the cap, so it never decimates.
	usage        map[uint64]float64
	usageSamples int
	usageSeconds float64
}

// mibBytes is one MiB in bytes, the histogram's bucket width.
const mibBytes = 1 << 20

// MemoryProfile summarises one harness run's cgroup memory (SN-500): the
// kernel's high-water mark and time-weighted percentiles of memory.current.
type MemoryProfile struct {
	PeakBytes      uint64
	P50Bytes       uint64
	P95Bytes       uint64
	Samples        int
	SampledSeconds float64
	LimitBytes     uint64
}

func (d *LocalDocker) readHostMemAvailable() (uint64, bool) {
	if d.hostMemAvailable != nil {
		return d.hostMemAvailable()
	}
	return procMemAvailable()
}

// procMemAvailable reads the kernel-wide MemAvailable. The scorer shares its
// kernel with the nested sandbox daemon, so this is the harness host's figure.
func procMemAvailable() (uint64, bool) {
	raw, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return 0, false
	}
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) >= 2 && fields[0] == "MemAvailable:" {
			kib, err := strconv.ParseUint(fields[1], 10, 64)
			return kib * 1024, err == nil
		}
	}
	return 0, false
}

func (d *LocalDocker) startMemoryWatch(containerID string, limitBytes uint64) *memoryWatch {
	if d.MemoryWatchInterval <= 0 {
		return nil
	}
	w := &memoryWatch{done: make(chan struct{})}
	go d.runMemoryWatch(w, containerID, limitBytes)
	return w
}

func (d *LocalDocker) runMemoryWatch(w *memoryWatch, containerID string, limitBytes uint64) {
	const script = `
printf '%s\n' __memory_events__
cat /sys/fs/cgroup/memory.events 2>/dev/null || true
printf '%s\n' __memory_peak__
cat /sys/fs/cgroup/memory.peak 2>/dev/null || true
printf '%s\n' __memory_current__
cat /sys/fs/cgroup/memory.current 2>/dev/null || true
`
	for {
		interval := d.MemoryWatchInterval
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		out, err := d.dockerOutput(ctx, "exec", containerID, "/bin/sh", "-c", script)
		if err == nil {
			var sample RuntimeDiagnostics
			parseRuntimeMetrics(&sample, string(out))
			w.recordCgroup(sample)
			if limitBytes > 0 && sample.MemoryPeakBytes != nil && *sample.MemoryPeakBytes*100 >= limitBytes*85 {
				interval /= 4
			}
			w.recordUsage(sample.MemoryCurrentBytes, interval)
		} else if state, inspectErr := d.dockerOutput(ctx, "inspect", "--format", "{{.State.Running}}", containerID); inspectErr != nil ||
			strings.TrimSpace(string(state)) != "true" {
			// Stopped (or gone): freeze before any post-kill host reading lands.
			cancel()
			w.freeze()
			return
		}
		// An image without /bin/sh still gets host readings while it runs.
		cancel()
		w.sampleHost(d.readHostMemAvailable)
		select {
		case <-w.done:
			return
		case <-time.After(interval):
		}
	}
}

func (w *memoryWatch) recordCgroup(sample RuntimeDiagnostics) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.frozen {
		return
	}
	if sample.MemoryPeakBytes != nil && (w.peak == nil || *sample.MemoryPeakBytes > *w.peak) {
		peak := *sample.MemoryPeakBytes
		w.peak = &peak
	}
	for key, value := range sample.MemoryEvents {
		if w.events == nil {
			w.events = make(map[string]uint64)
		}
		if value > w.events[key] {
			w.events[key] = value
		}
	}
}

// recordUsage adds one memory.current reading, weighted by the interval
// until the next reading.
func (w *memoryWatch) recordUsage(current *uint64, interval time.Duration) {
	if current == nil || interval <= 0 {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.frozen {
		return
	}
	if w.usage == nil {
		w.usage = make(map[uint64]float64)
	}
	seconds := interval.Seconds()
	w.usage[*current/mibBytes] += seconds
	w.usageSamples++
	w.usageSeconds += seconds
}

// profile is the run's memory summary, or nil when nothing was sampled.
func (w *memoryWatch) profile(limitBytes uint64) *MemoryProfile {
	if w == nil {
		return nil
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.usageSamples == 0 && w.peak == nil {
		return nil
	}
	profile := &MemoryProfile{
		Samples:        w.usageSamples,
		SampledSeconds: w.usageSeconds,
		LimitBytes:     limitBytes,
	}
	if w.peak != nil {
		profile.PeakBytes = *w.peak
	}
	if w.usageSamples > 0 {
		buckets := make([]uint64, 0, len(w.usage))
		for bucket := range w.usage {
			buckets = append(buckets, bucket)
		}
		sort.Slice(buckets, func(i, j int) bool { return buckets[i] < buckets[j] })
		profile.P50Bytes = weightedQuantile(buckets, w.usage, w.usageSeconds, 0.50)
		profile.P95Bytes = weightedQuantile(buckets, w.usage, w.usageSeconds, 0.95)
	}
	// memory.peak is the kernel's exact high-water mark; a percentile rounded
	// up to its bucket edge must not report more than it.
	if profile.PeakBytes > 0 {
		profile.P50Bytes = min(profile.P50Bytes, profile.PeakBytes)
		profile.P95Bytes = min(profile.P95Bytes, profile.PeakBytes)
	}
	return profile
}

// weightedQuantile is the smallest bucket whose cumulative weight reaches q of
// the total, reported at the bucket's upper edge so it never understates use.
func weightedQuantile(sorted []uint64, weights map[uint64]float64, total float64, q float64) uint64 {
	target := q * total
	cumulative := 0.0
	for _, bucket := range sorted {
		cumulative += weights[bucket]
		if cumulative >= target {
			return (bucket + 1) * mibBytes
		}
	}
	return (sorted[len(sorted)-1] + 1) * mibBytes
}

func (w *memoryWatch) sampleHost(read func() (uint64, bool)) {
	if w == nil {
		return
	}
	value, ok := read()
	if !ok {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.frozen {
		return
	}
	w.host = append(w.host, value)
	if len(w.host) > hostSampleWindow {
		w.host = w.host[len(w.host)-hostSampleWindow:]
	}
}

func (w *memoryWatch) freeze() {
	w.mu.Lock()
	w.frozen = true
	w.mu.Unlock()
}

func (w *memoryWatch) stop() {
	if w == nil {
		return
	}
	w.freeze()
	w.stopOnce.Do(func() { close(w.done) })
}

// mergeInto folds the sampler's evidence into a diagnostics read: the higher
// peak, the larger of each event counter, and the host minimum.
func (w *memoryWatch) mergeInto(diagnostics *RuntimeDiagnostics) {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.peak != nil && (diagnostics.MemoryPeakBytes == nil || *w.peak > *diagnostics.MemoryPeakBytes) {
		peak := *w.peak
		diagnostics.MemoryPeakBytes = &peak
	}
	for key, value := range w.events {
		if diagnostics.MemoryEvents == nil {
			diagnostics.MemoryEvents = make(map[string]uint64)
		}
		if value > diagnostics.MemoryEvents[key] {
			diagnostics.MemoryEvents[key] = value
		}
	}
	for _, value := range w.host {
		if diagnostics.HostMemAvailableMinBytes == nil || value < *diagnostics.HostMemAvailableMinBytes {
			minimum := value
			diagnostics.HostMemAvailableMinBytes = &minimum
		}
	}
}
