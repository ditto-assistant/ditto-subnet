package grade

import "github.com/ditto-assistant/dittobench-datagen/protocol"

func AuditBankV14() AuditBank {
	bank := AuditBankV13()
	bank.Version = "v14-1"
	bank.FloorBenchVersion = protocol.BenchVersionV14
	bank.Negatives = append(append([]AuditNegative(nil), bank.Negatives...),
		AuditNegative{Name: "v14-unprotected-correction-still-retracts", Case: lessonSafeSorry,
			Response: protocol.RunResponse{FinalText: "Safe. Actually, reckless."}},
	)
	bank.Positives = append(append([]AuditPositive(nil), bank.Positives...),
		AuditPositive{Name: "v14-claim-value-cue-in-slot", Case: lessonSafeSorry,
			Response: protocol.RunResponse{Answer: "safe; sorry", FinalText: "safe; sorry"}},
		AuditPositive{Name: "v14-claim-value-cue-as-sentence", Case: lessonSafeSorry,
			Response: protocol.RunResponse{FinalText: "Safe. Sorry."}},
	)
	return bank
}

var lessonSafeSorry = protocol.MemoryCase{
	QuestionType:   "world-story-lesson-claims",
	Question:       "What did I take away from the whole thing?",
	AnswerKind:     protocol.AnswerList,
	ExpectedAnswer: "safe; sorry",
	AnswerItems:    []string{"safe", "sorry"},
	Claims: []protocol.Claim{
		{Kind: protocol.ClaimKindConcept, Expected: "safe", Weight: 1,
			Accept: []string{"caution", "careful", "cautious", "play it safe", "better safe than sorry"}},
		{Kind: protocol.ClaimKindConcept, Expected: "sorry", Weight: 1,
			Accept: []string{"regret", "regretting it", "pay for it later", "wishing you had", "better safe than sorry"}},
	},
}
