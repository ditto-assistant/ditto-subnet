package universe

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestGenerateForVersionKeepsFrozenWorldsByteIdentical is the byte half of the
// v13 world contract: below v13 the versioned constructor is exactly Generate,
// and at v13 the v8 pairs are a prefix of the world (probes are appended, never
// interleaved or rewritten).
func TestGenerateForVersionKeepsFrozenWorldsByteIdentical(t *testing.T) {
	for _, scale := range []int{1, 2, 3} {
		for seed := int64(1); seed <= 10; seed++ {
			base := Generate(seed, scale)
			for version := protocol.BenchVersionV8; version <= protocol.BenchVersionV12; version++ {
				if got := GenerateForVersion(seed, scale, version); !reflect.DeepEqual(got, base) {
					t.Fatalf("seed %d scale %d v%d world differs from the frozen Generate output", seed, scale, version)
				}
			}
			v13 := GenerateForVersion(seed, scale, protocol.BenchVersionV13)
			if len(v13.Pairs) < len(base.Pairs) || !reflect.DeepEqual(v13.Pairs[:len(base.Pairs)], base.Pairs) {
				t.Fatalf("seed %d scale %d: v13 world does not keep the v8 pairs as a byte-identical prefix", seed, scale)
			}
			if v13.Probes == nil || len(v13.Pairs)-len(base.Pairs) != len(v13.Probes.Pairs) {
				t.Fatalf("seed %d scale %d: v13 probes are not exactly the appended pairs", seed, scale)
			}
			ids := map[string]bool{}
			for _, pair := range v13.Pairs {
				if ids[pair.PairID] {
					t.Fatalf("seed %d scale %d: duplicate pair id %s", seed, scale, pair.PairID)
				}
				ids[pair.PairID] = true
			}
		}
	}
}

// TestV13AsOfPairsValidateAcrossSeeds proves every as-of pair passes the v8
// plan proof on both halves, the before-half answer is the superseded value,
// and the anchors render as dates the harness can compare against timestamps.
func TestV13AsOfPairsValidateAcrossSeeds(t *testing.T) {
	for _, scale := range []int{2, 3} {
		wantPeople, wantProjects, wantTrips := V13AsOfPairCounts(scale)
		for seed := int64(1); seed <= 40; seed++ {
			w := GenerateForVersion(seed, scale, protocol.BenchVersionV13)
			a := w.V13Allocation(9)
			pairs, err := w.V13AsOfPairs(a)
			if err != nil {
				t.Fatalf("seed %d scale %d: %v", seed, scale, err)
			}
			if len(pairs) != wantPeople+wantProjects+wantTrips {
				t.Fatalf("seed %d scale %d: %d as-of pairs, want %d", seed, scale, len(pairs), wantPeople+wantProjects+wantTrips)
			}
			for _, pair := range pairs {
				before, after := pair.Before, pair.After
				if before.Case.ExpectedAnswer == after.Case.ExpectedAnswer {
					t.Fatalf("seed %d: as-of pair %s/%d answers coincide", seed, pair.Kind, pair.Index)
				}
				// The before-half asks the SUPERSEDED value: the current value is
				// its planted distractor, so a current-state index scores 0 there.
				if !grade.Hit(after.Case.ExpectedAnswer, strings.Join(before.Case.DistractorAnswers, " ")) &&
					before.Case.AnswerKind == protocol.AnswerValue {
					t.Fatalf("seed %d: before-half of %s/%d does not plant the current value", seed, pair.Kind, pair.Index)
				}
				if before.AsOfAnchor().IsZero() || !before.AsOfAnchor().Before(after.AsOfAnchor()) {
					t.Fatalf("seed %d: as-of anchors are not ordered for %s/%d", seed, pair.Kind, pair.Index)
				}
				evidence := before.RequiredPairIDs
				chainOld, chainNew := evidence[len(evidence)-2], evidence[len(evidence)-1]
				if w.chainStateAt(before.AsOfAnchor(), chainOld, chainNew) != "before" || w.chainStateAt(after.AsOfAnchor(), chainOld, chainNew) != "after" {
					t.Fatalf("seed %d: anchors of %s/%d do not bracket the correction", seed, pair.Kind, pair.Index)
				}
			}
		}
	}
}

// TestV13DecisionPairsValidateAcrossSeeds proves the absence proof holds for
// every family on every seed: the oracle resolves to nothing, the tempting
// value is planted, the grounding is citable, and the twin is answerable.
func TestV13DecisionPairsValidateAcrossSeeds(t *testing.T) {
	for _, scale := range []int{2, 3} {
		counts := V13AbsenceFamilyCounts(scale)
		for seed := int64(1); seed <= 40; seed++ {
			w := GenerateForVersion(seed, scale, protocol.BenchVersionV13)
			a := w.V13Allocation(9)
			pairs, err := w.V13DecisionPairs(a, nil)
			if err != nil {
				t.Fatalf("seed %d scale %d: %v", seed, scale, err)
			}
			got := map[string]int{}
			for _, pair := range pairs {
				got[pair.Family]++
				if !pair.Unanswerable.Unanswerable || pair.Unanswerable.Case.AnswerKind != protocol.AnswerAbsence {
					t.Fatalf("seed %d: %s unanswerable plan is mis-kinded", seed, pair.Family)
				}
				if pair.Answerable.Case.AnswerKind == protocol.AnswerAbsence || pair.Answerable.Case.ExpectedAnswer == AbsenceExpectedAnswer {
					t.Fatalf("seed %d: %s twin is not answerable", seed, pair.Family)
				}
				if len(pair.Unanswerable.Case.GroundingTokens) == 0 {
					t.Fatalf("seed %d: %s has no grounding token", seed, pair.Family)
				}
				if pair.Unanswerable.Case.Question == pair.Answerable.Case.Question {
					t.Fatalf("seed %d: %s twins share a surface", seed, pair.Family)
				}
			}
			for family, want := range counts {
				if family == V13FamilyCrossUser {
					continue // supplied by the caller's isolation projection
				}
				if got[family] != want {
					t.Fatalf("seed %d scale %d: family %s has %d pairs, want %d (%v)", seed, scale, family, got[family], want, got)
				}
			}
		}
	}
}

// TestV13StagedCorrectionsLeaveTheInitialSeed pins the realism staging: the
// staged trip corrections are absent from the initial pairs, present in exactly
// one later wave, and the ordinary plans that need them unlock after it.
func TestV13StagedCorrectionsLeaveTheInitialSeed(t *testing.T) {
	w := GenerateForVersion(123456789, 3, protocol.BenchVersionV13)
	a := w.V13Allocation(9)
	staged := w.StagedCorrectionWaves(a, 5)
	if len(staged) != V13StagedTripCount(3) {
		t.Fatalf("staged %d corrections, want %d", len(staged), V13StagedTripCount(3))
	}
	initial := w.InitialPairs(staged)
	if len(initial)+len(staged) != len(w.Pairs) {
		t.Fatalf("initial %d + staged %d != %d pairs", len(initial), len(staged), len(w.Pairs))
	}
	for _, pair := range initial {
		if staged[pair.PairID] != 0 {
			t.Fatalf("staged pair %s leaked into the initial seed", pair.PairID)
		}
	}
	waves := map[int]int{}
	for _, wave := range staged {
		waves[wave]++
	}
	if waves[1] == 0 || waves[2] == 0 {
		t.Fatalf("staged corrections do not cover both realism waves: %v", waves)
	}
	plans, err := w.QuestionPlansV13(91, a)
	if err != nil {
		t.Fatal(err)
	}
	later := 0
	for _, plan := range plans {
		if UnlockWaveFor(plan, staged) > 0 {
			later++
		}
	}
	if later == 0 {
		t.Fatal("no ordinary plan depends on a staged correction; the realism waves would be empty")
	}
	// Membership never depends on the wave count (the tool-side carrier knows
	// no profile); a single-wave profile assigns everything to memory wave 0.
	single := w.StagedCorrectionWaves(a, 1)
	if len(single) != len(staged) {
		t.Fatalf("single-wave membership %d != %d", len(single), len(staged))
	}
	for id, wave := range single {
		if wave != 0 {
			t.Fatalf("single-wave profile assigned %s to wave %d", id, wave)
		}
		if _, ok := staged[id]; !ok {
			t.Fatalf("membership drifted with the wave count: %s", id)
		}
	}
	if got := Generate(123456789, 3).StagedCorrectionWaves(a, 5); len(got) != 0 {
		t.Fatalf("frozen world staged %d corrections", len(got))
	}
}

// TestQuestionPlansExcludingMatchesQuestionPlansWithoutExclusions guards the v8
// byte contract of the shared selector.
func TestQuestionPlansExcludingMatchesQuestionPlansWithoutExclusions(t *testing.T) {
	w := Generate(42, 3)
	a, err := w.QuestionPlans(120)
	if err != nil {
		t.Fatal(err)
	}
	b, err := w.QuestionPlansExcluding(120, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatal("QuestionPlansExcluding(nil) drifted from QuestionPlans")
	}
}
