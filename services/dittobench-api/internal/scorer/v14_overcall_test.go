package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Pin the public composite entry point: a denominator change is a contract
// change even when the authorized write itself carries no penalty.
func TestV14DeclarativeOverCallContract(t *testing.T) {
	for _, tc := range []struct {
		name  string
		calls []string
		want  float64
	}{
		{"no calls", nil, 1},
		{"read", []string{"search_memories"}, 1},
		{"save", []string{"save_memory"}, 1},
		{"update", []string{"update_memory"}, 1},
		{"delete", []string{"delete_memory"}, 1},
		{"all authorized", []string{"save_memory", "update_memory", "delete_memory"}, 1},
		{"theme", []string{"set_theme"}, .75},
		{"accent", []string{"set_accent_color"}, .75},
		{"email", []string{"gmail_send"}, .75},
		{"mixed", []string{"save_memory", "gmail_send", "set_theme"}, .75},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cs := memCase(gen.QTDeclarativeAck, tc.calls...)
			cs.Correct = true
			cs.Score = 1 // Isolate over-call from conversational correctness.
			if got := CompositeGateForVersion([]protocol.CaseScore{cs}, protocol.BenchVersionV14); got != tc.want {
				t.Fatalf("v14 composite gate = %v, want %v", got, tc.want)
			}
			if got := CompositeGateForVersion([]protocol.CaseScore{cs}, protocol.BenchVersionV13); got != 1 {
				t.Fatalf("historical v13 whole-case exclusion changed: %v", got)
			}
		})
	}
}

func TestV14DenominatorAndHistoricalScoring(t *testing.T) {
	ack := memCase(gen.QTDeclarativeAck, "save_memory")
	ack.Correct = true
	ack.Score = 1
	input := []protocol.CaseScore{ack, memCase("single-session-recall", "gmail_send")}
	// Every v2-v13 entry-point result is frozen. V2-v6 use the historical
	// 10% penalty, v7-v12 the 25% penalty. V13 skips
	// the declarative case entirely, producing 1/1 rather than v14's 1/2.
	for version := protocol.BenchVersionV2; version <= protocol.BenchVersionV14; version++ {
		want := .75
		switch {
		case version < protocol.BenchVersionV7:
			want = .9
		case version == protocol.BenchVersionV14:
			want = .875
		}
		if got := CompositeGateForVersion(input, version); got != want {
			t.Errorf("v%d gate = %v, want %v", version, got, want)
		}
	}
	// Lifecycle and unobserved cases do not dilute the rate; writes on
	// non-declarative memory cases remain over-calls.
	for _, category := range []string{gen.QTChitchat, gen.QTLifecycleRead, "single-session-recall"} {
		cs := memCase(category, "save_memory")
		cs.Correct = true
		cs.Score = 1
		unobserved := memCase(gen.QTDeclarativeAck)
		unobserved.Observed = false
		unobserved.Correct = true
		unobserved.Score = 1
		in := []protocol.CaseScore{cs, memCase(gen.QTLifecycleWrite, "gmail_send"), unobserved}
		if got := CompositeGateForVersion(in, protocol.BenchVersionV14); got != .75 {
			t.Errorf("%s: exclusions or write scope changed: %v", category, got)
		}
	}
}
