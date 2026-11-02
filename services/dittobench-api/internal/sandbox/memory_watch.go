package sandbox

import (
	"bufio"
	"bytes"
	"context"
	"os"
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
