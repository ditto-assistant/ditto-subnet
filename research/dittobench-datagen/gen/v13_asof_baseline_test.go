package gen

import (
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

// A latest-state-only index cannot answer both sides of an as-of pair, even
// before the optional twin post-pass. Exercise the actual assembled artifact.
func TestV13PointInTimeTwinsDefeatAStaticStateIndex(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	for seed := int64(1); seed <= 10; seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatal(err)
		}
		groups := map[string][]protocol.MemoryCase{}
		for _, c := range a.MemoryCases {
			if c.TwinRelation == protocol.TwinRelationAsOf {
				groups[c.TwinPairID] = append(groups[c.TwinPairID], c.MemoryCase)
			}
		}
		if len(groups) != 6 {
			t.Fatalf("seed %d: got %d as-of groups", seed, len(groups))
		}
		for id, pair := range groups {
			if id == "" || len(pair) != 2 {
				t.Fatalf("bad as-of group %q: %d", id, len(pair))
			}
			for i, c := range pair {
				answer := renderV13Answer(c)
				response := protocol.RunResponse{Answer: answer, FinalText: answer}
				if v := grade.Memory(c, response); v.Score != 1 {
					t.Fatalf("seed %d oracle %s: %+v", seed, c.ID, v)
				}
				if v := grade.Memory(pair[1-i], response); v.Score != 0 {
					t.Fatalf("seed %d static index answered both halves of %s: %+v", seed, id, v)
				}
			}
		}
	}
}
