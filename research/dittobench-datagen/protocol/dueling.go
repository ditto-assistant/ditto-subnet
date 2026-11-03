package protocol

// Dueling bench contract constants (docs/dueling-bench.md section 7.1).
//
// From DuelingMinBenchVersion a canonical agent run is a datagen half plus a
// generator slice: DuelingSliceColumns generator columns of
// DuelingSliceCasesPerColumn cases each, pinned per season. Each counted
// column carries DuelingColumnWeight of the composite; a masked column's
// weight returns to the datagen half, so no column ever carries more. These
// are contract constants: changing any of them is a new bench version.
//
// The constants exist ahead of the bench version that activates them, so the
// season, slice and composite primitives (challenge/season.go,
// challenge/dueling_score.go) can land without advertising the version. Every
// dueling gate is a floor on DuelingMinBenchVersion, never an equality.
const (
	// DuelingMinBenchVersion is the first bench version whose canonical run
	// carries a generator slice.
	DuelingMinBenchVersion = 16
	// DuelingSliceColumns is the fixed number of generator columns in a slice.
	// The public default generator fills every slot no eligible miner
	// generator takes, so a slice always has exactly this many columns.
	DuelingSliceColumns = 5
	// DuelingSliceCasesPerColumn is the case count every slice column's
	// generate request asks for.
	DuelingSliceCasesPerColumn = 35
	// DuelingColumnWeight is one column's share of the composite
	// (0.5 / DuelingSliceColumns).
	DuelingColumnWeight = 0.1
)

// DuelingBenchVersion reports whether bench version v carries a generator
// slice in its canonical run.
func DuelingBenchVersion(v int) bool { return v >= DuelingMinBenchVersion }

// DuelingDetails is the details.dueling block of a bench >= 16 score report
// (docs/dueling-bench.md section 7.5), mirrored by
// ditto_screening_protocol.dueling.DuelingReport.
//
// The reported composite is the self-masked composite
// challenge.DuelingComposite(DatagenRaw, DatagenGate, Columns, {OwnerKey}),
// quantized to micros like every v9+ composite. DatagenRaw is the datagen half
// before any gate (0.5 x tool_mean + 0.5 x memory_mean); DatagenGate is the
// product of every gate factor, all computed from datagen cases only.
// Generator case text never leaves the scorer: only these aggregates do.
type DuelingDetails struct {
	SeasonID        string                 `json:"season_id"`
	SeasonIndex     int64                  `json:"season_index"`
	SliceSHA256     string                 `json:"slice_sha256"`
	OwnerKey        string                 `json:"owner_key"`
	DatagenRaw      float64                `json:"datagen_raw"`
	DatagenGate     float64                `json:"datagen_gate"`
	ColumnWeight    float64                `json:"column_weight"`
	Columns         []DuelingColumnDetails `json:"columns"`
	CompositeStderr float64                `json:"composite_stderr"`
}

// DuelingColumnDetails is one slice column's per-run statistics. N counts the
// column's executed cases after the slice dedupe; Sum and SumSq are the sums of
// their scores and squared scores, a failed /run counting as 0. OwnerKey is ""
// for the public default generator (including a substituted column).
type DuelingColumnDetails struct {
	ColumnID            string  `json:"column_id"`
	OwnerKey            string  `json:"owner_key"`
	SubmissionID        string  `json:"submission_id"`
	GradingBenchVersion int     `json:"grading_bench_version"`
	N                   int     `json:"n"`
	Sum                 float64 `json:"sum"`
	SumSq               float64 `json:"sum_sq"`
	Substituted         bool    `json:"substituted"`
}
