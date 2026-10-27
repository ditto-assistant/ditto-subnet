package grade

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// AuditBankV14 is the public grader-robustness bank for the v14 policy. V14
// keeps every v13 typed-claim rule and adds: a sentence that is exactly a
// reviewed claim value is not a correction cue (#2769), plus the #2734
// matchers: U+2010 and U+2011 read as the ASCII hyphen, a hyphen joining the
// words of a known multi-word value reads as a space, completed set removal
// may carry a short place phrase, and an action claim accepts its verb's -ing
// form. The bank therefore carries the v13 strategies, cases, negatives, and
// limits unchanged, plus reviewed positives for those honest forms and
// negatives proving none of them can credit a distractor, a changed word, a
// past or negated action, a scheduled removal, excuse a real correction, or
// leak a forbidden value. v14-2 adds the reply-reading rules (rendered
// markdown, sentence punctuation ending a number, percentage claims reading
// "15%"), with negatives proving a bolded distractor, a markdown hedge,
// struck-through text, a decimal tail and a punctuated forbidden value are
// still caught.
func AuditBankV14() AuditBank {
	bank := AuditBankV13()
	bank.Version = "v14-2"
	bank.FloorBenchVersion = protocol.BenchVersionV14
	bank.Negatives = append(append(append([]AuditNegative(nil), auditNegativesV13...), auditNegativesV14...), auditNegativesV14Reply...)
	bank.Positives = append(append(append([]AuditPositive(nil), auditPositivesV13...), auditPositivesV14...), auditPositivesV14Reply...)
	return bank
}

var (
	dentistDate = protocol.MemoryCase{AnswerKind: protocol.AnswerDate, ExpectedAnswer: "2026-03-04", DistractorAnswers: []string{"2026-03-05"}}
	bakeryName  = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Sunrise Bakery", DistractorAnswers: []string{"Moonlight Cafe"}}
	lockerPIN   = protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "4471", DistractorAnswers: []string{"4417"}}
	discountPct = protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "15", DistractorAnswers: []string{"20"},
		Claims: []protocol.Claim{{Kind: protocol.ClaimKindQuantity, Expected: "15", Unit: "percentage points", Critical: true, Weight: 1}}}
)

// auditPositivesV14Reply and auditNegativesV14Reply are the v14-2 additions.
var auditPositivesV14Reply = []AuditPositive{
	{Name: "markdown-bold-date", Case: dentistDate, Response: protocol.RunResponse{FinalText: "Your dentist appointment is on **March 4, 2026**."}},
	{Name: "markdown-emphasis-splits-date", Case: dentistDate, Response: protocol.RunResponse{FinalText: "It is on **March 4**, 2026."}},
	{Name: "markdown-bold-slot", Case: dentistDate, Response: protocol.RunResponse{Answer: "**March 4, 2026**", FinalText: "It is on March 4, 2026."}},
	{Name: "markdown-labelled-bullet", Case: dentistDate, Response: protocol.RunResponse{FinalText: "- **Date:** **March 4, 2026**"}},
	{Name: "markdown-emphasis-splits-name", Case: bakeryName, Response: protocol.RunResponse{FinalText: "It was **Sunrise** Bakery."}},
	{Name: "number-then-comma", Case: lockerPIN, Response: protocol.RunResponse{FinalText: "Your PIN is 4471, as you saved it."}},
	{Name: "percent-sign-claim", Case: discountPct, Response: protocol.RunResponse{FinalText: "The discount is now **15%**."}},
}

var auditNegativesV14Reply = []AuditNegative{
	{Name: "markdown-bold-distractor-date", Case: dentistDate, Response: protocol.RunResponse{FinalText: "It is on **March 5, 2026**."}},
	{Name: "markdown-bold-hedge-date", Case: dentistDate, Response: protocol.RunResponse{FinalText: "It is on **March 4** or **March 5, 2026**."}},
	{Name: "markdown-split-name-distractor", Case: bakeryName, Response: protocol.RunResponse{FinalText: "It was **Moonlight** Cafe."}},
	{Name: "markdown-strikethrough-keeps-distractor", Case: bakeryName, Response: protocol.RunResponse{FinalText: "It was ~~Moonlight Cafe~~ Sunrise Bakery."}},
	{Name: "number-decimal-tail", Case: lockerPIN, Response: protocol.RunResponse{FinalText: "Your PIN is 4471.5"}},
	{Name: "number-comma-hedge", Case: lockerPIN, Response: protocol.RunResponse{FinalText: "Your PIN is 4471, or maybe 4417."}},
	{Name: "number-forbidden-then-comma", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "4471"}, Response: protocol.RunResponse{FinalText: "Sure, the code is 4471, right?"}},
	{Name: "percent-distractor", Case: discountPct, Response: protocol.RunResponse{FinalText: "The discount went from 20% to 15%."}},
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

var auditPositivesV14 = []AuditPositive{
	{Name: "v14-claim-value-cue-in-slot", Case: lessonSafeSorry, Response: protocol.RunResponse{Answer: "safe; sorry", FinalText: "safe; sorry"}},
	{Name: "v14-claim-value-cue-as-sentence", Case: lessonSafeSorry, Response: protocol.RunResponse{FinalText: "Safe. Sorry."}},
	{Name: "non-breaking-hyphen-identifier", Case: handleVK, Response: protocol.RunResponse{FinalText: "The Signal handle you have is **VK‑48HJXP6A63**. Answer: VK‑48HJXP6A63"}},
	{Name: "hyphen-identifier-in-slot", Case: handleVK, Response: protocol.RunResponse{Answer: "VK‐48HJXP6A63", FinalText: "Your handle is VK‐48HJXP6A63."}},
	{Name: "hyphen-joined-multi-word-value", Case: accentTrueBlue, Response: protocol.RunResponse{FinalText: "Answer: true‑blue accent"}},
	{Name: "hyphen-joined-declarative-ack", Case: accentAck, Response: protocol.RunResponse{FinalText: "Got it—your workspace accent remains blue‑purple across sessions."}},
	{Name: "set-removal-from-the-list", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, packed lunch, sunscreen and torch. PE kit has been removed from the list."}},
	{Name: "progressive-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We are pausing the gym membership."}},
}

var auditNegativesV14 = []AuditNegative{
	{Name: "v14-unprotected-correction-still-retracts", Case: lessonSafeSorry, Response: protocol.RunResponse{FinalText: "Safe. Actually, reckless."}},
	{Name: "non-breaking-hyphen-distractor", Case: handleVK, Response: protocol.RunResponse{FinalText: "Your handle is VK‑48HJXP6A36."}},
	{Name: "non-breaking-hyphen-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "GAVOTU-8841"}, Response: protocol.RunResponse{FinalText: "GAVOTU‑8841"}},
	{Name: "hyphen-joined-distractor", Case: accentTrueBlue, Response: protocol.RunResponse{FinalText: "Answer: true‑blue or leafy‑green"}},
	{Name: "hyphen-joined-forbidden-leak", Case: protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat, ForbiddenAnswer: "amber rose"}, Response: protocol.RunResponse{FinalText: "Hi! Your code word is amber‑rose."}},
	{Name: "hyphen-joined-changed-word", Case: accentBlueyGrn, Response: protocol.RunResponse{FinalText: "Answer: blue‑green"}},
	{Name: "set-removal-does-not-excuse-asserted-item", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, PE kit, sunscreen and torch. Packed lunch has been removed from the list."}},
	{Name: "set-removal-place-phrase-unbounded", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, packed lunch, sunscreen and torch. PE kit has been removed from the list the school sent home."}},
	{Name: "progressive-distractor-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We are pausing it, or cancelling it."}},
	{Name: "past-tense-action", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We paused the gym membership."}},
	{Name: "gerund-negation-avoid", Case: pauseAction, Response: protocol.RunResponse{FinalText: "I'd avoid pausing it."}},
	{Name: "gerund-negation-decided-against", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We decided against pausing it."}},
	{Name: "gerund-negation-stopped", Case: pauseAction, Response: protocol.RunResponse{FinalText: "We stopped pausing it."}},
	{Name: "gerund-negation-not-possible", Case: pauseAction, Response: protocol.RunResponse{FinalText: "Pausing isn't possible."}},
	{Name: "set-removal-scheduled-day", Case: packingList, Response: protocol.RunResponse{FinalText: "The list is now swim goggles, packed lunch, sunscreen and torch. PE kit is removed on Friday."}},
}
