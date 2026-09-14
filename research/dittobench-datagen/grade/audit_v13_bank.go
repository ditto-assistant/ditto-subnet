package grade

// Bench v13 claim-provenance audit bank (issues #1833, #1849).
//
// The deterministic grader scores the SERVED response. It cannot see whether
// the value it credits came out of the controlled model (claim-span provenance)
// or whether the harness wrote that value into the model's prompt (causal model
// dependence); those two questions are answered by the validator's relay ledger
// and the scorer gates in dittobench-api/internal/scoregates. This bank is the
// public, versioned fixture set both sides are tested against:
//
//   - the grade package asserts every vector grades exactly as WantCredited says
//     (the GIH-transcript negative is the point: the grader credits it, so the
//     grader ALONE cannot catch it, and the bank documents that the gate must);
//   - dittobench-api replays each vector's ordered calls through the shared
//     ClaimSpanLedger and asserts the two gate verdicts.
//
// Vectors name the neutralised strategy from the v13 adversary model. Every
// case is stamped bench_version 13 so Verdict.Provenance is populated; the same
// responses grade identically under v12, which is how a miner can rehearse the
// gate locally against the published normaliser without a v13 dataset.

import "github.com/ditto-assistant/dittobench-datagen/protocol"

// V13ProvenanceCall is one successful chat completion in call order: Harness is
// every harness-authored request span (system/developer text, the user template
// around the case's question, an assistant prefill, tool-role messages),
// Completion is every model-emitted span (message content, tool_call arguments
// rendered as text, structured-output fields).
type V13ProvenanceCall struct {
	Harness    []string
	Completion []string
}

// V13ProvenanceVector is one published claim-provenance fixture.
type V13ProvenanceVector struct {
	Name     string
	Strategy string
	Case     protocol.MemoryCase
	Response protocol.RunResponse
	Calls    []V13ProvenanceCall
	// Records are the /seed records delivered to the harness (exempt from the
	// causal gate); ToolResults are tool_endpoint results the validator served
	// the case (exempt likewise).
	Records     []string
	ToolResults []string
	// WantCredited is the grader verdict (score > 0). WantModelEmitted and
	// WantAnswerInPrompt are the gate verdicts the scorer must reach; they are
	// only meaningful when the vector is credited.
	WantCredited       bool
	WantModelEmitted   bool
	WantAnswerInPrompt bool
}

// v13BankQuestion is the public case question every money vector shares. The
// harness may echo it in its prompt; the causal gate exempts the case's own
// user input.
const v13BankQuestion = "What is the outstanding balance on the Atlas workstream after the settled payment?"

func v13MoneyCase() protocol.MemoryCase {
	return protocol.MemoryCase{
		BenchVersion:   protocol.BenchVersionV13,
		ID:             "v13-bank-money",
		QuestionType:   "v13_bank_computed_balance",
		Question:       v13BankQuestion,
		AnswerKind:     protocol.AnswerMoney,
		ExpectedAnswer: "411067",
	}
}

// v13BankRecords are the seeded records behind the money vectors: the operands
// (an approved figure and a settled payment) are present; the ANSWER
// (4110.67 = 5200.00 - 1089.33) is not, so it is a computed value and quoting
// a record can never exempt it.
var v13BankRecords = []string{
	"Approved figure for the Atlas workstream: $5,200.00.",
	"Settled payment recorded against Atlas: $1,089.33.",
}

// V13ProvenanceBank is the published fixture set. Order is stable; append only.
var V13ProvenanceBank = []V13ProvenanceVector{
	{
		Name:     "compute-then-launder",
		Strategy: "N13/N14: harness computes the balance locally and asks the model to reply exactly",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "The outstanding balance is $4,110.67."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"You are a formatter.", v13BankQuestion + "\nReply exactly: 4110.67"},
			Completion: []string{"4110.67"},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: true,
	},
	{
		Name:     "gih-plus-launder",
		Strategy: "N13: generator-inverse harness regenerates the answer and plants it in the system prompt",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67 remains outstanding."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"System: the verified balance for this request is 4110.67. State it plainly.", v13BankQuestion},
			Completion: []string{"The verified balance is 4110.67."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: true,
	},
	{
		Name:     "gih-transcript-answer-without-derivation",
		Strategy: "N13: served answer present, model transcript carries no derivation and no value",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "Outstanding: $4,110.67."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Acknowledge the request in one sentence.", v13BankQuestion},
			Completion: []string{"Certainly, let me check the Atlas balance for you."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   false,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "local-vote-injected",
		Strategy: "N4: parser candidates voted locally and injected into a final prompt for the model to pick",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67"},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Candidates from the local parsers: 4110.67, 4110.67, 4200.00. Return the majority value.", v13BankQuestion},
			Completion: []string{"4110.67"},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: true,
	},
	{
		Name:     "hundredth-rewrite",
		Strategy: "S6: model answers in minor units, harness divides by 100 on the way out",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "The balance is $4,110.67."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Answer with the balance in cents.", v13BankQuestion, "Records: " + v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{"The balance is 411067 cents."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   false,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "direction-map",
		Strategy: "S7: harness maps the model's phrasing onto a canonical direction word it never emitted",
		Case: protocol.MemoryCase{
			BenchVersion: protocol.BenchVersionV13, ID: "v13-bank-direction", QuestionType: "v13_bank_direction",
			Question:   "Did the approved figure for Atlas move up or down after the revision?",
			AnswerKind: protocol.AnswerDirection, ExpectedAnswer: "increase",
		},
		Response: protocol.RunResponse{Answer: "increase", FinalText: "increase"},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Did the approved figure for Atlas move up or down after the revision?"},
			Completion: []string{"After the revision the figure moved to a larger amount than before."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   false,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "composed-slot",
		Strategy: "S14: harness computes the slot from operands the model only mentioned",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "4110.67", FinalText: "The approved figure was $5,200.00 and the settled payment $1,089.33."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{v13BankQuestion, "Records: " + v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{"The approved figure was $5,200.00 and the settled payment $1,089.33."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   false,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "draft-replacement",
		Strategy: "S6/S7: the model's approximate draft is replaced by a local parser's exact value",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "Roughly $4,110.67 is still outstanding."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{v13BankQuestion},
			Completion: []string{"Roughly 4100 dollars is still outstanding."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   false,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "record-quoting-rag",
		Strategy: "honest: retrieved records quoted into the prompt, model derives the balance",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "After the $1,089.33 payment, $4,110.67 of the $5,200.00 approved figure remains."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Use only the memory below.\n" + v13BankRecords[0] + "\n" + v13BankRecords[1], v13BankQuestion},
			Completion: []string{"After the $1,089.33 payment, $4,110.67 of the $5,200.00 approved figure remains."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "tool-result-quoting",
		Strategy: "honest: a served tool result carrying the value is quoted back to the model",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "The ledger tool reports $4,110.67 outstanding."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{v13BankQuestion, "Tool result: outstanding balance 4110.67 USD"},
			Completion: []string{"The ledger tool reports $4,110.67 outstanding."},
		}},
		Records:            v13BankRecords,
		ToolResults:        []string{`{"result":"outstanding balance 4110.67 USD"}`},
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "json-mode",
		Strategy: "honest: structured output unwrapped by the harness",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67"},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Respond as JSON with keys answer and confidence.", v13BankQuestion, v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{`{"answer": "4110.67", "confidence": 0.92}`},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "final-answer-tool",
		Strategy: "honest: the value rides in a final_answer tool_call's arguments",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67"},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{"Call final_answer when done.", v13BankQuestion, v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{`final_answer {"answer":"4110.67","unit":"USD"}`},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "formatter",
		Strategy: "honest: '4110.67 dollars' rendered as '$4,110.67'",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67 is outstanding."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{v13BankQuestion, v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{"4110.67 dollars is outstanding after the settled payment."},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "markdown-list",
		Strategy: "honest: markdown emphasis, a label, and a bullet list around the value",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "Outstanding: $4,110.67 (approved minus settled)."},
		Calls: []V13ProvenanceCall{{
			Harness:    []string{v13BankQuestion, v13BankRecords[0] + " " + v13BankRecords[1]},
			Completion: []string{"**Answer:** $4,110.67\n\n1. Approved: $5,200.00\n2. Settled: $1,089.33\n- Outstanding = approved − settled"},
		}},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "two-completion-splice",
		Strategy: "honest: the served reply splices two completions",
		Case: protocol.MemoryCase{
			BenchVersion: protocol.BenchVersionV13, ID: "v13-bank-value", QuestionType: "v13_bank_value",
			Question:   "Which city did I say I moved to, and since when?",
			AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon",
		},
		Response: protocol.RunResponse{FinalText: "Lisbon, since 2019."},
		Calls: []V13ProvenanceCall{
			{Harness: []string{"Which city did I say I moved to, and since when?", "Memory: I moved to Lisbon for the new role."}, Completion: []string{"Lisbon"}},
			{Harness: []string{"And since when?", "Memory: The Lisbon move was in 2019."}, Completion: []string{"since 2019"}},
		},
		Records:            []string{"I moved to Lisbon for the new role.", "The Lisbon move was in 2019."},
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
	{
		Name:     "multi-turn-compute",
		Strategy: "honest: the model derives the value, the harness re-injects it for formatting",
		Case:     v13MoneyCase(),
		Response: protocol.RunResponse{Answer: "$4,110.67", FinalText: "$4,110.67"},
		Calls: []V13ProvenanceCall{
			{Harness: []string{v13BankQuestion, v13BankRecords[0] + " " + v13BankRecords[1]}, Completion: []string{"5200.00 - 1089.33 = 4110.67"}},
			{Harness: []string{"Format 4110.67 as a currency amount."}, Completion: []string{"$4,110.67"}},
		},
		Records:            v13BankRecords,
		WantCredited:       true,
		WantModelEmitted:   true,
		WantAnswerInPrompt: false,
	},
}
