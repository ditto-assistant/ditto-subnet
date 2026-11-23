package scoregates

import (
	"math"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// P95 harness-memory factor (details.memory_factor, SN-500 step 2).
//
// The scorer already measures the harness cgroup through a run: memory.peak
// and a time-weighted histogram of memory.current (internal/sandbox). From
// bench 16 the run's own P95 maps to a composite factor that rides in the
// same composite gate as priced cost:
//
//	P95 <= free                 -> 10000 bps
//	free < P95 < cap            -> 10000 - (10000 - floor) x (P95 - free) / (cap - free)
//	P95 >= cap                  -> floor
//
// with free and floor from the lease's benchmark_runtime (defaults 512 MiB
// and 9000 bps) and cap the harness memory limit the run ran under. A run
// that exceeds its cap is an OOM, a failed run with no score, so the floor is
// the lowest factor a scored run can carry.
//
// The posture comes from the lease, never from the scorer's environment, so
// every validator on one quorum applies the same rule. Shadow (the default,
// and the reading of any unknown posture) records the factor and applies
// 10000; enforce applies it; off records nothing.

const (
	MemoryFactorPostureOff     = "off"
	MemoryFactorPostureShadow  = "shadow"
	MemoryFactorPostureEnforce = "enforce"

	MemoryFactorResultMeasured    = "measured"
	MemoryFactorResultNoTelemetry = "no_telemetry"

	DefaultMemoryFactorFreeMiB  = 512
	DefaultMemoryFactorFloorBPS = 9000

	mibBytes = uint64(1) << 20
	// bpsScale is a factor of 1 in basis points.
	bpsScale = 10000
)

// MemoryFactorPolicy is the lease's factor policy.
type MemoryFactorPolicy struct {
	Posture  string
	FreeMiB  int
	FloorBPS int
}

// DefaultMemoryFactorPolicy is shadow with the published curve.
func DefaultMemoryFactorPolicy() MemoryFactorPolicy {
	return MemoryFactorPolicy{
		Posture:  MemoryFactorPostureShadow,
		FreeMiB:  DefaultMemoryFactorFreeMiB,
		FloorBPS: DefaultMemoryFactorFloorBPS,
	}
}

// MemoryFactorBPS maps a P95 to basis points under the curve above. A cap at
// or below the free allowance leaves no penalised band, so it is 10000.
func MemoryFactorBPS(p95Bytes, freeBytes, limitBytes uint64, floorBPS int) int {
	if p95Bytes <= freeBytes || limitBytes <= freeBytes {
		return bpsScale
	}
	span := limitBytes - freeBytes
	over := min(p95Bytes-freeBytes, span)
	drop := float64(bpsScale-floorBPS) * float64(over) / float64(span)
	return bpsScale - int(math.Round(drop))
}

// BuildMemoryFactor is the details.memory_factor block for one run, or nil
// under the off posture. samples == 0 or an unknown cap means the run's
// memory was not measured: the factor stays 10000 and nothing is applied.
func BuildMemoryFactor(policy MemoryFactorPolicy, p95Bytes, peakBytes, limitBytes uint64, samples int) *protocol.MemoryFactorSummary {
	if policy.Posture == MemoryFactorPostureOff {
		return nil
	}
	posture := policy.Posture
	if posture != MemoryFactorPostureEnforce {
		posture = MemoryFactorPostureShadow
	}
	summary := &protocol.MemoryFactorSummary{
		Posture:          posture,
		Result:           MemoryFactorResultNoTelemetry,
		P95Bytes:         p95Bytes,
		PeakBytes:        peakBytes,
		LimitBytes:       limitBytes,
		FreeBytes:        uint64(max(policy.FreeMiB, 0)) * mibBytes,
		Samples:          samples,
		FloorBPS:         policy.FloorBPS,
		FactorBPS:        bpsScale,
		AppliedFactorBPS: bpsScale,
	}
	if samples == 0 || limitBytes == 0 {
		return summary
	}
	summary.Result = MemoryFactorResultMeasured
	summary.FactorBPS = MemoryFactorBPS(p95Bytes, summary.FreeBytes, limitBytes, policy.FloorBPS)
	if posture == MemoryFactorPostureEnforce {
		summary.Applied = true
		summary.AppliedFactorBPS = summary.FactorBPS
	}
	return summary
}

// MemoryFactorMultiplier is the factor a composite actually carries: 1 unless
// the block was applied.
func MemoryFactorMultiplier(summary *protocol.MemoryFactorSummary) float64 {
	if summary == nil || !summary.Applied {
		return 1
	}
	return float64(summary.AppliedFactorBPS) / float64(bpsScale)
}
