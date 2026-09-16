package parserprobe

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestBusinessV13WireParser(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV13)
	total, solved := 0, 0
	counts, successes := map[string]int{}, map[string]int{}
	for seed := int64(1); seed <= 10; seed++ {
		a, err := gen.GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		stores := buildStores(a)
		for _, c := range a.MemoryCases {
			if c.QuestionType != "v13-open-program" && c.QuestionType != "v13-personal-program" && !strings.HasPrefix(c.QuestionType, "record-quantity-") && !strings.HasPrefix(c.QuestionType, "point-in-time-") {
				continue
			}
			user := c.UserID
			if user == "" {
				user = gen.PrimaryUser
			}
			d := answerV13(stores[user], c.Question)
			total++
			counts[c.QuestionType]++
			if d.ok && grade.Memory(c.MemoryCase, launder(d)).Score == 1 {
				solved++
				successes[c.QuestionType]++
			} else if counts[c.QuestionType] < 2 {
				t.Logf("%s %q -> %+v expected %s", c.QuestionType, c.Question, d, c.ExpectedAnswer)
				if strings.HasPrefix(c.QuestionType, "point-in-time") {
					for _, p := range stores[user].people {
						if strings.Contains(c.Question, p.name) {
							t.Logf("person %+v", p)
						}
					}
					for _, p := range stores[user].projects {
						if p.alias != "" && strings.Contains(c.Question, p.alias) {
							t.Logf("project %+v", p)
						}
					}
				}
			}
		}
	}
	t.Logf("counts %v solved %v", counts, successes)
	if float64(solved)/float64(total) < .95 {
		t.Fatalf("wire-only parser recovered %d/%d; want >=95%%", solved, total)
	}
}
