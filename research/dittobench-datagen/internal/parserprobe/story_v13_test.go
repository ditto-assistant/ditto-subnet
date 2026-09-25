package parserprobe

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
)

// Public-wire recovery is measured independently of metadata classification:
// registering a family alone must not masquerade as implementing its parser.
func TestStoryV13WireParser(t *testing.T) {
	p, _ := gen.ProfileForVersion("full", 13)
	counts, pass := map[string]int{}, map[string]int{}
	for seed := int64(11); seed < 21; seed++ {
		a, err := gen.GenerateDataset(seed, p, 13)
		if err != nil {
			t.Fatal(err)
		}
		st := buildStores(a)[gen.PrimaryUser]
		for _, c := range a.MemoryCases {
			if !strings.HasPrefix(c.QuestionType, "world-story-") {
				continue
			}
			counts[c.QuestionType]++
			d := answerStoryV13(st, c.Question)
			if d.ok && grade.Memory(c.MemoryCase, launder(d)).Score == 1 {
				pass[c.QuestionType]++
			} else {
				t.Logf("seed %d %s got %+v want %s", seed, c.QuestionType, d, c.ExpectedAnswer)
			}
		}
	}
	t.Logf("counts %v pass %v", counts, pass)
	for family, n := range counts {
		if float64(pass[family])/float64(n) < .9 {
			t.Errorf("%s recovered %d/%d, want >=90%%", family, pass[family], n)
		}
	}
}
