package gen

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// TestV13IsolationQuotaMatchesTheProfiles pins the scale-derived isolation
// quota the cross-user family assumes to the quota the public profiles seed.
func TestV13IsolationQuotaMatchesTheProfiles(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		if got := v13IsoCasesForMem(prof.Mem); got != prof.IsoCases {
			t.Fatalf("%s: scale-derived isolation quota %d, profile seeds %d", runSize, got, prof.IsoCases)
		}
	}
}

// TestV13ReferenceRunKeepsTheFixedEnvelope proves the v13 families are carved
// out of the existing budget: the public case counts are unchanged from v12 and
// generation is deterministic.
func TestV13ReferenceRunKeepsTheFixedEnvelope(t *testing.T) {
	for _, seed := range []int64{1, 2, 3, 7, 11, 42, 123456789, 3058240546919425205} {
		for _, runSize := range []string{"small", "medium", "full"} {
			prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("v13 seed %d %s: %v", seed, runSize, err)
			}
			again, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(artifact, again) {
				t.Fatalf("v13 seed %d %s is not deterministic", seed, runSize)
			}
			// The plumbing envelope (profilesV13): full carries 100 tool cases and
			// 250 memory cases (224 primary + 9 isolation + the fixed tail); small
			// and medium keep the v12 counts.
			want := map[string]int{"small": 30, "medium": 143, "full": 350}[runSize]
			if got := len(artifact.ToolCases) + len(artifact.MemoryCases); got != want {
				t.Fatalf("v13 seed %d %s run has %d cases, want fixed envelope %d", seed, runSize, got, want)
			}
			for _, mc := range artifact.MemoryCases {
				if mc.BenchVersion != protocol.BenchVersionV13 {
					t.Fatalf("v13 seed %d %s case %s carries bench_version %d", seed, runSize, mc.ID, mc.BenchVersion)
				}
			}
		}
	}
}

// v13Availability replays the ordered seed boundary: tool prerequisites first,
// then memory waves in order, returning the set of pair ids available AFTER
// each wave index (index 0 includes the prerequisites).
func v13Availability(tools []protocol.ToolCase, waves []protocol.SeedRequest) []map[string]bool {
	out := make([]map[string]bool, len(waves))
	seen := map[string]bool{}
	for _, tc := range tools {
		for _, pair := range tc.PrerequisitePairs {
			seen[pair.PairID] = true
		}
	}
	for _, wave := range waves {
		if wave.UserID != "" && wave.UserID != PrimaryUser {
			// The secondary isolation graph is seeded up front, before any wave.
			for _, pair := range wave.Pairs {
				seen[pair.PairID] = true
			}
		}
	}
	for w, wave := range waves {
		if wave.UserID == "" || wave.UserID == PrimaryUser {
			for _, pair := range wave.Pairs {
				seen[pair.PairID] = true
			}
		}
		snapshot := make(map[string]bool, len(seen))
		for id := range seen {
			snapshot[id] = true
		}
		out[w] = snapshot
	}
	return out
}

// TestV13EveryDeclaredMemoryCaseAnswerableAfterItsWave is the v13 form of
// TestV8InitialWorldMakesEveryDeclaredMemoryCaseAnswerable. v8-v12 seed the
// whole world before any case, so "answerable" meant "available at wave 0".
// v13 stages a bounded share of corrections into later waves, so the contract
// becomes: every record a case requires is available by the wave it runs after
// -- and the staged cases are genuinely NOT answerable one wave earlier, which
// is what makes the runner's synchronous ingest ack load-bearing.
func TestV13EveryDeclaredMemoryCaseAnswerableAfterItsWave(t *testing.T) {
	for _, runSize := range []string{"small", "medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		for _, seed := range []int64{1, 42, 123456789, 3058240546919425205} {
			rng, err := NewRNGForVersion(seed, protocol.BenchVersionV13)
			if err != nil {
				t.Fatal(err)
			}
			tools, _ := GenerateToolsForVersion(rng, seed, prof.Tools, protocol.BenchVersionV13)
			suite, err := GenerateMemorySuiteForVersion(rng, seed, prof.Mem, prof.Waves, prof.RawPairsFrac, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			available := v13Availability(tools, suite.Waves)
			declared, later := 0, 0
			for _, staged := range suite.Cases {
				w := staged.RunAfterWave
				if w < 0 || w >= len(available) {
					t.Fatalf("%s seed %d case %s runs after wave %d of %d", runSize, seed, staged.Case.ID, w, len(available))
				}
				for _, pairID := range staged.RequiredPairIDs {
					declared++
					if !available[w][pairID] {
						t.Fatalf("%s seed %d case %s requires pair %s unavailable after wave %d", runSize, seed, staged.Case.ID, pairID, w)
					}
				}
				if w == 0 {
					continue
				}
				later++
				earlier := false
				for _, pairID := range staged.RequiredPairIDs {
					if !available[w-1][pairID] {
						earlier = true
					}
				}
				if !earlier {
					t.Fatalf("%s seed %d case %s is staged after wave %d but already answerable after wave %d", runSize, seed, staged.Case.ID, w, w-1)
				}
			}
			if declared == 0 {
				t.Fatalf("%s seed %d exposed no evidence declarations", runSize, seed)
			}
			if runSize != "small" && (later == 0 || suite.StagedCorrectionCases != later) {
				t.Fatalf("%s seed %d staged %d cases into later waves (telemetry %d); the realism waves must carry dependent cases", runSize, seed, later, suite.StagedCorrectionCases)
			}
			if runSize != "small" && (len(suite.Waves[1].Pairs) == 0 || len(suite.Waves[2].Pairs) == 0) {
				t.Fatalf("%s seed %d realism waves 1/2 carry %d/%d pairs", runSize, seed, len(suite.Waves[1].Pairs), len(suite.Waves[2].Pairs))
			}
			// Staged corrections are a bounded realism share of the world's
			// corrections (10-15%), never the world itself.
			scale, _ := v8WorldProfile(prof.Mem)
			world := universe.GenerateForVersion(seed, scale, protocol.BenchVersionV13)
			corrections := len(world.People) + len(world.Projects) + len(world.Trips)
			stagedPairs := 0
			for w := 1; w < len(suite.Waves); w++ {
				stagedPairs += len(suite.Waves[w].Pairs)
			}
			if runSize != "small" && (stagedPairs*100 < 10*corrections || stagedPairs*100 > 15*corrections) {
				t.Fatalf("%s seed %d stages %d of %d corrections; want 10-15%%", runSize, seed, stagedPairs, corrections)
			}
			worldCases := 0
			for _, staged := range suite.Cases {
				if strings.HasPrefix(staged.Case.QuestionType, "world-") {
					worldCases++
				}
			}
			if later*2 > worldCases {
				t.Fatalf("%s seed %d stages %d of %d world cases; waves must stay realism-only", runSize, seed, later, worldCases)
			}
		}
	}
}

// TestV13StagedCorrectionsAreAbsentFromTheInitialSeed proves the tool-side
// carrier and the memory waves agree: a staged correction is delivered exactly
// once, in a later wave, never as a tool prerequisite.
func TestV13StagedCorrectionsAreAbsentFromTheInitialSeed(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	artifact, err := GenerateDataset(123456789, prof, protocol.BenchVersionV13)
	if err != nil {
		t.Fatal(err)
	}
	world := universe.GenerateForVersion(123456789, 3, protocol.BenchVersionV13)
	staged := world.StagedCorrectionWaves(world.V13Allocation(9), prof.Waves)
	if len(staged) == 0 {
		t.Fatal("no staged corrections")
	}
	deliveries := map[string][]string{}
	for _, tc := range artifact.ToolCases {
		for _, pair := range tc.PrerequisitePairs {
			deliveries[pair.PairID] = append(deliveries[pair.PairID], "prerequisite")
		}
	}
	for w, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			deliveries[pair.PairID] = append(deliveries[pair.PairID], fmt.Sprintf("wave-%d", w))
		}
	}
	for id, wave := range staged {
		got := deliveries[id]
		if len(got) != 1 || got[0] != fmt.Sprintf("wave-%d", wave) {
			t.Fatalf("staged correction %s delivered as %v, want exactly wave-%d", id, got, wave)
		}
	}
	// Every non-staged world record still arrives before the first tool case.
	for _, pair := range world.InitialPairs(staged) {
		if got := deliveries[pair.PairID]; len(got) != 1 || got[0] != "prerequisite" {
			t.Fatalf("world record %s delivered as %v, want exactly the tool prerequisite seed", pair.PairID, got)
		}
	}
}

// staticStateIndex is the harness a v13 as-of case is built to defeat: it
// resolves the entity and returns the CURRENT value regardless of any same-turn
// anchor. On an as-of pair it is right exactly once.
func staticStateIndex(mc protocol.MemoryCase, current string) protocol.RunResponse {
	return protocol.RunResponse{Answer: current, FinalText: "The current value on file is " + current + "."}
}

// TestV13PointInTimeTwinsDefeatAStaticStateIndex proves the primary carrier:
// twelve as-of cases per full seed (six as_of_twin pairs), the same-turn anchor
// rendered in the question, the oracle scoring 1.0 on both halves, and a
// pre-compiled current-state index scoring 0 on every before-half.
func TestV13PointInTimeTwinsDefeatAStaticStateIndex(t *testing.T) {
	for _, tc := range []struct {
		runSize string
		want    int
	}{{"full", 12}, {"medium", 4}, {"small", 0}} {
		prof, _ := ProfileForVersion(tc.runSize, protocol.BenchVersionV13)
		for seed := int64(1); seed <= 12; seed++ {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("%s seed %d: %v", tc.runSize, seed, err)
			}
			pairs := map[string][]ArtifactCase{}
			count := 0
			for _, mc := range artifact.MemoryCases {
				if !strings.HasPrefix(mc.QuestionType, QTPointInTime) {
					if mc.TwinRelation == protocol.TwinRelationAsOf {
						t.Fatalf("%s seed %d: non point-in-time case %s carries the as_of_twin relation", tc.runSize, seed, mc.ID)
					}
					continue
				}
				count++
				if mc.TwinRelation != protocol.TwinRelationAsOf || mc.TwinPairID == "" || mc.TwinGroup != "" {
					t.Fatalf("%s seed %d: as-of case %s relation=%q pair=%q group=%q", tc.runSize, seed, mc.ID, mc.TwinRelation, mc.TwinPairID, mc.TwinGroup)
				}
				if mc.RunAfterWave != 0 {
					t.Fatalf("%s seed %d: as-of case %s must not depend on a staged wave", tc.runSize, seed, mc.ID)
				}
				pairs[mc.TwinPairID] = append(pairs[mc.TwinPairID], mc)
			}
			if count != tc.want {
				t.Fatalf("%s seed %d: %d point-in-time cases, want %d", tc.runSize, seed, count, tc.want)
			}
			for pairID, members := range pairs {
				if len(members) != 2 {
					t.Fatalf("%s seed %d: as-of pair %s has %d members", tc.runSize, seed, pairID, len(members))
				}
				a, b := members[0], members[1]
				if a.ExpectedAnswer == b.ExpectedAnswer || a.Question == b.Question {
					t.Fatalf("%s seed %d: as-of pair %s halves coincide", tc.runSize, seed, pairID)
				}
				// Each half's answer is the other's planted distractor: the current
				// value is a distractor on the before-half, so the static index that
				// returns it scores 0 there and 1 on the after-half.
				staticIndexScore := 0.0
				for _, half := range members {
					other := a
					if half.ID == a.ID {
						other = b
					}
					if !contains(half.DistractorAnswers, other.ExpectedAnswer) {
						t.Fatalf("%s seed %d: as-of half %s does not plant its twin's answer", tc.runSize, seed, half.ID)
					}
					oracle := protocol.RunResponse{Answer: renderV13Answer(half.MemoryCase), FinalText: "As of that date the records show " + renderV13Answer(half.MemoryCase) + "."}
					if v := grade.Memory(half.MemoryCase, oracle); v.Score != 1 {
						t.Fatalf("%s seed %d: oracle scored %v on as-of case %s: %v", tc.runSize, seed, v.Score, half.ID, v.Notes)
					}
					// The current value is whichever half carries the later anchor;
					// derive it as the answer of the half whose distractors hold the
					// other's answer AND whose question is not the superseded one. The
					// generator orders [before, after] per pair, so after == the half
					// whose ExpectedAnswer the before-half plants; both plant each
					// other, so identify "current" through the world instead.
					staticIndexScore += grade.Memory(half.MemoryCase, staticStateIndex(half.MemoryCase, renderV13Answer(v13CurrentHalf(members).MemoryCase))).Score
				}
				if staticIndexScore != 1 {
					t.Fatalf("%s seed %d: static-state index scored %v on as-of pair %s, want exactly one half", tc.runSize, seed, staticIndexScore, pairID)
				}
			}
		}
	}
}

// v13CurrentHalf picks the after-half of an as-of pair: the generator emits the
// halves in [before, after] order and placeV13TwinPairs keeps that order per
// pair only through the opaque coin, so recover it from the question text: the
// after-half anchor is the later date.
func v13CurrentHalf(members []ArtifactCase) ArtifactCase {
	a, b := members[0], members[1]
	if v13AnchorDate(a.Question).After(v13AnchorDate(b.Question)) {
		return a
	}
	return b
}

// renderV13Answer renders an expected answer the way a harness would speak it:
// money in cents becomes a decimal currency amount; everything else is verbatim.
func renderV13Answer(mc protocol.MemoryCase) string {
	if mc.AnswerKind == protocol.AnswerMoney {
		cents := 0
		fmt.Sscanf(mc.ExpectedAnswer, "%d", &cents)
		return fmt.Sprintf("$%d.%02d", cents/100, cents%100)
	}
	return mc.ExpectedAnswer
}

func contains(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}

// v13AnchorDate parses the same-turn anchor rendered in an as-of question.
func v13AnchorDate(question string) time.Time {
	m := pitDateRe.FindString(question)
	date, err := time.Parse("January 2, 2006", m)
	if err != nil {
		return time.Time{}
	}
	return date
}
