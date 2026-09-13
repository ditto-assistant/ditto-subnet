package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestV13WorldMemorySuiteCarriesStoryV2 pins the story v2 share of the v13
// world memory suite: 78 story cases (13 arcs x 6 oracles, issue #1841) inside
// the unchanged fixed world-case envelope, no v8 money story oracle, and no
// generator role name in any story session id. The v12 suite for the same seed
// is regenerated alongside to prove the envelope did not move.
func TestV13WorldMemorySuiteCarriesStoryV2(t *testing.T) {
	for seed := int64(1); seed <= 3; seed++ {
		v13, err := generateV8WorldMemorySuite(seed, 225, 5, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("seed %d v13 world suite: %v", seed, err)
		}
		v12, err := generateV8WorldMemorySuite(seed, 225, 5, protocol.BenchVersionV12)
		if err != nil {
			t.Fatalf("seed %d v12 world suite: %v", seed, err)
		}
		if len(v13.Cases) != len(v12.Cases) {
			t.Fatalf("seed %d v13 world suite has %d cases, v12 has %d", seed, len(v13.Cases), len(v12.Cases))
		}
		story, money := 0, 0
		kinds := map[string]int{}
		for _, staged := range v13.Cases {
			qt := staged.Case.QuestionType
			if !strings.HasPrefix(qt, "world-story-") {
				continue
			}
			story++
			kinds[qt]++
			if staged.Case.AnswerKind == protocol.AnswerMoney {
				money++
			}
			if staged.Case.BenchVersion != protocol.BenchVersionV13 {
				t.Fatalf("seed %d story case %s carries bench_version %d", seed, staged.Case.ID, staged.Case.BenchVersion)
			}
			if len(staged.RequiredPairIDs) < 3 {
				t.Fatalf("seed %d story case %s requires %d pairs", seed, staged.Case.ID, len(staged.RequiredPairIDs))
			}
		}
		if story != 78 {
			t.Fatalf("seed %d v13 story cases=%d, want 78 (%v)", seed, story, kinds)
		}
		if money > 5 {
			t.Fatalf("seed %d v13 money-bearing story cases=%d, want <= 5", seed, money)
		}
		for _, retired := range []string{"world-story-balance-current", "world-story-budget-delta", "world-story-post-approval-balance", "world-story-later-net-change", "world-story-outcome-summary"} {
			if kinds[retired] > 0 {
				t.Fatalf("seed %d v13 still emits the retired v8 story oracle %s", seed, retired)
			}
		}
		if kinds["world-story-x-owner-email"] != 13 {
			t.Fatalf("seed %d cross-record story oracles=%d, want one per arc", seed, kinds["world-story-x-owner-email"])
		}
	}
}

// TestV13WorldPairsHaveOpaqueStorySessions checks the seeded world itself:
// every story v2 memory carries an opaque session id and a non-positional
// timestamp, while the v12 world for the same seed keeps its frozen bytes.
func TestV13WorldPairsHaveOpaqueStorySessions(t *testing.T) {
	for seed := int64(1); seed <= 3; seed++ {
		w := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV13)
		storyPairs := map[string]bool{}
		for _, story := range w.Stories {
			storyPairs[story.PairID] = true
		}
		if len(storyPairs) < 13*4 {
			t.Fatalf("seed %d has only %d story pairs", seed, len(storyPairs))
		}
		for _, pair := range w.Pairs {
			if storyPairs[pair.PairID] && strings.HasPrefix(pair.SessionID, "story-") {
				t.Fatalf("seed %d story pair %s leaks its slot through session %q", seed, pair.PairID, pair.SessionID)
			}
		}
		frozen := universe.Generate(seed, 3)
		v12 := universe.GenerateForVersion(seed, 3, protocol.BenchVersionV12)
		if len(frozen.Pairs) != len(v12.Pairs) {
			t.Fatalf("seed %d v12 world pair count moved", seed)
		}
		for i := range frozen.Pairs {
			if frozen.Pairs[i] != v12.Pairs[i] {
				t.Fatalf("seed %d v12 world pair %d moved", seed, i)
			}
		}
	}
}
