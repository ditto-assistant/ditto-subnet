package main

import (
	"math"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// memoryFactorFor builds details.memory_factor (SN-500 step 2) from the run's
// own memory profile under the lease's policy. nil before bench 16 and under
// the off posture. A run without a sandbox, or whose sampler read nothing, is
// recorded as no_telemetry and keeps its full composite.
func memoryFactorFor(benchVersion int, runtime *benchmarkRuntimeSettings, handle *sandbox.Handle) *protocol.MemoryFactorSummary {
	if !protocol.MemoryFactorBenchVersion(benchVersion) {
		return nil
	}
	var p95, peak, limit uint64
	samples := 0
	if profile := handle.MemoryProfile(); profile != nil {
		p95, peak, limit, samples = profile.P95Bytes, profile.PeakBytes, profile.LimitBytes, profile.Samples
	}
	if limit == 0 && runtime != nil && runtime.SandboxMemoryLimitMiB > 0 {
		limit = uint64(runtime.SandboxMemoryLimitMiB) << 20
	}
	return scoregates.BuildMemoryFactor(runtime.memoryFactorPolicy(), p95, peak, limit, samples)
}

// applyMemoryFactorToHalf folds the applied memory factor into the datagen
// half's composite gate, beside priced cost, so the dueling composite and its
// masked recomputations (dethrone, title duel) all carry it.
func applyMemoryFactorToHalf(half scorer.DatagenHalf, summary *protocol.MemoryFactorSummary) scorer.DatagenHalf {
	half.Gate = half.Gate * scoregates.MemoryFactorMultiplier(summary)
	return half
}

// applyMemoryFactorToReport scales a non-dueling bench 16+ report's composite
// (a practice run without a slice), quantized to micros, before the v9 base
// evidence binds it.
func applyMemoryFactorToReport(report protocol.ScoreReport, summary *protocol.MemoryFactorSummary) protocol.ScoreReport {
	m := scoregates.MemoryFactorMultiplier(summary)
	if m == 1 {
		return report
	}
	report.Composite = math.Round(report.Composite*m*1e6) / 1e6
	report.CompositeStderr = math.Round(report.CompositeStderr*m*1e6) / 1e6
	return report
}
