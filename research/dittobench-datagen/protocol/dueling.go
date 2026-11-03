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
