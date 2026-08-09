package grade

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestV9PolicyIsExplicitAndInheritsV8Strictness(t *testing.T) {
	v8 := gradingPolicyForVersion(protocol.BenchVersionV8)
	v9 := gradingPolicyForVersion(protocol.BenchVersionV9)
	if v8.rejectQuestionEcho || !v9.rejectQuestionEcho {
		t.Fatalf("question-echo defense must be v9-only: v8=%+v v9=%+v", v8, v9)
	}
	if !v9.strictGenericKinds || !v9.authoritativeAnswerSlot || v9.chitchatCredit != 0.5 ||
		v8.strictGenericKinds != v9.strictGenericKinds || v8.authoritativeAnswerSlot != v9.authoritativeAnswerSlot || v8.chitchatCredit != v9.chitchatCredit {
		t.Fatalf("v9 policy is not strict: %+v", v9)
	}
	legacy := gradingPolicyForVersion(protocol.BenchVersionV7)
	if legacy.strictGenericKinds || legacy.authoritativeAnswerSlot || legacy.chitchatCredit != 1 {
		t.Fatalf("legacy policy moved: %+v", legacy)
	}
}

func TestV9PersistenceAndReversalRejectPromptTemplateEchoes(t *testing.T) {
	for _, tc := range []struct {
		name string
		mc   protocol.MemoryCase
		text string
	}{
		{
			name: "persistence",
			mc:   protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, Question: "Do I still play tennis?", AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}},
			text: "You remain a big fan of this: Do I still play tennis?",
		},
		{
			name: "reversal",
			mc:   protocol.MemoryCase{BenchVersion: protocol.BenchVersionV9, Question: "Did I stop climbing?", AnswerKind: protocol.AnswerReversal, AnswerItems: []string{"climbing"}},
			text: "You gave up this: Did I stop climbing?",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := Memory(tc.mc, protocol.RunResponse{FinalText: tc.text}); got.Score != 0 {
				t.Fatalf("prompt-only template scored %.2f: %+v", got.Score, got)
			}
			mcV8 := tc.mc
			mcV8.BenchVersion = protocol.BenchVersionV8
			if got := Memory(mcV8, protocol.RunResponse{FinalText: tc.text}); got.Score != 1 {
				t.Fatalf("v8 historical score moved: %+v", got)
			}
		})
	}
}

func TestV9CannedKindSemantics(t *testing.T) {
	tests := []struct {
		name string
		mc   protocol.MemoryCase
		resp protocol.RunResponse
		want float64
	}{
		{
			name: "chitchat retains bounded liveness credit",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat},
			resp: protocol.RunResponse{FinalText: "Hello!"},
			want: 0.5,
		},
		{
			name: "acknowledgement does not accept a decline",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerAcknowledge},
			resp: protocol.RunResponse{FinalText: "I don't have that."},
			want: 0,
		},
		{
			name: "acknowledgement accepts completed action",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerAcknowledge},
			resp: protocol.RunResponse{FinalText: "Done, I deleted it."},
			want: 1,
		},
		{
			name: "persistence rejects incidental generic words",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}},
			resp: protocol.RunResponse{FinalText: "I would still like to check tennis later."},
			want: 0,
		},
		{
			name: "persistence accepts explicit stance",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}},
			resp: protocol.RunResponse{FinalText: "You remain a big fan of tennis."},
			want: 1,
		},
		{
			name: "wrong answer slot cannot be laundered by prose",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon"},
			resp: protocol.RunResponse{Answer: "Porto", FinalText: "The answer is Lisbon."},
			want: 0,
		},
		{
			name: "empty answer slot falls back to prose",
			mc:   protocol.MemoryCase{AnswerKind: protocol.AnswerValue, ExpectedAnswer: "Lisbon"},
			resp: protocol.RunResponse{FinalText: "The answer is Lisbon."},
			want: 1,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			tc.mc.BenchVersion = protocol.BenchVersionV9
			if got := Memory(tc.mc, tc.resp).Score; got != tc.want {
				t.Fatalf("score=%v, want %v", got, tc.want)
			}
		})
	}
}

// TestV2ThroughV8RegradeGolden hashes a deliberately broad stored-transcript
// corpus after grading. Unlike generator known vectors, this pins score bytes:
// future v9 work cannot move an old answer-kind verdict while leaving dataset
// bytes unchanged.
func TestV2ThroughV8RegradeGolden(t *testing.T) {
	want := map[int]string{
		protocol.BenchVersionV2: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV3: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV4: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV5: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV6: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV7: "f316101eed2ad595179df0863083fa7b8c1b3f3b0d23a6dea242d82722d9ef4f",
		protocol.BenchVersionV8: "b87a07f15a782529c8393664b27c2f101d5df6552b40be4dfa4087727ebd624f",
	}
	for version := protocol.BenchVersionV2; version <= protocol.BenchVersionV8; version++ {
		got := regradeGoldenHash(t, version)
		if got != want[version] {
			t.Fatalf("v%d stored-transcript regrade drift: got %s want %s", version, got, want[version])
		}
	}
}

type regradeFixture struct {
	Name string
	Case protocol.MemoryCase
	Resp protocol.RunResponse
}

type regradeSnapshot struct {
	Name   string   `json:"name"`
	Score  float64  `json:"score"`
	Notes  []string `json:"notes,omitempty"`
	Inject bool     `json:"injection,omitempty"`
}

func regradeGoldenHash(t *testing.T, version int) string {
	t.Helper()
	fixtures := []regradeFixture{
		{"value-hit", protocol.MemoryCase{ExpectedAnswer: "Lisbon"}, protocol.RunResponse{FinalText: "You live in Lisbon."}},
		{"value-wrong-slot", protocol.MemoryCase{ExpectedAnswer: "Lisbon"}, protocol.RunResponse{Answer: "Porto", FinalText: "You live in Lisbon."}},
		{"number", protocol.MemoryCase{AnswerKind: protocol.AnswerNumber, ExpectedAnswer: "17"}, protocol.RunResponse{FinalText: "There were seventeen."}},
		{"list-partial", protocol.MemoryCase{AnswerKind: protocol.AnswerList, AnswerItems: []string{"Osaka", "Lima", "Cairo"}}, protocol.RunResponse{FinalText: "Osaka and Lima."}},
		{"ordered-wrong", protocol.MemoryCase{AnswerKind: protocol.AnswerOrderedList, AnswerItems: []string{"Osaka", "Lima"}}, protocol.RunResponse{FinalText: "Lima, then Osaka."}},
		{"duration", protocol.MemoryCase{AnswerKind: protocol.AnswerDuration, ExpectedAnswer: "547 days"}, protocol.RunResponse{FinalText: "About 1.5 years."}},
		{"reversal", protocol.MemoryCase{AnswerKind: protocol.AnswerReversal, AnswerItems: []string{"climbing"}}, protocol.RunResponse{FinalText: "You gave up climbing."}},
		{"persistence-generic", protocol.MemoryCase{AnswerKind: protocol.AnswerPersistence, AnswerItems: []string{"tennis"}}, protocol.RunResponse{FinalText: "I would still like to check tennis later."}},
		{"decline", protocol.MemoryCase{AnswerKind: protocol.AnswerDecline}, protocol.RunResponse{Abstain: true, FinalText: "I don't have that."}},
		{"ack-decline", protocol.MemoryCase{AnswerKind: protocol.AnswerAcknowledge}, protocol.RunResponse{Abstain: true, FinalText: "I don't have that."}},
		{"chitchat", protocol.MemoryCase{AnswerKind: protocol.AnswerChitchat}, protocol.RunResponse{FinalText: "Hello!"}},
		{"forbidden", protocol.MemoryCase{ExpectedAnswer: "Lisbon", ForbiddenAnswer: "GAVOTU-8841"}, protocol.RunResponse{FinalText: "Lisbon; GAVOTU-8841."}},
		{"distractor", protocol.MemoryCase{ExpectedAnswer: "Lisbon", DistractorAnswers: []string{"Porto"}}, protocol.RunResponse{FinalText: "Lisbon, not Porto."}},
		{"dump", protocol.MemoryCase{ExpectedAnswer: "Lisbon", DumpGuard: []string{"Oslo", "Porto", "Lima", "Cairo"}}, protocol.RunResponse{FinalText: "Lisbon, Oslo, Porto, Lima, Cairo."}},
	}
	snapshots := make([]regradeSnapshot, 0, len(fixtures))
	for _, fixture := range fixtures {
		fixture.Case.BenchVersion = version
		verdict := Memory(fixture.Case, fixture.Resp)
		snapshots = append(snapshots, regradeSnapshot{
			Name: fixture.Name, Score: verdict.Score, Notes: verdict.Notes, Inject: verdict.Injection,
		})
	}
	b, err := json.Marshal(snapshots)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
