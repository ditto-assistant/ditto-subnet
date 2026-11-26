package challenge

import (
	"encoding/json"
	"math"
	"os"
	"sort"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// DuelingCompositeVectors pins DuelingComposite and DuelingCompositeStderr.
// ditto_screening_protocol.dueling must reproduce every expected value bit for
// bit (packages/ditto-screening-protocol/tests/test_dueling.py).
type DuelingCompositeVectors struct {
	Composites []DuelingCompositeVector `json:"composites"`
}

type DuelingCompositeVector struct {
	Name string `json:"name"`
	// BenchVersion is omitted (0) on the bench 16..18 vectors, which predate
	// it and score with DuelingWeightingForVersion(16, ...).
	BenchVersion  int                      `json:"bench_version,omitempty"`
	DatagenRaw    float64                  `json:"datagen_raw"`
	DatagenGate   float64                  `json:"datagen_gate"`
	DatagenStderr float64                  `json:"datagen_stderr"`
	Columns       []DuelingColumnAggregate `json:"columns"`
	// Mask lists the masked owner keys, sorted.
	Mask []string `json:"mask"`
	// ColumnWeight and DatagenQualifier are the weighting, recorded on the
	// bench >= 19 vectors only so every older vector stays byte-identical.
	ColumnWeight     float64  `json:"column_weight,omitempty"`
	DatagenQualifier *float64 `json:"datagen_qualifier,omitempty"`
	Composite        float64  `json:"composite"`
	CompositeStderr  float64  `json:"composite_stderr"`
}

// weighting is the vector's weighting; a vector without a bench version is
// a bench 16 vector.
func (v DuelingCompositeVector) weighting() DuelingWeighting {
	version := v.BenchVersion
	if version == 0 {
		version = protocol.DuelingMinBenchVersion
	}
	return DuelingWeightingForVersion(version, v.DatagenRaw)
}

func maskSet(keys []string) map[string]bool {
	m := map[string]bool{}
	for _, k := range keys {
		m[k] = true
	}
	return m
}

func agg(id, owner string, scores ...float64) DuelingColumnAggregate {
	a := DuelingColumnAggregate{ColumnID: id, OwnerKey: owner, N: len(scores)}
	for _, s := range scores {
		a.Sum += s
		a.SumSq += float64(s * s) // no fused multiply-add: the inputs are vectors
	}
	return a
}

// duelingSlice is a slice of five columns: three miner owners (o1 holds two
// columns only in the pair tests), and the public default on c4.
func duelingSlice() []DuelingColumnAggregate {
	return []DuelingColumnAggregate{
		// Given out of column order: the composite sorts.
		agg("c3", "coldkey:o3", 0.3, 0.6, 0.9, 1, 0, 0.45, 0.7),
		agg("c0", "coldkey:o1", 1, 0.5, 0.25, 1, 1, 0.8, 0.9, 0.1),
		agg("c1", "coldkey:o2", 0.2, 0.4, 0.6, 0.8, 1, 0),
		agg("c2", "coldkey:o2", 0.95, 0.85, 0.75),
		agg("c4", "", 0.6, 0.7, 0.8, 0.9, 0.55, 0.65),
	}
}

func buildDuelingCompositeVectors() DuelingCompositeVectors {
	type in struct {
		name   string
		bench  int
		raw    float64
		gate   float64
		se     float64
		cols   []DuelingColumnAggregate
		masked []string
	}
	substituted := duelingSlice()
	substituted[2] = agg("c1", "", 0.5, 0.5, 0.75, 0.25)
	substituted[2].Substituted = true
	empty := duelingSlice()
	empty[0] = DuelingColumnAggregate{ColumnID: "c3", OwnerKey: "coldkey:o3"}
	single := duelingSlice()
	single[3] = agg("c2", "coldkey:o2", 0.7)
	inputs := []in{
		{"unmasked", 0, 0.8123456789, 1, 0.031, duelingSlice(), nil},
		{"self-mask", 0, 0.8123456789, 1, 0.031, duelingSlice(), []string{"coldkey:o1"}},
		{"self-mask owner with two columns", 0, 0.77, 0.95, 0.02, duelingSlice(), []string{"coldkey:o2"}},
		{"pair-mask", 0, 0.6666666666666666, 0.9, 0.0275, duelingSlice(), []string{"coldkey:o1", "coldkey:o3"}},
		{"pair-mask same owner", 0, 0.7, 1, 0.01, duelingSlice(), []string{"coldkey:o2", "coldkey:o2"}},
		{"empty owner never masked", 0, 0.5, 1, 0.04, duelingSlice(), []string{"", "coldkey:o3"}},
		{"substituted column", 0, 0.9, 1, 0.015, substituted, []string{"coldkey:o2"}},
		{"n zero scores zero", 0, 0.85, 1, 0.02, empty, nil},
		{"n one has no variance", 0, 0.85, 1, 0.02, single, []string{"coldkey:o1"}},
		{"everything masked but the default", 0, 0.4, 1, 0.05, duelingSlice(), []string{"coldkey:o1", "coldkey:o2", "coldkey:o3"}},
		{"unknown mask key", 0, 0.3, 0.5, 0.05, duelingSlice(), []string{"coldkey:nobody"}},
		{"gate zero", 0, 0.95, 0, 0.02, duelingSlice(), []string{"coldkey:o1"}},
		{"no columns", 0, 0.42, 0.8, 0.03, nil, nil},
		{"perfect", 0, 1, 1, 0, []DuelingColumnAggregate{
			agg("c0", "coldkey:o1", 1, 1), agg("c1", "coldkey:o2", 1, 1), agg("c2", "coldkey:o3", 1, 1),
			agg("c3", "coldkey:o4", 1, 1), agg("c4", "", 1, 1),
		}, []string{"coldkey:o4"}},
		// Bench 18 weights exactly as the unnamed bench 16 vectors do.
		{"v18 weights as v16", protocol.BenchVersionV18, 0.8123456789, 1, 0.031, duelingSlice(), []string{"coldkey:o1"}},
		// Bench 19: w = 0.15 and the datagen qualifier.
		{"v19 unmasked", protocol.BenchVersionV19, 0.9123456789, 1, 0.031, duelingSlice(), nil},
		{"v19 self-mask", protocol.BenchVersionV19, 0.8123456789, 1, 0.031, duelingSlice(), []string{"coldkey:o1"}},
		{"v19 self-mask owner with two columns", protocol.BenchVersionV19, 0.86, 0.95, 0.02, duelingSlice(), []string{"coldkey:o2"}},
		{"v19 datagen at the qualifier floor", protocol.BenchVersionV19, 0.8, 1, 0.02, duelingSlice(), []string{"coldkey:o3"}},
		{"v19 qualifier below one", protocol.BenchVersionV19, 0.6, 0.95, 0.025, duelingSlice(), []string{"coldkey:o1"}},
		{"v19 pair-mask", protocol.BenchVersionV19, 0.6666666666666666, 0.9, 0.0275, duelingSlice(), []string{"coldkey:o1", "coldkey:o3"}},
		{"v19 pair-mask above the floor", protocol.BenchVersionV19, 0.9, 1, 0.012, duelingSlice(), []string{"coldkey:o1", "coldkey:o2"}},
		{"v19 everything masked but the default", protocol.BenchVersionV19, 0.4, 1, 0.05, duelingSlice(), []string{"coldkey:o1", "coldkey:o2", "coldkey:o3"}},
		{"v19 substituted column", protocol.BenchVersionV19, 0.9, 1, 0.015, substituted, []string{"coldkey:o2"}},
		{"v19 datagen zero", protocol.BenchVersionV19, 0, 1, 0, duelingSlice(), nil},
		{"v19 gate zero", protocol.BenchVersionV19, 0.95, 0, 0.02, duelingSlice(), []string{"coldkey:o1"}},
		{"v19 perfect", protocol.BenchVersionV19, 1, 1, 0, []DuelingColumnAggregate{
			agg("c0", "coldkey:o1", 1, 1), agg("c1", "coldkey:o2", 1, 1), agg("c2", "coldkey:o3", 1, 1),
			agg("c3", "coldkey:o4", 1, 1), agg("c4", "", 1, 1),
		}, []string{"coldkey:o4"}},
	}
	var out DuelingCompositeVectors
	for _, v := range inputs {
		mask := append([]string{}, v.masked...)
		sort.Strings(mask)
		vec := DuelingCompositeVector{
			Name: v.name, BenchVersion: v.bench, DatagenRaw: v.raw, DatagenGate: v.gate, DatagenStderr: v.se,
			Columns: v.cols, Mask: mask,
		}
		weighting := vec.weighting()
		if protocol.DuelingHeldOutBenchVersion(v.bench) {
			q := weighting.Qualifier
			vec.ColumnWeight, vec.DatagenQualifier = weighting.ColumnWeight, &q
		}
		vec.Composite = DuelingComposite(weighting, v.raw, v.gate, v.cols, maskSet(v.masked))
		vec.CompositeStderr = DuelingCompositeStderr(weighting, v.se, v.gate, v.cols, maskSet(v.masked))
		out.Composites = append(out.Composites, vec)
	}
	return out
}

func TestDuelingCompositeVectorsGolden(t *testing.T) {
	got := buildDuelingCompositeVectors()
	path := "testdata/dueling_composite_vectors.json"
	if *update {
		raw, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./challenge -run TestDuelingCompositeVectorsGolden -update)", err)
	}
	var want DuelingCompositeVectors
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	if len(want.Composites) != len(got.Composites) {
		t.Fatal("composite vectors drifted; re-run with -update and review the diff")
	}
	for _, w := range want.Composites {
		// Recompute from the file's inputs, bit for bit.
		weighting := w.weighting()
		c := DuelingComposite(weighting, w.DatagenRaw, w.DatagenGate, w.Columns, maskSet(w.Mask))
		se := DuelingCompositeStderr(weighting, w.DatagenStderr, w.DatagenGate, w.Columns, maskSet(w.Mask))
		if math.Float64bits(c) != math.Float64bits(w.Composite) || math.Float64bits(se) != math.Float64bits(w.CompositeStderr) {
			t.Errorf("vector %q: got (%v, %v), file has (%v, %v)", w.Name, c, se, w.Composite, w.CompositeStderr)
		}
	}
	byName := map[string]DuelingCompositeVector{}
	for _, w := range want.Composites {
		byName[w.Name] = w
	}
	if v16, v18 := byName["self-mask"], byName["v18 weights as v16"]; v16.Composite != v18.Composite || v16.CompositeStderr != v18.CompositeStderr {
		t.Fatal("bench 18 must weight exactly as bench 16")
	}
	gotRaw, _ := json.Marshal(got)
	wantRaw, _ := json.Marshal(want)
	if string(gotRaw) != string(wantRaw) {
		t.Fatal("composite vectors drifted; re-run with -update and review the diff")
	}
}

func TestDuelingCompositeFormula(t *testing.T) {
	v16 := DuelingWeightingForVersion(protocol.DuelingMinBenchVersion, 0.8)
	cols := duelingSlice()
	means := map[string]float64{}
	for _, c := range cols {
		means[c.ColumnID] = c.Sum / float64(c.N)
	}
	near := func(a, b float64) bool { return math.Abs(a-b) < 1e-12 }

	// Unmasked: 0.5 × datagen + 0.1 × every column.
	all := 0.5 * 0.8
	for _, m := range means {
		all += 0.1 * m
	}
	if got := DuelingComposite(v16, 0.8, 1, cols, nil); !near(got, all) {
		t.Fatalf("unmasked composite %v, want %v", got, all)
	}
	// Self-mask o2 (c1, c2): their 0.2 returns to datagen.
	self := 0.7*0.8 + 0.1*(means["c0"]+means["c3"]+means["c4"])
	if got := DuelingComposite(v16, 0.8, 1, cols, maskSet([]string{"coldkey:o2"})); !near(got, self) {
		t.Fatalf("self-masked composite %v, want %v", got, self)
	}
	// The gate scales everything; gate 0 is 0.
	if got := DuelingComposite(v16, 0.8, 0.5, cols, nil); !near(got, all/2) {
		t.Fatalf("gated composite %v, want %v", got, all/2)
	}
	if DuelingComposite(v16, 0.8, 0, cols, nil) != 0 {
		t.Fatal("gate 0 must zero the composite")
	}
	// No column carries more than the column weight: a perfect column moves
	// a zero-datagen composite by exactly w.
	one := []DuelingColumnAggregate{agg("c0", "coldkey:o1", 1, 1, 1)}
	if got := DuelingComposite(v16, 0, 1, one, nil); !near(got, 0.1) {
		t.Fatalf("one perfect column is worth %v, want 0.1", got)
	}
	// The default column's empty owner key is never masked, even when "" is
	// in the mask.
	if DuelingComposite(v16, 0.8, 1, cols, maskSet([]string{""})) != DuelingComposite(v16, 0.8, 1, cols, nil) {
		t.Fatal("an empty owner key must never be masked")
	}
	// Input order does not matter.
	rev := append([]DuelingColumnAggregate(nil), cols...)
	sort.Slice(rev, func(i, j int) bool { return rev[i].ColumnID > rev[j].ColumnID })
	if DuelingComposite(v16, 0.8, 1, rev, nil) != DuelingComposite(v16, 0.8, 1, cols, nil) {
		t.Fatal("columns must be summed in column id order")
	}
}

func TestDuelingCompositeStderrMatchesSectionFourUnmasked(t *testing.T) {
	v16 := DuelingWeightingForVersion(protocol.DuelingMinBenchVersion, 0.8)
	cols := duelingSlice()
	var g2 float64
	for _, c := range cols {
		n := float64(c.N)
		s2 := (c.SumSq - c.Sum*c.Sum/n) / (n - 1)
		g2 += s2 / n
	}
	g2 /= 25 // (1/k²) with k = 5
	want := 0.9 * math.Sqrt(0.25*0.03*0.03+0.25*g2)
	if got := DuelingCompositeStderr(v16, 0.03, 0.9, cols, nil); math.Abs(got-want) > 1e-12 {
		t.Fatalf("stratified SE %v, want %v", got, want)
	}
}

func TestDuelingWeightingForVersion(t *testing.T) {
	for _, v := range []int{protocol.DuelingMinBenchVersion, 17, protocol.BenchVersionV18} {
		// Below bench 19 the qualifier is 1 at any datagen score.
		for _, raw := range []float64{0, 0.3, 0.8, 1} {
			if got := DuelingWeightingForVersion(v, raw); got != (DuelingWeighting{ColumnWeight: 0.1, Qualifier: 1}) {
				t.Fatalf("bench %d weighting %+v, want w 0.1 and q 1", v, got)
			}
		}
	}
	for raw, q := range map[float64]float64{0: 0, 0.4: 0.5, 0.6: 0.75, 0.8: 1, 0.95: 1, 1: 1} {
		for _, v := range []int{protocol.BenchVersionV19, protocol.BenchVersionV19 + 3} {
			got := DuelingWeightingForVersion(v, raw)
			if got.ColumnWeight != 0.15 || math.Abs(got.Qualifier-q) > 1e-15 {
				t.Fatalf("bench %d at datagen %v: weighting %+v, want w 0.15 and q %v", v, raw, got, q)
			}
		}
	}
}

func TestDuelingCompositeV19Formula(t *testing.T) {
	cols := duelingSlice()
	means := map[string]float64{}
	for _, c := range cols {
		means[c.ColumnID] = c.Sum / float64(c.N)
	}
	near := func(a, b float64) bool { return math.Abs(a-b) < 1e-12 }
	at := func(raw float64) DuelingWeighting { return DuelingWeightingForVersion(protocol.BenchVersionV19, raw) }

	// Above the floor: 0.25 × datagen + 0.15 × every column.
	all := 0.25 * 0.9
	for _, m := range means {
		all += 0.15 * m
	}
	if got := DuelingComposite(at(0.9), 0.9, 1, cols, nil); !near(got, all) {
		t.Fatalf("unmasked v19 composite %v, want %v", got, all)
	}
	// Self-mask o2 (c1, c2): their 0.3 returns to datagen.
	self := 0.55*0.9 + 0.15*(means["c0"]+means["c3"]+means["c4"])
	if got := DuelingComposite(at(0.9), 0.9, 1, cols, maskSet([]string{"coldkey:o2"})); !near(got, self) {
		t.Fatalf("self-masked v19 composite %v, want %v", got, self)
	}
	// Paired dethrone mask (o1 and o3): their 0.3 returns to datagen too.
	pair := 0.55*0.9 + 0.15*(means["c1"]+means["c2"]+means["c4"])
	if got := DuelingComposite(at(0.9), 0.9, 1, cols, maskSet([]string{"coldkey:o1", "coldkey:o3"})); !near(got, pair) {
		t.Fatalf("pair-masked v19 composite %v, want %v", got, pair)
	}
	// Below the floor the qualifier scales the whole composite, columns too.
	columnsShare := all - 0.25*0.9
	low := 0.75 * (0.25*0.6 + columnsShare)
	if got := DuelingComposite(at(0.6), 0.6, 1, cols, nil); !near(got, low) {
		t.Fatalf("qualified v19 composite %v, want %v", got, low)
	}
	// A perfect column moves a datagen-at-floor composite by exactly 0.15.
	one := []DuelingColumnAggregate{agg("c0", "coldkey:o1", 1, 1, 1)}
	zero := []DuelingColumnAggregate{agg("c0", "coldkey:o1", 0, 0, 0)}
	if got := DuelingComposite(at(0.8), 0.8, 1, one, nil) - DuelingComposite(at(0.8), 0.8, 1, zero, nil); !near(got, 0.15) {
		t.Fatalf("one perfect column is worth %v, want 0.15", got)
	}
	// The stderr scales by the qualifier exactly as by the gate.
	if a, b := DuelingCompositeStderr(at(0.6), 0.03, 1, cols, nil), DuelingCompositeStderr(at(0.9), 0.03, 0.75, cols, nil); !near(a, b) {
		t.Fatalf("qualified stderr %v, want gate-equivalent %v", a, b)
	}
}

func TestCheckDuelingColumns(t *testing.T) {
	if err := CheckDuelingColumns(duelingSlice()); err != nil {
		t.Fatal(err)
	}
	long := append(duelingSlice(), agg("c5", "coldkey:o5", 1))
	if CheckDuelingColumns(long) == nil {
		t.Fatal("an overlong report must be refused")
	}
	if CheckDuelingColumns(duelingSlice()[:4]) == nil {
		t.Fatal("a short report must be refused")
	}
	dup := duelingSlice()
	dup[0].ColumnID = "c0"
	if CheckDuelingColumns(dup) == nil {
		t.Fatal("duplicate column ids must be refused")
	}
	bad := duelingSlice()
	bad[1].Sum = math.NaN()
	if CheckDuelingColumns(bad) == nil {
		t.Fatal("non-finite statistics must be refused")
	}
	owned := duelingSlice()
	owned[0].Substituted = true
	if CheckDuelingColumns(owned) == nil {
		t.Fatal("a substituted column that names an owner must be refused")
	}
	phantom := duelingSlice()
	phantom[3].N = 0
	if CheckDuelingColumns(phantom) == nil {
		t.Fatal("a column with no cases but nonzero sums must be refused")
	}
	neg := duelingSlice()
	neg[2].N = -1
	if CheckDuelingColumns(neg) == nil {
		t.Fatal("a negative count must be refused")
	}
}
