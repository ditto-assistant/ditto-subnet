package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func memCase(category string, called ...string) protocol.CaseScore {
	return protocol.CaseScore{Kind: protocol.KindMemory, Category: category, Observed: true, Called: called}
}

// TestDeclarativeAckIsAMemoryWriteFromV13 is the (b) repair. Keeping a value the
// user just stated is the declarative-acknowledgement case's own work, so the
// save call that does it must not be charged as a memory over-call.
func TestDeclarativeAckIsAMemoryWriteFromV13(t *testing.T) {
	for _, tool := range []string{"save_memory", "update_memory", "delete_memory"} {
		t.Run(tool, func(t *testing.T) {
			in := []protocol.CaseScore{
				memCase(gen.QTDeclarativeAck, tool),
				memCase("single-session-recall", tool),
			}
			// v13: only the recall half over-calls, so the rate is 1/1.
			if got := memoryOverCallFactorWith(in, v7MemoryOverCallMaxPenalty, protocol.BenchVersionV13); got != 1.0-v7MemoryOverCallMaxPenalty {
				t.Fatalf("v13 declarative ack must be exempt: got %.6f", got)
			}
			// v12 and earlier are unchanged: both halves over-call, same rate,
			// but the ack case is counted, which is the defect being preserved
			// for already-scored contracts.
			if got := memoryOverCallFactorWith(in, v7MemoryOverCallMaxPenalty, protocol.BenchVersionV12); got != 1.0-v7MemoryOverCallMaxPenalty {
				t.Fatalf("v12 rate changed: got %.6f", got)
			}
		})
	}
}

// TestDeclarativeAckExemptionChangesTheGateOnlyAtV13 shows the exemption moving
// a real score, and shows v8..v12 explicitly not moving.
func TestDeclarativeAckExemptionChangesTheGateOnlyAtV13(t *testing.T) {
	// A harness that saves every stated value and does nothing else wrong.
	in := []protocol.CaseScore{
		memCase(gen.QTDeclarativeAck, "save_memory"),
		memCase(gen.QTDeclarativeAck, "save_memory"),
		memCase("single-session-recall", "search_memories"),
	}
	v12 := memoryOverCallFactorWith(in, v7MemoryOverCallMaxPenalty, protocol.BenchVersionV12)
	v13 := memoryOverCallFactorWith(in, v7MemoryOverCallMaxPenalty, protocol.BenchVersionV13)
	if v13 != 1.0 {
		t.Fatalf("v13 must charge a correct harness nothing here, got %.6f", v13)
	}
	want := round6(1.0 - v7MemoryOverCallMaxPenalty*2.0/3.0)
	if v12 != want {
		t.Fatalf("v12 must keep its historical penalty %.6f, got %.6f", want, v12)
	}
}

// TestMemoryWriteCategoryScope pins which categories are exempt at which
// contract, so widening the repair to v8..v12 cannot happen by accident: that
// is a rescore, and a separate decision.
func TestMemoryWriteCategoryScope(t *testing.T) {
	cases := []struct {
		category string
		version  int
		want     bool
	}{
		{gen.QTLifecycleWrite, protocol.BenchVersionV3, true},
		{gen.QTLifecycleWrite, protocol.BenchVersionV13, true},
		{gen.QTDeclarativeAck, protocol.BenchVersionV3, false},
		{gen.QTDeclarativeAck, protocol.BenchVersionV8, false},
		{gen.QTDeclarativeAck, protocol.BenchVersionV12, false},
		{gen.QTDeclarativeAck, protocol.BenchVersionV13, true},
		{gen.QTLifecycleRead, protocol.BenchVersionV13, false},
		{gen.QTChitchat, protocol.BenchVersionV13, false},
		{"single-session-recall", protocol.BenchVersionV13, false},
	}
	for _, tc := range cases {
		if got := memoryWriteCategory(tc.category, tc.version); got != tc.want {
			t.Errorf("memoryWriteCategory(%q, v%d) = %v, want %v", tc.category, tc.version, got, tc.want)
		}
	}
}

// TestCompositeGateV13ExemptionIsWiredThrough proves the version actually
// reaches the factor from the public entry point.
func TestCompositeGateV13ExemptionIsWiredThrough(t *testing.T) {
	in := []protocol.CaseScore{
		memCase(gen.QTDeclarativeAck, "save_memory"),
		memCase("single-session-recall", "search_memories"),
	}
	v12 := CompositeGateForVersion(in, protocol.BenchVersionV12)
	v13 := CompositeGateForVersion(in, protocol.BenchVersionV13)
	if v12 >= v13 {
		t.Fatalf("v13 gate %.6f should exceed the v12 gate %.6f for a harness that only saved a stated value", v13, v12)
	}
	// Pre-v7 contracts do not reach compositeGateV7 at all.
	if got := CompositeGateForVersion(in, protocol.BenchVersionV6); got != CompositeGateForVersion(in, protocol.BenchVersionV6) {
		t.Fatal("pre-v7 gate is not stable")
	}
}
