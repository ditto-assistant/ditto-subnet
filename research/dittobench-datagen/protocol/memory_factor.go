package protocol

// MemoryFactorMinBenchVersion is the first bench version whose score report
// carries details.memory_factor (SN-500 step 2). Like priced cost it is a
// floor: every later version inherits the rule unless a contract replaces it.
const MemoryFactorMinBenchVersion = 16

// MemoryFactorBenchVersion reports whether bench version v records the factor.
func MemoryFactorBenchVersion(v int) bool { return v >= MemoryFactorMinBenchVersion }

// MemoryFactorSummary is the details.memory_factor block: the run's own
// time-weighted P95 harness memory mapped to a composite factor.
//
// FactorBPS is 10000 at or below FreeBytes and falls linearly to FloorBPS at
// LimitBytes, the harness cap the run ran under. AppliedFactorBPS is what the
// composite actually carries (inside details.dueling.datagen_gate on a
// dueling run): FactorBPS under the enforce posture, 10000 under shadow or
// when the run's memory was not measured. Unlike priced cost, P95 cannot be
// recomputed from the report; it is the scorer's own cgroup measurement.
type MemoryFactorSummary struct {
	Posture string `json:"posture"`
	Applied bool   `json:"applied"`
	// Result is "measured", or "no_telemetry" when the sampler recorded no
	// memory.current reading; the latter always carries 10000.
	Result string `json:"result"`

	P95Bytes   uint64 `json:"p95_bytes"`
	PeakBytes  uint64 `json:"peak_bytes"`
	LimitBytes uint64 `json:"limit_bytes"`
	FreeBytes  uint64 `json:"free_bytes"`
	Samples    int    `json:"samples"`

	FloorBPS         int `json:"floor_bps"`
	FactorBPS        int `json:"factor_bps"`
	AppliedFactorBPS int `json:"applied_factor_bps"`
}
