package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestCanonicalAnswersSelfGrade is the answer-key sufficiency invariant: for
// every memory case the generator emits, a response consisting of the case's
// OWN canonical answer must grade to 1.0. A case whose key cannot pass its own
// grader is unwinnable by construction (this sweep is what surfaced the
// common-word-blocked "May" birthday month). Declines are answered with the
// abstain flag, the primary wire signal.
func TestCanonicalAnswersSelfGrade(t *testing.T) {
	prof, _ := ProfileFor("full")
	for seed := int64(1); seed <= 30; seed++ {
		a, err := GenerateDataset(seed, prof, protocol.BenchVersionV2)
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
		for _, c := range a.MemoryCases {
			resp := protocol.RunResponse{FinalText: c.ExpectedAnswer}
			if c.AnswerKind == protocol.AnswerDecline {
				resp = protocol.RunResponse{Abstain: true, FinalText: "I don't have that on record."}
			}
			if v := grade.Memory(c.MemoryCase, resp); v.Score != 1 {
				t.Errorf("seed %d %s (%s/%s): canonical key %q scores %.2f (%v) — case is unwinnable",
					seed, c.QuestionID, c.QuestionType, c.AnswerKind, c.ExpectedAnswer, v.Score, v.Notes)
			}
		}
	}
}
