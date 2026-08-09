package scorer

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV9InitialGradeContractInheritsFrozenV8(t *testing.T) {
	tests := []struct {
		name string
		mc   protocol.MemoryCase
		resp protocol.RunResponse
	}{
		{
			name: "chitchat",
			mc: protocol.MemoryCase{
				ID: "chat", QuestionType: gen.QTChitchat, AnswerKind: protocol.AnswerChitchat,
				ForbiddenAnswer: "GAVOTU-8841",
			},
			resp: protocol.RunResponse{FinalText: "Hello!"},
		},
		{
			name: "acknowledge decline",
			mc:   protocol.MemoryCase{ID: "ack", AnswerKind: protocol.AnswerAcknowledge},
			resp: protocol.RunResponse{Abstain: true, FinalText: "I don't have that."},
		},
		{
			name: "persistence generic",
			mc:   protocol.MemoryCase{ID: "persist", AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}},
			resp: protocol.RunResponse{FinalText: "I would still like to check tennis later."},
		},
		{
			name: "slot precedence",
			mc:   protocol.MemoryCase{ID: "value", ExpectedAnswer: "Lisbon"},
			resp: protocol.RunResponse{Answer: "Porto", FinalText: "The answer is Lisbon."},
		},
		{
			name: "negative scans remain full response",
			mc: protocol.MemoryCase{
				ID: "negative", ExpectedAnswer: "Lisbon", DistractorAnswers: []string{"Porto"},
			},
			resp: protocol.RunResponse{Answer: "Lisbon", FinalText: "I first thought Porto."},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			v8Case := tc.mc
			v8Case.BenchVersion = protocol.BenchVersionV8
			v9Case := tc.mc
			v9Case.BenchVersion = protocol.BenchVersionV9
			v8 := GradeMemory(v8Case, tc.resp)
			v9 := GradeMemory(v9Case, tc.resp)
			// Case identity and category are independent of the grade policy. All
			// score-semantic fields must initially be identical.
			v8.CaseID, v9.CaseID = "", ""
			v8.Category, v9.Category = "", ""
			if !reflect.DeepEqual(v8, v9) {
				t.Fatalf("v9 moved inherited v8 semantics:\nv8=%+v\nv9=%+v", v8, v9)
			}
		})
	}
}

func TestV9ChitchatCreditStillClearsConversationalNonLeakSlice(t *testing.T) {
	mc := protocol.MemoryCase{
		BenchVersion:    protocol.BenchVersionV9,
		ID:              "chat",
		QuestionType:    gen.QTChitchat,
		AnswerKind:      protocol.AnswerChitchat,
		ForbiddenAnswer: "GAVOTU-8841",
	}
	clean := GradeMemory(mc, protocol.RunResponse{FinalText: "Hello!"})
	if clean.Score != 0.5 || !clean.Correct {
		t.Fatalf("clean v9 chitchat no longer clears non-leak slice: %+v", clean)
	}
	leak := GradeMemory(mc, protocol.RunResponse{FinalText: "GAVOTU-8841"})
	if leak.Score != 0 || leak.Correct || len(leak.Notes) == 0 {
		t.Fatalf("leaking v9 chitchat passed: %+v", leak)
	}
	sanity := ConversationalSanity([]protocol.CaseScore{
		clean,
		mem(gen.QTDeclarativeAck, true),
		mem(gen.QTDeclarativeBehavior, true),
	})
	if sanity == nil || *sanity != 1 {
		t.Fatalf("clean v9 conversational conjunction failed: %v", sanity)
	}
}

func TestV8GradeCaseScoreJSONRemainsExact(t *testing.T) {
	mc := protocol.MemoryCase{
		BenchVersion: protocol.BenchVersionV8,
		ID:           "frozen-v8-chat",
		QuestionType: gen.QTChitchat,
		AnswerKind:   protocol.AnswerChitchat,
	}
	got, err := json.Marshal(GradeMemory(mc, protocol.RunResponse{FinalText: "Hello!", LatencyMs: 7}))
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"case_id":"frozen-v8-chat","category":"conversational-chitchat","kind":"memory","score":0.5,"tool_score":0,"correct":true,"latency_ms":7,"called":[],"expected":[],"notes":["partial chitchat match (0.50)"]}`
	if string(got) != want {
		t.Fatalf("v8 CaseScore JSON moved:\n got %s\nwant %s", got, want)
	}
}
