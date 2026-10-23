package grade

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// AuditBankV14 is the public grader-robustness bank for the v14 policy. V14
// keeps every v13 typed-claim rule and adds one reply-text fold: U+2010 and
// U+2011 read as the ASCII hyphen (#2734). The bank therefore carries the v13
// strategies, cases, negatives, and limits unchanged, plus reviewed positives
// for the hyphen forms the pinned model writes and negatives proving the fold
// cannot credit a distractor or leak a forbidden value.
func AuditBankV14() AuditBank {
	bank := AuditBankV13()
	bank.Version = "v14-1"
	bank.FloorBenchVersion = protocol.BenchVersionV14
	bank.Negatives = append(append([]AuditNegative(nil), auditNegativesV13...), auditNegativesV14...)
	bank.Positives = append(append([]AuditPositive(nil), auditPositivesV13...), auditPositivesV14...)
	return bank
}

var handleVK = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "VK-48HJXP6A63", DistractorAnswers: []string{"VK-48HJXP6A36"}}

var auditPositivesV14 = []AuditPositive{
	{Name: "non-breaking-hyphen-identifier", Case: handleVK, Response: protocol.RunResponse{FinalText: "The Signal handle you have is **VK‑48HJXP6A63**. Answer: VK‑48HJXP6A63"}},
	{Name: "hyphen-identifier-in-slot", Case: handleVK, Response: protocol.RunResponse{Answer: "VK‐48HJXP6A63", FinalText: "Your handle is VK‐48HJXP6A63."}},
}

var auditNegativesV14 = []AuditNegative{
	{Name: "non-breaking-hyphen-distractor", Case: handleVK, Response: protocol.RunResponse{FinalText: "Your handle is VK‑48HJXP6A36."}},
	{Name: "non-breaking-hyphen-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}, Response: protocol.RunResponse{FinalText: "GAVOTU‑8841"}},
}
