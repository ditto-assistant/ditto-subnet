package parserprobe

import (
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"testing"
)

func TestStoresRespectRunWaveAndUser(t *testing.T) {
	a := gen.DatasetArtifact{BenchVersion: 13, MemoryWaves: []protocol.SeedRequest{
		{Wave: 0, Pairs: []protocol.MemoryPair{{PairID: "first", Prompt: "First record."}}},
		{Wave: 1, Pairs: []protocol.MemoryPair{{PairID: "future", Prompt: "Future record."}}},
		{Wave: 0, UserID: "other", Pairs: []protocol.MemoryPair{{PairID: "private", Prompt: "Other user's record."}}},
	}}
	for wave, want := range []int{1, 2} {
		s := buildStoresThroughWave(a, wave)
		if got := len(s[gen.PrimaryUser].pairs); got != want {
			t.Fatalf("wave %d saw %d records, want %d", wave, got, want)
		}
		if got := len(s["other"].pairs); got != 1 {
			t.Fatalf("other graph saw %d records", got)
		}
	}
}
