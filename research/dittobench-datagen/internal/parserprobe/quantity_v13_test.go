package parserprobe

import (
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"strings"
	"testing"
)

func TestQuantityV13DeliveredWaveControl(t *testing.T) {
	profile, _ := gen.ProfileForVersion("full", 13)
	total, passed := 0, 0
	for seed := int64(1); seed <= 10; seed++ {
		a, err := gen.GenerateDataset(seed, profile, 13)
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range a.MemoryCases {
			if !strings.HasPrefix(c.QuestionType, "record-quantity-") {
				continue
			}
			user := c.UserID
			if user == "" {
				user = gen.PrimaryUser
			}
			st := buildStoresThroughWave(a, c.RunAfterWave)[user]
			if st == nil {
				st = newStore(13)
			}
			d := answerV13(st, c.Question)
			total++
			if d.ok && grade.Memory(c.MemoryCase, launder(d)).Score == 1 {
				passed++
				continue
			}
			t.Logf("seed %d wave %d %s question %q got %+v expected %s", seed, c.RunAfterWave, c.QuestionType, c.Question, d, c.ExpectedAnswer)
		}
	}
	if float64(passed)/float64(total) < .90 {
		t.Fatalf("delivered quantity control %d/%d below 90%%", passed, total)
	}
}
