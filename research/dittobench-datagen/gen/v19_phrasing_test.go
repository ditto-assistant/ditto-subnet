package gen

import (
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// v19RephrasedQuestionType reports whether the v19 phrase grammar
// (universe/v19_phrasing.go) words a question type: story oracles, business
// and personal programs, the abstention probes with their decision twins,
// and the isolation contact twin.
func v19RephrasedQuestionType(qt string) bool {
	return strings.HasPrefix(qt, "world-story-") || strings.HasPrefix(qt, QTAbsence) ||
		qt == universe.V13ProgramQuestionType || qt == universe.V13PersonalQuestionType || qt == "world-isolation-contact-current"
}

// The grammar reaches the full v19 dataset and changes only the wording: a
// case v18 and v19 both ask keeps its answer, accept sets, claims, distractors
// and grounding, and nearly all of them read differently.
func TestV19DatasetRephrasesQuestionsNotAnswers(t *testing.T) {
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV19)
	for seed := int64(1); seed <= 4; seed++ {
		a18, err := GenerateDataset(seed, prof, protocol.BenchVersionV18)
		if err != nil {
			t.Fatal(err)
		}
		a19, err := GenerateDataset(seed, prof, protocol.BenchVersionV19)
		if err != nil {
			t.Fatal(err)
		}
		v18 := map[string]ArtifactCase{}
		for _, c := range a18.MemoryCases {
			v18[c.ID] = c
		}
		shared, reworded := 0, 0
		for _, c := range a19.MemoryCases {
			old, ok := v18[c.ID]
			if !ok || !v19RephrasedQuestionType(c.QuestionType) {
				continue
			}
			shared++
			if old.ExpectedAnswer != c.ExpectedAnswer || old.AnswerKind != c.AnswerKind ||
				!reflect.DeepEqual(old.AnswerItems, c.AnswerItems) || !reflect.DeepEqual(old.AcceptAny, c.AcceptAny) ||
				!reflect.DeepEqual(old.AnswerItemAcceptAny, c.AnswerItemAcceptAny) || !reflect.DeepEqual(old.Claims, c.Claims) ||
				!reflect.DeepEqual(old.DistractorAnswers, c.DistractorAnswers) || !reflect.DeepEqual(old.GroundingTokens, c.GroundingTokens) {
				t.Fatalf("seed %d %s (%s): v19 changed the graded answer, not just the wording", seed, c.ID, c.QuestionType)
			}
			if old.Question != c.Question {
				reworded++
			}
		}
		if shared < 60 || reworded < shared*9/10 {
			t.Fatalf("seed %d: %d of %d shared rephrased cases reworded", seed, reworded, shared)
		}
	}
}
