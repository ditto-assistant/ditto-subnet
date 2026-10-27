package parserprobe

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestChainV14WireParser: the generator-inverse fold of the public v14
// supersession-chain frames recovers the program cases from the wire alone.
// The chain is public, so a full inverse parser is expected to solve it; the
// SN-354 shortcuts it replaces are measured in universe. The v14 role
// families (SN-359, SN-360) have no public inverse grammar here yet, so their
// cases are left out of the count.
func TestChainV14WireParser(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV14)
	total, solved, logged := 0, 0, 0
	for seed := int64(1); seed <= 10; seed++ {
		a, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV14)
		if err != nil {
			t.Fatal(err)
		}
		stores := buildStores(a)
		roleCases := v14RoleFamilyCaseIDs(t, seed)
		for _, c := range a.MemoryCases {
			if c.QuestionType != "v13-open-program" && c.QuestionType != "v13-personal-program" {
				continue
			}
			if roleCases[c.ID] {
				continue
			}
			user := c.UserID
			if user == "" {
				user = gen.PrimaryUser
			}
			d := answerV13(stores[user], c.Question)
			total++
			if d.ok && grade.Memory(c.MemoryCase, launder(d)).Score == 1 {
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
}

// v14RoleFamilyCaseIDs returns the ids of the business cases a v14 seed renders
// in the holder-vs-actor and identity-join role families.
func v14RoleFamilyCaseIDs(t *testing.T, seed int64) map[string]bool {
	t.Helper()
	cases, err := universe.GenerateV13ProgramsForVersion(seed, 64, protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{}
	for i, c := range cases {
		switch universe.V13FamilyOfVersion(seed, i, protocol.BenchVersionV14) {
		case universe.V14FamilyHolderVsActor, universe.V14FamilyIdentityJoin:
			ids[c.Plan.Case.ID] = true
		}
	}
	return ids
}
