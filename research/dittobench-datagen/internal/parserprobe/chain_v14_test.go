package parserprobe

import (
	"sort"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestChainV14WireParser: the generator-inverse fold of the public v14
// program frames recovers the program cases from the wire alone, including
// the supersession chains (SN-353/354), the temporal (SN-355/358), process and
// membership (SN-356/357) and role (SN-359/360) families. Every v14 business
// family is public, so a full inverse parser is expected to solve each of
// them; the SN-354 shortcuts the chains replace are measured in universe.
func TestChainV14WireParser(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV14)
	type tally struct{ total, solved int }
	total, solved, logged := 0, 0, 0
	byFamily := map[universe.V13Family]*tally{}
	for seed := int64(1); seed <= 10; seed++ {
		a, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		stores := buildStores(a)
		families := v14BusinessFamilyByCaseID(t, seed)
		for _, c := range a.MemoryCases {
			if c.QuestionType != "v13-open-program" && c.QuestionType != "v13-personal-program" {
				continue
			}
			user := c.UserID
			if user == "" {
				user = gen.PrimaryUser
			}
			d := answerV13(stores[user], c.Question)
			ok := d.ok && grade.Memory(c.MemoryCase, launder(d)).Score == 1
			total++
			if fam, isBusiness := families[c.ID]; isBusiness {
				if byFamily[fam] == nil {
					byFamily[fam] = &tally{}
				}
				byFamily[fam].total++
				if ok {
					byFamily[fam].solved++
				}
			}
			if ok {
				solved++
			} else if logged < 6 {
				logged++
				t.Logf("%q -> %+v expected %s", c.Question, d, c.ExpectedAnswer)
				for _, row := range scopedV14Rows(stores[user], c.Question) {
					kind, value, ok := parseChainStep(row)
					t.Logf("   row %q -> %s %q %v", row, kind, value, ok)
				}
			}
		}
	}
	t.Logf("v14 program cases solved %d/%d", solved, total)
	if float64(solved)/float64(total) < .95 {
		t.Fatalf("wire-only parser recovered %d/%d; want >=95%%", solved, total)
	}
	names := make([]string, 0, len(byFamily))
	for fam := range byFamily {
		names = append(names, string(fam))
	}
	sort.Strings(names)
	for _, name := range names {
		f := byFamily[universe.V13Family(name)]
		t.Logf("  %-28s %d/%d", name, f.solved, f.total)
		if float64(f.solved)/float64(f.total) < .95 {
			t.Errorf("family %s: wire-only parser recovered %d/%d; want >=95%%", name, f.solved, f.total)
		}
	}
	for _, fam := range []universe.V13Family{universe.V14FamilyHolderVsActor, universe.V14FamilyIdentityJoin} {
		if byFamily[fam] == nil || byFamily[fam].total == 0 {
			t.Errorf("family %s never sampled; the probe no longer covers it", fam)
		}
	}
}

// v14BusinessFamilyByCaseID returns the provenance family of each business
// case a v14 seed renders. Test-side only: the parser never reads it.
func v14BusinessFamilyByCaseID(t *testing.T, seed int64) map[string]universe.V13Family {
	t.Helper()
	cases, err := universe.GenerateV13ProgramsForVersion(seed, 64, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]universe.V13Family{}
	for i, c := range cases {
		ids[c.Plan.Case.ID] = universe.V13FamilyOfVersion(seed, i, protocol.BenchVersionV14)
	}
	return ids
}
