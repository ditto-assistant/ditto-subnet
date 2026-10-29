package parserprobe

import (
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"strings"
	"testing"
)

func TestQuantityV13DeliveredWaveControl(t *testing.T) {
	t.Parallel()
	total, passed := 0, 0
	for _, a := range probeArtifacts(t, 13, seedRange(1, 3)...) {
		seed := a.Seed
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
