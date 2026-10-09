package grade

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// AuditBankV14 is the public grader-robustness bank for the v14 policy. V14
// keeps every v13 typed-claim rule and adds the #2734 matchers: U+2010 and
// U+2011 read as the ASCII hyphen, a hyphen joining the words of a known
// multi-word value reads as a space, completed set removal may carry a short
// place phrase, and an action claim accepts its verb's -ing form. The bank
// therefore carries the v13 strategies, cases, negatives, and limits
// unchanged, plus reviewed positives for those honest forms and negatives
// proving none of them can credit a distractor, a changed word, a past action,
// or leak a forbidden value.
func AuditBankV14() AuditBank {
	bank := AuditBankV13()
	bank.Version = "v14-1"
	bank.FloorBenchVersion = protocol.BenchVersionV14
	bank.Negatives = append(append([]AuditNegative(nil), auditNegativesV13...), auditNegativesV14...)
	bank.Positives = append(append([]AuditPositive(nil), auditPositivesV13...), auditPositivesV14...)
	return bank
}

var handleVK = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "VK-48HJXP6A63", DistractorAnswers: []string{"VK-48HJXP6A36"}}

var (
	accentTrueBlue = protocol.MemoryCase{QuestionType: "declarative-behavior", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "true blue", DistractorAnswers: []string{"leafy green"}}
	accentBlueyGrn = protocol.MemoryCase{QuestionType: "declarative-behavior", AnswerKind: protocol.AnswerValue, ExpectedAnswer: "bluey green"}
	accentAck      = protocol.MemoryCase{QuestionType: DeclarativeAckQuestionType, AnswerKind: protocol.AnswerValue,
		Question:       "Please keep my workspace accent blue purple from now on.",
		ExpectedAnswer: "blue purple", AcceptAny: []string{"got it", "noted", "understood"}}
	packingList = func() protocol.MemoryCase {
		items := []string{"swim goggles", "packed lunch", "sunscreen", "torch"}
		mc := protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: items, AnswerItemKinds: make([]string, len(items)),
			ExpectedAnswer: "swim goggles; packed lunch; sunscreen; torch", DistractorAnswers: []string{"PE kit", "raincoat"}}
		for _, item := range items {
			mc.Claims = append(mc.Claims, protocol.Claim{Kind: protocol.ClaimKindSetMember, Expected: item, Accept: []string{item}, Weight: 0.25})
		}
		return mc
	}()
	pauseAction = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "pause it", DistractorAnswers: []string{"cancel it"},
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindAction, Expected: "pause it", Accept: []string{"pause it", "pause", "put it on pause", "freeze it for a few months"}, Weight: 1}}}
)

var auditPositivesV14 = []AuditPositive{
	{Name: "non-breaking-hyphen-identifier", Case: handleVK, Response: protocol.RunResponse{FinalText: "The Signal handle you have is **VK‑48HJXP6A63**. Answer: VK‑48HJXP6A63"}},
	{Name: "hyphen-identifier-in-slot", Case: handleVK, Response: protocol.RunResponse{Answer: "VK‐48HJXP6A63", FinalText: "Your handle is VK‐48HJXP6A63."}},
	{Name: "hyphen-joined-multi-word-value", Case: accentTrueBlue, Response: protocol.RunResponse{FinalText: "Answer: true‑blue accent"}},
	{Name: "hyphen-joined-declarative-ack", Case: accentAck, Response: protocol.RunResponse{FinalText: "Got it—your workspace accent remains blue‑purple across sessions."}},
	{Name: "set-removal-from-the-list", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, packed lunch, sunscreen and torch. PE kit has been removed from the list."}},
	{Name: "progressive-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We are pausing the gym membership."}},
}

var auditNegativesV14 = []AuditNegative{
	{Name: "non-breaking-hyphen-distractor", Case: handleVK, Response: protocol.RunResponse{FinalText: "Your handle is VK‑48HJXP6A36."}},
	{Name: "non-breaking-hyphen-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}, Response: protocol.RunResponse{FinalText: "GAVOTU‑8841"}},
	{Name: "hyphen-joined-distractor", Case: accentTrueBlue, Response: protocol.RunResponse{FinalText: "Answer: true‑blue or leafy‑green"}},
	{Name: "hyphen-joined-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "amber rose"}, Response: protocol.RunResponse{FinalText: "Hi! Your code word is amber‑rose."}},
	{Name: "hyphen-joined-changed-word", Case: accentBlueyGrn, Response: protocol.RunResponse{FinalText: "Answer: blue‑green"}},
	{Name: "set-removal-does-not-excuse-asserted-item", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, PE kit, sunscreen and torch. Packed lunch has been removed from the list."}},
	{Name: "set-removal-place-phrase-unbounded", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, packed lunch, sunscreen and torch. PE kit has been removed from the list the school sent home."}},
	{Name: "progressive-distractor-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We are pausing it, or cancelling it."}},
	{Name: "past-tense-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We paused the gym membership."}},
}
