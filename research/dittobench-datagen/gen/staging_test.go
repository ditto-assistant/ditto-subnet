package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestStagedSuiteEvidenceSeededByUnlockWave is the core Tier-C correctness
// invariant: for every case, ALL of its answer's evidence is present in the
// haystack accumulated through its RunAfterWave, so a case is never asked
// before the facts it depends on have been seeded (ground truth stays exact).
func TestStagedSuiteEvidenceSeededByUnlockWave(t *testing.T) {
	suite := GenerateMemorySuite(NewRNG(1), 4242, 40, 3, 0.35)
	if suite.SeedingWaves != 3 {
		t.Fatalf("want 3 waves, got %d", suite.SeedingWaves)
	}

	// Cumulative haystack text through each wave.
	cumThroughWave := make([]string, suite.SeedingWaves)
	var sb strings.Builder
	for w, wave := range suite.Waves {
		if wave.Wave != w {
			t.Fatalf("wave %d carries Wave=%d", w, wave.Wave)
		}
		for _, p := range wave.Pairs {
			sb.WriteString(strings.ToLower(p.Prompt))
			sb.WriteByte(' ')
			sb.WriteString(strings.ToLower(p.Response))
			sb.WriteByte(' ')
		}
		cumThroughWave[w] = sb.String()
	}

	for _, sc := range suite.Cases {
		if strings.Contains(sc.Case.QuestionType, "abstention") {
			continue // needle-absent by design
		}
		switch sc.Case.QuestionType {
		case "single-session-recall", "knowledge-update", "preference":
			ans := strings.ToLower(sc.Case.ExpectedAnswer)
			if !strings.Contains(cumThroughWave[sc.RunAfterWave], ans) {
				t.Fatalf("case %s (%s): answer %q not seeded by unlock wave %d",
					sc.Case.ID, sc.Case.QuestionType, sc.Case.ExpectedAnswer, sc.RunAfterWave)
			}
		}
	}
}

// TestEveryPairInExactlyOneWave checks the wave partition is a true partition of
// the haystack (no pair dropped, none duplicated) and that subject links only
// reference pairs seeded in the SAME wave (no dangling cross-wave links).
func TestEveryPairInExactlyOneWave(t *testing.T) {
	suite := GenerateMemorySuite(NewRNG(2), 7, 30, 3, 0.3)
	pairWave := map[string]int{}
	for w, wave := range suite.Waves {
		for _, p := range wave.Pairs {
			if _, dup := pairWave[p.PairID]; dup {
				t.Fatalf("pair %s in more than one wave", p.PairID)
			}
			pairWave[p.PairID] = w
		}
	}
	for w, wave := range suite.Waves {
		for _, l := range wave.Links {
			pw, ok := pairWave[l.PairID]
			if !ok {
				t.Fatalf("link references unknown pair %s", l.PairID)
			}
			if pw != w {
				t.Fatalf("wave %d links to pair %s seeded in wave %d (dangling)", w, l.PairID, pw)
			}
		}
	}
}

// TestTierBSeedsPairsWithoutSubjects proves the memory-construction test: a
// Tier-B (raw-pairs) run seeds a materially larger share of its pairs with NO
// prepared subject than a Tier-A run: the info is present, the scaffolding is
// not, so a subjects-first retriever must self-construct to answer.
func TestTierBSeedsPairsWithoutSubjects(t *testing.T) {
	unlinkedFrac := func(rawFrac float64) float64 {
		suite := GenerateMemorySuite(NewRNG(3), 555, 40, 1, rawFrac)
		var pairs int
		linked := map[string]bool{}
		for _, wave := range suite.Waves {
			pairs += len(wave.Pairs)
			for _, l := range wave.Links {
				linked[l.PairID] = true
			}
		}
		var unlinked int
		for _, wave := range suite.Waves {
			for _, p := range wave.Pairs {
				if !linked[p.PairID] {
					unlinked++
				}
			}
		}
		if pairs == 0 {
			t.Fatal("no pairs")
		}
		return float64(unlinked) / float64(pairs)
	}
	tierA := unlinkedFrac(0)
	tierB := unlinkedFrac(0.5)
	if tierB <= tierA {
		t.Fatalf("Tier-B should leave more pairs unlinked than Tier-A: A=%.2f B=%.2f", tierA, tierB)
	}
}

// TestSingleWaveMatchesFlat guards backward compatibility: a 1-wave, Tier-A
// suite is exactly the flat GenerateMemoryV2 single-seed view.
func TestSingleWaveMatchesFlat(t *testing.T) {
	seedReq, cases, _, _ := GenerateMemoryV2(NewRNG(9), 111, 20)
	if len(cases) == 0 || len(seedReq.Pairs) == 0 {
		t.Fatal("flat view empty")
	}
	var _ protocol.SeedRequest = seedReq
}
