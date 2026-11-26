package challenge

import (
	"errors"
	"fmt"
	"math"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// DuelingColumnAggregate is one slice column's per-run case statistics, as the
// scorer reports them in details.dueling and the validator fold reads them
// (docs/dueling-bench.md section 7.3). N counts the column's executed cases
// (after SliceExecutionCases dedupe); Sum and SumSq are the sums of their case
// scores and squared case scores, a failed /run counting as 0.
type DuelingColumnAggregate struct {
	ColumnID string `json:"column_id"`
	// OwnerKey is the column generator's owner; "" for the public default
	// generator (including a substituted column), which is never masked.
	OwnerKey    string  `json:"owner_key"`
	N           int     `json:"n"`
	Sum         float64 `json:"sum"`
	SumSq       float64 `json:"sum_sq"`
	Substituted bool    `json:"substituted"`
}

// CheckDuelingColumns validates a run's column aggregates at the report
// boundary, before DuelingComposite or DuelingCompositeStderr scores them:
// exactly protocol.DuelingSliceColumns columns with the unique ids c0..c4,
// non-negative counts and finite sums (zero sums when a column has no cases). The composite functions themselves take
// any slice (a subset is how masks and tests exercise them), so every consumer
// of a signed report calls this first; an overlong slice would otherwise give
// the datagen half a negative weight.
func CheckDuelingColumns(columns []DuelingColumnAggregate) error {
	if len(columns) != protocol.DuelingSliceColumns {
		return fmt.Errorf("dueling report needs exactly %d columns, got %d", protocol.DuelingSliceColumns, len(columns))
	}
	seen := map[string]bool{}
	for _, c := range columns {
		ok := false
		for i := 0; i < protocol.DuelingSliceColumns; i++ {
			ok = ok || c.ColumnID == SliceColumnID(i)
		}
		if !ok || seen[c.ColumnID] {
			return fmt.Errorf("dueling report columns must be the unique ids c0..c%d, got %q", protocol.DuelingSliceColumns-1, c.ColumnID)
		}
		seen[c.ColumnID] = true
		if c.Substituted && c.OwnerKey != "" {
			return errors.New("dueling report column " + c.ColumnID + " is substituted but names an owner")
		}
		finite := !math.IsNaN(c.Sum) && !math.IsInf(c.Sum, 0) && !math.IsNaN(c.SumSq) && !math.IsInf(c.SumSq, 0)
		if c.N < 0 || !finite || c.SumSq < 0 || (c.N == 0 && (c.Sum != 0 || c.SumSq != 0)) {
			return errors.New("dueling report column " + c.ColumnID + " has invalid statistics")
		}
	}
	return nil
}

// DuelingWeighting is one bench version's composite weighting: the counted
// column weight w and the datagen qualifier q, a factor on the whole
// composite. Both are float64 fields, never typed constants: a constant
// expression such as w*w is evaluated exactly at compile time, while the
// Python mirror rounds every product.
type DuelingWeighting struct {
	ColumnWeight float64
	Qualifier    float64
}

// DuelingWeightingForVersion is bench version v's weighting for a run whose
// datagen half scored datagenRaw: w = protocol.DuelingColumnWeightForVersion(v)
// and q = protocol.DuelingDatagenQualifier(v, datagenRaw), which is 1 below
// bench 19.
func DuelingWeightingForVersion(v int, datagenRaw float64) DuelingWeighting {
	return DuelingWeighting{
		ColumnWeight: protocol.DuelingColumnWeightForVersion(v),
		Qualifier:    protocol.DuelingDatagenQualifier(v, datagenRaw),
	}
}

// DuelingDetailsWeighting is the weighting a details.dueling block was scored
// with: its column_weight and its datagen_qualifier, 1 when absent (bench
// 16..18).
func DuelingDetailsWeighting(d protocol.DuelingDetails) DuelingWeighting {
	q := 1.0
	if d.DatagenQualifier != nil {
		q = *d.DatagenQualifier
	}
	return DuelingWeighting{ColumnWeight: d.ColumnWeight, Qualifier: q}
}

// mean is the column's mean case score; a column with no cases scores 0.
func (c DuelingColumnAggregate) mean() float64 {
	if c.N <= 0 {
		return 0
	}
	return c.Sum / float64(c.N)
}

// counted returns the columns not masked, sorted by column ID. A column with
// an empty owner key is never masked.
func duelingCounted(columns []DuelingColumnAggregate, mask map[string]bool) []DuelingColumnAggregate {
	out := make([]DuelingColumnAggregate, 0, len(columns))
	for _, c := range columns {
		if c.OwnerKey != "" && mask[c.OwnerKey] {
			continue
		}
		out = append(out, c)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].ColumnID < out[j].ColumnID })
	return out
}

// DuelingComposite is the dueling composite of docs/dueling-bench.md section 1:
//
//	gate × q × ((1 − w × |U|) × datagen + w × Σ_{c ∈ U} col_c)
//
// where w and q are the bench version's weighting (DuelingWeightingForVersion:
// w = 0.1 and q = 1 on bench 16..18; w = 0.15 and q = min(1, datagen / 0.8)
// from bench 19), U the columns whose owner is not in mask (an empty owner is
// never masked), and col_c = sum / n (0 when n = 0). A masked column's weight
// returns to the datagen half, so no column carries more than w. The reported
// composite masks the agent's own owner; the paired dethrone test masks both
// agents' owners. q depends on the datagen half alone, so every mask shares it.
//
// The arithmetic is float64 in a fixed order: columns in column ID order, and
// every product is converted explicitly so the compiler cannot fuse it into a
// multiply-add. The Go spec lets an implementation fuse an unconverted
// product into a later add, even across statements (arm64 does); only an
// explicit float64 conversion forces the rounding the Python mirror does.
// gate × q is exact when q = 1, so bench 16..18 composites are unchanged.
// ditto_screening_protocol.dueling reproduces every result bit for bit
// (testdata/dueling_composite_vectors.json).
func DuelingComposite(weighting DuelingWeighting, datagenRaw, datagenGate float64, columns []DuelingColumnAggregate, mask map[string]bool) float64 {
	w := weighting.ColumnWeight
	counted := duelingCounted(columns, mask)
	var generator float64
	for _, c := range counted {
		generator = generator + float64(w*c.mean())
	}
	share := 1 - float64(w*float64(len(counted)))
	inner := float64(share*datagenRaw) + generator
	return float64(float64(datagenGate*weighting.Qualifier) * inner)
}

// DuelingCompositeStderr is the composite's stratified standard error
// (docs/dueling-bench.md section 4), with columns as strata:
//
//	SE² = (1 − w × |U|)² × SE_datagen² + w² × Σ_{c ∈ U} s_c² / n_c
//
// scaled by the datagen gate and the qualifier, which the error treats as fixed
// exactly as it does the gate. s_c² is the column's sample variance
// (sum_sq − sum²/n) / (n − 1), clamped at 0; a column with n < 2 contributes
// no variance. With no column masked on bench 16..18 this is the section 4
// form 0.25 × SE_datagen² + 0.25 × SE_G², SE_G² = (1/k²) Σ s_c²/n_c over
// k = 5 columns (bench 19: 0.0625 × SE_datagen² + 0.5625 × SE_G²); a masked column's weight returns to the datagen half exactly as in
// DuelingComposite. datagenStderr is the datagen half's standard error before
// gates.
func DuelingCompositeStderr(weighting DuelingWeighting, datagenStderr, datagenGate float64, columns []DuelingColumnAggregate, mask map[string]bool) float64 {
	w := weighting.ColumnWeight
	counted := duelingCounted(columns, mask)
	var strata float64
	for _, c := range counted {
		if c.N < 2 {
			continue
		}
		n := float64(c.N)
		ss := c.SumSq - float64(c.Sum*c.Sum)/n
		variance := ss / (n - 1)
		if variance < 0 || math.IsNaN(variance) {
			variance = 0
		}
		strata = strata + variance/n
	}
	share := 1 - float64(w*float64(len(counted)))
	datagenVar := float64(float64(share*share) * float64(datagenStderr*datagenStderr))
	total := datagenVar + float64(float64(w*w)*strata)
	return float64(float64(datagenGate*weighting.Qualifier) * math.Sqrt(total))
}
