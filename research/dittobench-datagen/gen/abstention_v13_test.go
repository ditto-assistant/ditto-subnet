package gen

import (
	"fmt"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func renderV13Answer(mc protocol.MemoryCase) string {
	if mc.AnswerKind == protocol.AnswerMoney {
		cents := 0
		fmt.Sscanf(mc.ExpectedAnswer, "%d", &cents)
		return fmt.Sprintf("$%d.%02d", cents/100, cents%100)
	}
	return mc.ExpectedAnswer
}

// v13MisleadingFamilies are the absence families whose evidence plants a
// tempting value (everything but pure absence).
var v13MisleadingFamilies = map[string]bool{
	universe.V13FamilyNearMiss: true, universe.V13FamilyStaleRemoved: true, universe.V13FamilyFalsePremise: true,
	universe.V13FamilyCrossUser: true, universe.V13FamilyInsufficient: true,
}

// TestV13AbstentionBudgetAndFamilyMixAcrossFortySeeds is the coverage half of
// #1530: 25 unanswerable cases per full seed (8 per medium seed), at least half
// from misleading-evidence families, pure absence at most a quarter, every
// family present, every case carrying a grounding token and a tempting value,
// and every case paired with an answerable decision twin.
func TestV13AbstentionBudgetAndFamilyMixAcrossFortySeeds(t *testing.T) {
	for _, tc := range []struct {
		runSize string
		want    int
	}{{"full", 25}, {"medium", 8}} {
		prof, _ := ProfileForVersion(tc.runSize, protocol.BenchVersionV13)
		for seed := int64(1); seed <= 40; seed++ {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("%s seed %d: %v", tc.runSize, seed, err)
			}
			families := map[string]int{}
			twins := map[string][]ArtifactCase{}
			unanswerable := 0
			for _, mc := range artifact.MemoryCases {
				switch {
				case strings.HasPrefix(mc.QuestionType, QTAbsenceTwin):
					if mc.AnswerKind == protocol.AnswerAbsence || mc.TwinRelation != protocol.TwinRelationDecision {
						t.Fatalf("%s seed %d: twin %s kind=%q relation=%q", tc.runSize, seed, mc.ID, mc.AnswerKind, mc.TwinRelation)
					}
					twins[mc.TwinPairID] = append(twins[mc.TwinPairID], mc)
				case strings.HasPrefix(mc.QuestionType, QTAbsence):
					unanswerable++
					family := strings.TrimPrefix(mc.QuestionType, QTAbsence)
					families[family]++
					if mc.AnswerKind != protocol.AnswerAbsence || mc.ExpectedAnswer != universe.AbsenceExpectedAnswer {
						t.Fatalf("%s seed %d: %s is not an absence case", tc.runSize, seed, mc.ID)
					}
					if len(mc.GroundingTokens) == 0 || len(mc.DistractorAnswers) == 0 {
						t.Fatalf("%s seed %d: %s lacks grounding (%d) or a tempting value (%d)", tc.runSize, seed, mc.ID, len(mc.GroundingTokens), len(mc.DistractorAnswers))
					}
					if mc.TwinRelation != protocol.TwinRelationDecision || mc.TwinPairID == "" || mc.TwinGroup != "" {
						t.Fatalf("%s seed %d: %s relation=%q pair=%q group=%q", tc.runSize, seed, mc.ID, mc.TwinRelation, mc.TwinPairID, mc.TwinGroup)
					}
					twins[mc.TwinPairID] = append(twins[mc.TwinPairID], mc)
				default:
					if mc.AnswerKind == protocol.AnswerAbsence || mc.TwinRelation == protocol.TwinRelationDecision {
						t.Fatalf("%s seed %d: %s (%s) carries absence grading outside the family", tc.runSize, seed, mc.ID, mc.QuestionType)
					}
				}
			}
			if unanswerable != tc.want {
				t.Fatalf("%s seed %d: %d unanswerable cases, want %d (%v)", tc.runSize, seed, unanswerable, tc.want, families)
			}
			misleading := 0
			for family, n := range families {
				if v13MisleadingFamilies[family] {
					misleading += n
				}
			}
			if 2*misleading < unanswerable {
				t.Fatalf("%s seed %d: misleading-evidence share %d/%d below 50%%: %v", tc.runSize, seed, misleading, unanswerable, families)
			}
			if 4*families[universe.V13FamilyPureAbsence] > unanswerable {
				t.Fatalf("%s seed %d: pure absence %d/%d above 25%%", tc.runSize, seed, families[universe.V13FamilyPureAbsence], unanswerable)
			}
			for _, family := range universe.V13AbsenceFamilies {
				if families[family] == 0 {
					t.Fatalf("%s seed %d: family %s absent: %v", tc.runSize, seed, family, families)
				}
			}
			if len(twins) != unanswerable {
				t.Fatalf("%s seed %d: %d decision pairs for %d unanswerable cases", tc.runSize, seed, len(twins), unanswerable)
			}
			for pairID, members := range twins {
				if len(members) != 2 {
					t.Fatalf("%s seed %d: pair %s has %d members", tc.runSize, seed, pairID, len(members))
				}
				a, b := members[0], members[1]
				if (a.AnswerKind == protocol.AnswerAbsence) == (b.AnswerKind == protocol.AnswerAbsence) {
					t.Fatalf("%s seed %d: pair %s is not one unanswerable + one answerable", tc.runSize, seed, pairID)
				}
				if strings.TrimPrefix(a.QuestionType, QTAbsenceTwin) != strings.TrimPrefix(b.QuestionType, QTAbsence) &&
					strings.TrimPrefix(b.QuestionType, QTAbsenceTwin) != strings.TrimPrefix(a.QuestionType, QTAbsence) {
					t.Fatalf("%s seed %d: pair %s mixes families %s/%s", tc.runSize, seed, pairID, a.QuestionType, b.QuestionType)
				}
			}
		}
	}
}

// TestV13RelationTwinsAreSpacedAndNeverAdjacent checks the placement rule for
// every relation pair (decision and as-of): within the wave-0 run order the two
// members are at least twenty cases apart.
func TestV13RelationTwinsAreSpacedAndNeverAdjacent(t *testing.T) {
	for _, runSize := range []string{"medium", "full"} {
		prof, _ := ProfileForVersion(runSize, protocol.BenchVersionV13)
		for seed := int64(1); seed <= 20; seed++ {
			artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
			if err != nil {
				t.Fatalf("%s seed %d: %v", runSize, seed, err)
			}
			position := map[string][]int{}
			wave0 := 0
			for _, mc := range artifact.MemoryCases {
				if mc.RunAfterWave != 0 || mc.UserID != "" {
					continue
				}
				if mc.TwinPairID != "" {
					position[mc.TwinPairID] = append(position[mc.TwinPairID], wave0)
				}
				wave0++
			}
			if len(position) == 0 {
				t.Fatalf("%s seed %d: no relation pairs", runSize, seed)
			}
			for pairID, at := range position {
				if len(at) != 2 {
					t.Fatalf("%s seed %d: pair %s has %d wave-0 members", runSize, seed, pairID, len(at))
				}
				distance := at[1] - at[0]
				if distance < 0 {
					distance = -distance
				}
				if distance < 20 {
					t.Fatalf("%s seed %d: pair %s members are %d apart, want >= 20", runSize, seed, pairID, distance)
				}
			}
		}
	}
}

// TestV13AbstentionOracleAndBaselinesThroughTheArtifact grades generated
// cases: a grounded oracle scores 1.0 on both halves of every decision pair;
// an always-answer policy (asserting the tempting value) scores 0 on every
// unanswerable case; an always-abstain policy scores 0 on every twin.
func TestV13AbstentionOracleAndBaselinesThroughTheArtifact(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV13)
	for seed := int64(1); seed <= 10; seed++ {
		artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV13)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, mc := range artifact.MemoryCases {
			switch {
			case strings.HasPrefix(mc.QuestionType, QTAbsence) && !strings.HasPrefix(mc.QuestionType, QTAbsenceTwin):
				token := mc.GroundingTokens[0]
				oracle := protocol.RunResponse{Abstain: true, FinalText: fmt.Sprintf("I don't have a record that establishes that. What I do have mentions %s, which does not answer it.", token)}
				if v := grade.Memory(mc.MemoryCase, oracle); v.Score != 1 {
					t.Fatalf("seed %d: grounded oracle scored %v on %s (%s): %v", seed, v.Score, mc.ID, mc.QuestionType, v.Notes)
				}
				cited := protocol.RunResponse{FinalText: fmt.Sprintf("The only matching record on file is %s, but that is not what you asked about, so I can't confirm it; the records only mention %s, nothing more.", renderV13Tempting(mc.MemoryCase), token)}
				if v := grade.Memory(mc.MemoryCase, cited); v.Score != 1 {
					t.Fatalf("seed %d: cited-and-rejected decline scored %v on %s (%s): %v", seed, v.Score, mc.ID, mc.QuestionType, v.Notes)
				}
				// The never-decide hedge: decline phrase first, tempting value offered
				// last, slot empty. It must fail this half (and its twin below).
				hedge := protocol.RunResponse{FinalText: fmt.Sprintf("I don't have a record that establishes that, but the closest I have is %s; the records mention %s.", renderV13Tempting(mc.MemoryCase), token)}
				if v := grade.Memory(mc.MemoryCase, hedge); v.Score != 0 {
					t.Fatalf("seed %d: hedge scored %v on %s (%s): %v", seed, v.Score, mc.ID, mc.QuestionType, v.Notes)
				}
				cited = protocol.RunResponse{FinalText: fmt.Sprintf("What I do have is %s, which is not what you asked about, so I can't confirm it; the records only mention %s, nothing more.", renderV13Tempting(mc.MemoryCase), token)}
				if v := grade.Memory(mc.MemoryCase, cited); v.Score != 1 {
					t.Fatalf("seed %d: cited-and-rejected decline scored %v on %s (%s): %v", seed, v.Score, mc.ID, mc.QuestionType, v.Notes)
				}
				alwaysAnswer := protocol.RunResponse{Answer: renderV13Tempting(mc.MemoryCase), FinalText: "It's " + renderV13Tempting(mc.MemoryCase) + "."}
				if v := grade.Memory(mc.MemoryCase, alwaysAnswer); v.Score != 0 {
					t.Fatalf("seed %d: always-answer scored %v on %s: %v", seed, v.Score, mc.ID, v.Notes)
				}
				generic := protocol.RunResponse{Abstain: true, FinalText: "I don't have that information."}
				if v := grade.Memory(mc.MemoryCase, generic); v.Score != 0 {
					t.Fatalf("seed %d: generic refusal scored %v on %s: %v", seed, v.Score, mc.ID, v.Notes)
				}
			case strings.HasPrefix(mc.QuestionType, QTAbsenceTwin):
				oracle := protocol.RunResponse{Answer: renderV13Answer(mc.MemoryCase), FinalText: "It's " + renderV13Answer(mc.MemoryCase) + "."}
				if v := grade.Memory(mc.MemoryCase, oracle); v.Score != 1 {
					t.Fatalf("seed %d: oracle scored %v on twin %s (%s): %v", seed, v.Score, mc.ID, mc.QuestionType, v.Notes)
				}
				alwaysAbstain := protocol.RunResponse{Abstain: true, FinalText: "I don't have a record that establishes that."}
				if v := grade.Memory(mc.MemoryCase, alwaysAbstain); v.Score != 0 {
					t.Fatalf("seed %d: always-abstain scored %v on twin %s: %v", seed, v.Score, mc.ID, v.Notes)
				}
				hedge := protocol.RunResponse{FinalText: "I don't have a record that establishes that, but the closest I have is " + renderV13Answer(mc.MemoryCase) + "."}
				if v := grade.Memory(mc.MemoryCase, hedge); v.Score != 0 {
					t.Fatalf("seed %d: hedge (prose decline, empty slot, true value) scored %v on twin %s: %v", seed, v.Score, mc.ID, v.Notes)
				}
			}
		}
	}
}

// renderV13Tempting renders the first tempting value the way a lure-following
// harness would assert it.
func renderV13Tempting(mc protocol.MemoryCase) string {
	value := mc.DistractorAnswers[0]
	if strings.HasPrefix(mc.QuestionType, QTAbsence+universe.V13FamilyInsufficient) {
		cents := 0
		fmt.Sscanf(value, "%d", &cents)
		return fmt.Sprintf("$%d.%02d", cents/100, cents%100)
	}
	return value
}
