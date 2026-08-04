package gen

import (
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v5Suite generates a full v5 memory suite for a seed (deterministic).
func v5Suite(t *testing.T, seed int64) MemorySuite {
	t.Helper()
	r, err := NewRNGForVersion(seed, protocol.BenchVersionV5)
	if err != nil {
		t.Fatalf("rng: %v", err)
	}
	suite, err := GenerateMemorySuiteForVersion(r, seed, 55, 4, 0.15, protocol.BenchVersionV5)
	if err != nil {
		t.Fatalf("generate v5: %v", err)
	}
	return suite
}

// TestV5ConversationalCasesPresent confirms every v5 conversational and
// declarative-write class is generated at full run size, and that pre-v5
// contracts carry none of them (the version gate holds).
func TestV5ConversationalCasesPresent(t *testing.T) {
	suite := v5Suite(t, 20260720)
	want := map[string]bool{
		QTChitchat: false, QTDeclarativeAck: false, QTAbstainConfab: false,
		QTDeclarativeWrite: false, QTDeclarativeRead: false, QTDeclarativeBehavior: false,
	}
	for _, sc := range suite.Cases {
		if _, ok := want[sc.Case.QuestionType]; ok {
			want[sc.Case.QuestionType] = true
		}
	}
	for qt, seen := range want {
		if !seen {
			t.Errorf("v5 suite missing conversational class %q", qt)
		}
	}
	if suite.ConversationalCases == 0 {
		t.Fatal("ConversationalCases telemetry is zero for a v5 full run")
	}

	// The gate: a v4 suite carries none of the new classes.
	r4, _ := NewRNGForVersion(20260720, protocol.BenchVersionV4)
	s4, err := GenerateMemorySuiteForVersion(r4, 20260720, 55, 4, 0.15, protocol.BenchVersionV4)
	if err != nil {
		t.Fatalf("generate v4: %v", err)
	}
	if s4.ConversationalCases != 0 {
		t.Fatalf("v4 must carry no conversational cases, got %d", s4.ConversationalCases)
	}
	for _, sc := range s4.Cases {
		if _, ok := want[sc.Case.QuestionType]; ok {
			t.Fatalf("v4 suite leaked a v5 class %q", sc.Case.QuestionType)
		}
	}
}

// TestV5Deterministic pins byte-stable v5 generation from the same seed (the
// (seed, bench_version) reproducibility contract, extended to v5).
func TestV5Deterministic(t *testing.T) {
	a := v5Suite(t, 777)
	b := v5Suite(t, 777)
	if len(a.Cases) != len(b.Cases) {
		t.Fatalf("case count drift: %d vs %d", len(a.Cases), len(b.Cases))
	}
	for i := range a.Cases {
		if a.Cases[i].Case.ID != b.Cases[i].Case.ID || a.Cases[i].Case.Question != b.Cases[i].Case.Question {
			t.Fatalf("case %d drift between two generations", i)
		}
	}
}

// TestV5SeedingInvariants pins the two properties the conversational cases depend
// on: greeting sentinels and abstention neighbors ARE seeded into the haystack
// (so a leak is reachable), and the coined declarative-write values are ABSENT
// from the haystack (so the persistence read is unfakeable).
func TestV5SeedingInvariants(t *testing.T) {
	suite := v5Suite(t, 31337)
	var hay strings.Builder
	for _, w := range suite.Waves {
		for _, p := range w.Pairs {
			hay.WriteString(p.Prompt)
			hay.WriteByte(' ')
			hay.WriteString(p.Response)
			hay.WriteByte(' ')
		}
	}
	haystack := hay.String()
	for _, sc := range suite.Cases {
		mc := sc.Case
		switch mc.QuestionType {
		case QTChitchat:
			if !strings.Contains(haystack, mc.ForbiddenAnswer) {
				t.Errorf("greeting sentinel %q not seeded — leak unreachable", mc.ForbiddenAnswer)
			}
		case QTAbstainConfab:
			for _, d := range mc.DistractorAnswers {
				if !strings.Contains(haystack, d) {
					t.Errorf("abstention neighbor %q not seeded — confabulation unreachable", d)
				}
			}
		case QTDeclarativeRead, QTDeclarativeBehavior:
			if strings.Contains(haystack, mc.ExpectedAnswer) {
				t.Errorf("declarative value %q present in haystack — persistence is fakeable", mc.ExpectedAnswer)
			}
		}
	}
}

// TestV5RegressionAuroraNine is the core Phase-A demonstration: a Unicorn-style
// leak-router (force every turn into a recall pipeline that dumps a retrieved
// value) scores far below an honest harness on the conversational classes. This
// is the "my name is Peyton -> Aurora-9" failure, now penalized by construction.
func TestV5RegressionAuroraNine(t *testing.T) {
	suite := v5Suite(t, 424242)

	// Seeded values a recall pipeline could retrieve, keyed nowhere the harness
	// legitimately needs: the router surfaces the first one it "retrieves". We use
	// each conversational case's OWN forbidden/distractor as the stray value it
	// would dump (that is exactly what force-routing a greeting produces).
	honest := 0.0
	router := 0.0
	n := 0
	for _, sc := range suite.Cases {
		mc := sc.Case
		var honestResp, routerResp protocol.RunResponse
		switch mc.QuestionType {
		case QTChitchat:
			honestResp = protocol.RunResponse{FinalText: "Hi! How can I help today?"}
			routerResp = protocol.RunResponse{FinalText: "Your value is " + mc.ForbiddenAnswer + "."}
		case QTDeclarativeAck:
			honestResp = protocol.RunResponse{FinalText: "Got it, I'll use " + mc.ExpectedAnswer + "."}
			routerResp = protocol.RunResponse{FinalText: "Your value is " + mc.ForbiddenAnswer + "."}
		case QTAbstainConfab:
			honestResp = protocol.RunResponse{FinalText: "I don't have that on file.", Abstain: true}
			routerResp = protocol.RunResponse{FinalText: "It's " + mc.DistractorAnswers[0] + "."}
		case QTDeclarativeBehavior:
			honestResp = protocol.RunResponse{FinalText: "Booking through " + mc.ExpectedAnswer + "."}
			// The router never captured the declarative preference (no save verb),
			// so it cannot name the honored value and guesses the rejected one.
			routerResp = protocol.RunResponse{FinalText: "I'll use " + mc.DistractorAnswers[0] + "."}
		default:
			continue
		}
		honest += grade.Memory(mc, honestResp).Score
		router += grade.Memory(mc, routerResp).Score
		n++
	}
	if n == 0 {
		t.Fatal("no conversational cases to score")
	}
	honestMean := honest / float64(n)
	routerMean := router / float64(n)
	if honestMean < 0.99 {
		t.Fatalf("honest harness should ace conversational sanity, got %.3f", honestMean)
	}
	if routerMean > 0.05 {
		t.Fatalf("leak-router should be near zero on conversational sanity, got %.3f", routerMean)
	}
	t.Logf("conversational sanity: honest=%.3f router=%.3f over %d cases", honestMean, routerMean, n)
}
