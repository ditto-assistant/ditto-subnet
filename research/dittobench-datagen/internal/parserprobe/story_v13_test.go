package parserprobe

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/grade"
)

// Public-wire recovery is measured independently of metadata classification:
// registering a family alone must not masquerade as implementing its parser.
//
// Three seeds give each story family ~39 cases: a family the parser cannot
// read drops to ~0%, and a broken frame variant well below the 90% floor.
func TestStoryV13WireParser(t *testing.T) {
	t.Parallel()
	counts, pass := map[string]int{}, map[string]int{}
	for _, a := range probeArtifacts(t, 13, seedRange(11, 13)...) {
		seed := a.Seed
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
