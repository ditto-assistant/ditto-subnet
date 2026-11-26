package protocol

// Dueling bench contract constants (docs/dueling-bench.md section 7.1).
//
// From DuelingMinBenchVersion a canonical agent run is a datagen half plus a
// generator slice: DuelingSliceColumns generator columns of
// DuelingSliceCasesPerColumn cases each, pinned per season. Each counted
// column carries DuelingColumnWeightForVersion of the composite; a masked
// column's weight returns to the datagen half, so no column ever carries more.
// These are contract constants: changing any of them is a new bench version.
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
	// DuelingColumnWeight is one column's share of the composite on bench
	// 16..18 (0.5 / DuelingSliceColumns). Read DuelingColumnWeightForVersion.
	DuelingColumnWeight = 0.1
	// DuelingHeldOutMinBenchVersion is the first bench version that moves rank
	// to the held-out generator columns (docs/dueling-bench.md section 9):
	// DuelingHeldOutColumnWeight per column and the datagen qualifier.
	DuelingHeldOutMinBenchVersion = 19
	// DuelingHeldOutColumnWeight is one column's share of the composite from
	// bench 19 (0.75 / DuelingSliceColumns): the datagen half keeps 0.25.
	DuelingHeldOutColumnWeight = 0.15
	// DuelingDatagenQualifierFloor is the datagen score from which the bench
	// >= 19 qualifier is 1. Below it the whole composite scales by
	// datagen_raw / floor, so the public bench cannot be skipped.
	DuelingDatagenQualifierFloor = 0.80
)

// DuelingBenchVersion reports whether bench version v carries a generator
// slice in its canonical run.
func DuelingBenchVersion(v int) bool { return v >= DuelingMinBenchVersion }

// DuelingHeldOutBenchVersion reports whether bench version v weights the
// held-out columns at DuelingHeldOutColumnWeight and applies the datagen
// qualifier. A floor, like every dueling gate.
func DuelingHeldOutBenchVersion(v int) bool { return v >= DuelingHeldOutMinBenchVersion }

// DuelingColumnWeightForVersion is one counted column's share of the composite
// at bench version v: 0.1 on bench 16..18 and 0.15 from bench 19. Every
// consumer reads the weight a report was scored with from its column_weight.
func DuelingColumnWeightForVersion(v int) float64 {
	if DuelingHeldOutBenchVersion(v) {
		return DuelingHeldOutColumnWeight
	}
	return DuelingColumnWeight
}

// DuelingDatagenQualifier is the bench >= 19 factor on the whole composite:
// min(1, datagenRaw / DuelingDatagenQualifierFloor), from the datagen half
// before any gate. It is 1 below bench 19.
func DuelingDatagenQualifier(v int, datagenRaw float64) float64 {
	if !DuelingHeldOutBenchVersion(v) {
		return 1
	}
	q := datagenRaw / float64(DuelingDatagenQualifierFloor)
	if q > 1 {
		return 1
	}
	return q
}

// DuelingDetails is the details.dueling block of a bench >= 16 score report
// (docs/dueling-bench.md section 7.5), mirrored by
// ditto_screening_protocol.dueling.DuelingReport.
//
// The reported composite is the self-masked composite
// challenge.DuelingComposite(Weighting(), DatagenRaw, DatagenGate, Columns,
// {OwnerKey}), quantized to micros like every v9+ composite. ColumnWeight is
// the bench version's DuelingColumnWeightForVersion. DatagenQualifier is
// present from bench 19 only: min(1, DatagenRaw / DuelingDatagenQualifierFloor),
// a factor on the whole composite kept apart from DatagenGate; absent, it is 1,
// so a bench 16..18 report is unchanged. DatagenRaw is the datagen half
// before any gate (0.5 x tool_mean + 0.5 x memory_mean); DatagenGate is the
// product of every gate factor, all computed from datagen cases only, and the
// applied priced inference cost factor (details.priced_cost), which prices the
// whole run and is floored above zero.
// Generator case text never leaves the scorer: only these aggregates do.
type DuelingDetails struct {
	SeasonID     string  `json:"season_id"`
	SeasonIndex  int64   `json:"season_index"`
	SliceSHA256  string  `json:"slice_sha256"`
	OwnerKey     string  `json:"owner_key"`
	DatagenRaw   float64 `json:"datagen_raw"`
	DatagenGate  float64 `json:"datagen_gate"`
	ColumnWeight float64 `json:"column_weight"`
	// DatagenQualifier is a pointer so a bench 19 qualifier of 0 is still
	// emitted; nil (omitted) below bench 19.
	DatagenQualifier *float64               `json:"datagen_qualifier,omitempty"`
	Columns          []DuelingColumnDetails `json:"columns"`
	CompositeStderr  float64                `json:"composite_stderr"`
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
	// BudgetExhausted counts this column's cases the run did not reach
	// because its priced-cost budget was spent (scored 0, inside N). Monitoring
	// only: column statistics never feed generator pay.
	BudgetExhausted int `json:"budget_exhausted,omitempty"`
}
