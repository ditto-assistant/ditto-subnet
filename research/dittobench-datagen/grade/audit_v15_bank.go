package grade

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// AuditBankV15 is the public grader-robustness bank for the v15 policy. V15
// keeps every v14 rule and reads a case's multi-word values as single phrases
// during clause segmentation (caseValuesAreAtomic), so a value containing a
// boundary or rejection word ("macaroni and cheese", "Stick No Bills") grades
// as itself. The negatives prove the phrase protection cannot credit a hedge
// over the value and a distractor, a rejected value, or an asserted
// distractor that itself contains the boundary word.
func AuditBankV15() AuditBank {
	bank := AuditBankV14()
	bank.Version = "v15-1"
	bank.FloorBenchVersion = protocol.BenchVersionV15
	bank.Negatives = append(append([]AuditNegative(nil), bank.Negatives...), auditNegativesV15...)
	bank.Positives = append(append([]AuditPositive(nil), bank.Positives...), auditPositivesV15...)
	return bank
}

var (
	accentMacaroni = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "macaroni and cheese", DistractorAnswers: []string{"pale brown", "forest green"}}
	accentTeal     = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "teal green", DistractorAnswers: []string{"macaroni and cheese", "dark cyan"}}
	fontStick      = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Stick No Bills", DistractorAnswers: []string{"Inter"}}
)

var auditPositivesV15 = []AuditPositive{
	{Name: "conjunction-value-bare", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "Macaroni and cheese."}},
	{Name: "conjunction-value-in-sentence", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "I'd apply your macaroni and cheese accent."}},
	{Name: "conjunction-value-slot", Case: accentMacaroni, Response: protocol.RunResponse{Answer: "macaroni and cheese", FinalText: "Your accent is macaroni and cheese."}},
	{Name: "conjunction-value-over-rejected-distractor", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "Macaroni and cheese, not pale brown."}},
	{Name: "rejection-word-value", Case: fontStick, Response: protocol.RunResponse{FinalText: "Your interface font is Stick No Bills."}},
	{Name: "rejected-conjunction-distractor", Case: accentTeal, Response: protocol.RunResponse{FinalText: "Teal green, not macaroni and cheese."}},
}

var auditNegativesV15 = []AuditNegative{
	{Name: "conjunction-value-hedged-with-distractor", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "Either macaroni and cheese or pale brown."}},
	{Name: "conjunction-value-rejected", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "Not macaroni and cheese; I'd go with pale brown."}},
	{Name: "conjunction-distractor-asserted", Case: accentTeal, Response: protocol.RunResponse{FinalText: "Your accent is macaroni and cheese."}},
	{Name: "conjunction-fragment-is-not-the-value", Case: accentMacaroni, Response: protocol.RunResponse{FinalText: "Cheese."}},
}
