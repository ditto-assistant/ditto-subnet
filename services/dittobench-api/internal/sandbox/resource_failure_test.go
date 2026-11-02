package sandbox

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func u64(v uint64) *uint64 { return &v }

// TestResourceFailureRules pins the published OOM rule: the miner is charged
// only on an OOM kill at its own cap while the host had headroom; every gap in
// that evidence is the validator's.
func TestResourceFailureRules(t *testing.T) {
	limit := uint64(8 << 30)
	floor := uint64(PublishedEnvelope.OOM.HostAvailableFloorMiB) * mib
	tolerance := uint64(PublishedEnvelope.OOM.PeakToleranceMiB) * mib
	cases := []struct {
		name       string
		diag       RuntimeDiagnostics
		code       string
		minerFault bool
	}{
		{"cap reached, host fine", RuntimeDiagnostics{
			OOMKilled: true, MemoryLimitBytes: u64(limit), MemoryPeakBytes: u64(limit - tolerance), HostMemAvailableMinBytes: u64(16 << 30),
		}, CodeHarnessOOM, true},
		{"memcg oom event proves the cap without a peak", RuntimeDiagnostics{
			MemoryEvents: map[string]uint64{"oom": 1, "oom_kill": 1}, MemoryLimitBytes: u64(limit), HostMemAvailableMinBytes: u64(16 << 30),
		}, CodeHarnessOOM, true},
		{"kill below the cap is the host's", RuntimeDiagnostics{
			OOMKilled: true, MemoryLimitBytes: u64(limit), MemoryPeakBytes: u64(limit - tolerance - 1), HostMemAvailableMinBytes: u64(16 << 30),
		}, CodeHostMemoryPressure, false},
		{"cap reached while host was short", RuntimeDiagnostics{
			OOMKilled: true, MemoryLimitBytes: u64(limit), MemoryPeakBytes: u64(limit), HostMemAvailableMinBytes: u64(floor),
		}, CodeHostMemoryPressure, false},
		{"no host evidence never charges the miner", RuntimeDiagnostics{
			OOMKilled: true, MemoryLimitBytes: u64(limit), MemoryPeakBytes: u64(limit),
		}, CodeHostMemoryPressure, false},
		{"bare 137 is infrastructure", RuntimeDiagnostics{
			StateKnown: true, ExitCode: 137,
		}, CodeHarnessKilled137, false},
	}
	for _, tc := range cases {
		verdict, ok := tc.diag.ResourceFailure()
		if !ok || verdict.Code != tc.code || verdict.MinerFault != tc.minerFault {
			t.Errorf("%s: got %+v ok=%t, want %s minerFault=%t", tc.name, verdict, ok, tc.code, tc.minerFault)
		}
	}
}

// TestDiagnosticsKeepsPreKillEvidenceAfterExit is the bug the sampler fixes: a
// killed harness takes its cgroup with it, and a host reading taken after the
// kernel freed its memory must not stand in for the host at the kill.
func TestDiagnosticsKeepsPreKillEvidenceAfterExit(t *testing.T) {
	d := NewLocalDocker()
	d.MemoryWatchInterval = time.Millisecond
	var running atomic.Bool
	running.Store(true)
	var host atomic.Uint64
	host.Store(512 << 20) // under the floor while the harness lives
	d.hostMemAvailable = func() (uint64, bool) { return host.Load(), true }
	d.dockerCommand = func(_ context.Context, args ...string) ([]byte, error) {
		switch args[0] {
		case "exec":
			if !running.Load() {
				return nil, errors.New("container is not running")
			}
			return []byte("__memory_events__\noom 0\noom_kill 0\n__memory_peak__\n8589934592\n"), nil
		case "inspect":
			if args[2] == "{{.State.Running}}" {
				if running.Load() {
					return []byte("true"), nil
				}
				return []byte("false"), nil
			}
			return []byte(`{"Running":false,"OOMKilled":true,"ExitCode":137}`), nil
		}
		t.Fatalf("unexpected docker command: %v", args)
		return nil, nil
	}
	h := &Handle{ContainerID: "opaque", memoryLimitBytes: 8 << 30}
	h.watch = d.startMemoryWatch(h.ContainerID, h.memoryLimitBytes)
	deadline := time.Now().Add(2 * time.Second)
	for {
		h.watch.mu.Lock()
		sampled := h.watch.peak != nil && len(h.watch.host) > 0
		h.watch.mu.Unlock()
		if sampled || time.Now().After(deadline) {
			break
		}
		time.Sleep(time.Millisecond)
	}
	running.Store(false)
	time.Sleep(20 * time.Millisecond)
	host.Store(32 << 30) // memory freed after the kill
	diagnostics := d.Diagnostics(context.Background(), h)
	h.watch.stop()
	verdict, ok := diagnostics.ResourceFailure()
	if !ok || verdict.Code != CodeHostMemoryPressure {
		t.Fatalf("pre-kill host pressure lost: %+v %+v", verdict, diagnostics)
	}
	if diagnostics.MemoryPeakBytes == nil || *diagnostics.MemoryPeakBytes != 8<<30 {
		t.Fatalf("pre-kill peak lost: %+v", diagnostics)
	}
}

func TestRuntimeEnvelopeMatchesPublishedSource(t *testing.T) {
	source := filepath.Join("..", "..", "..", "..", "packages", "ditto-screening-protocol",
		"ditto_screening_protocol", "data", "runtime_envelope.json")
	published, err := os.ReadFile(source)
	if err != nil {
		t.Skipf("published envelope not in this checkout: %v", err)
	}
	if !bytes.Equal(published, runtimeEnvelopeJSON) {
		t.Fatalf("internal/sandbox/runtime_envelope.json drifted from %s; copy it", source)
	}
}
