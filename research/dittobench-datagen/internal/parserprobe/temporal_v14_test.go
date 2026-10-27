package parserprobe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestTemporalV14GrammarInvertsEveryShape keeps the public inverse grammar in
// step with the v14 temporal families (SN-355, SN-358): every as-of and
// event-sequence query kind must be parsed from the public prose. A family the
// probe cannot read would score as a silent zero that looks like hardening.
func TestTemporalV14GrammarInvertsEveryShape(t *testing.T) {
	seen := map[string]int{}
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV14)
	for seed := int64(1); seed <= 30; seed++ {
		a, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		stores := buildStores(a)
		for _, c := range a.MemoryCases {
			if c.V10Provenance == nil {
				continue
			}
			switch op := c.V10Provenance.Program.Op; op {
			case "as_of", "nth_by_date", "before_by_date", "after_by_date":
				user := c.UserID
				if user == "" {
					user = gen.PrimaryUser
				}
				d := answerBusinessV13(stores[user], c.Question)
				if !d.ok || d.value != c.ExpectedAnswer {
					t.Fatalf("seed %d %s: parsed %q, want %q (%s)", seed, op, d.value, c.ExpectedAnswer, c.Question)
				}
				seen[op]++
			}
		}
	}
	for _, op := range []string{"as_of", "nth_by_date", "before_by_date", "after_by_date"} {
		if seen[op] == 0 {
			t.Fatalf("no %s case over 30 seeds: %v", op, seen)
		}
	}
}
