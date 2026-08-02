package scorer

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestToolEfficiencyFactor(t *testing.T) {
	obs := func(expected, called int, score float64) protocol.CaseScore {
		cs := protocol.CaseScore{Kind: protocol.KindTool, Observed: true, Score: score}
		for i := 0; i < expected; i++ {
			cs.Expected = append(cs.Expected, "t")
		}
		for i := 0; i < called; i++ {
			cs.Called = append(cs.Called, "t")
		}
		return cs
	}

	cases := []struct {
		name string
		in   []protocol.CaseScore
		want float64
	}{
		{"no observed cases → no effect", []protocol.CaseScore{{Kind: protocol.KindTool, Score: 1}}, 1.0},
		{"within budget → full", []protocol.CaseScore{obs(1, 1, 1)}, 1.0},
		{"one extra call is free", []protocol.CaseScore{obs(1, 2, 1)}, 1.0},
		{"saturated overshoot → floor", []protocol.CaseScore{obs(1, 6, 1)}, 1 - effMaxPenalty},
		{"mid overshoot", []protocol.CaseScore{obs(1, 4, 1)}, 1 - 0.5*effMaxPenalty}, // over=3, (3-1)/(5-1)=0.5
		{"low-scoring case excluded", []protocol.CaseScore{obs(1, 6, 0.2)}, 1.0},
		{"unobserved overshoot excluded", []protocol.CaseScore{{Kind: protocol.KindTool, Score: 1, Expected: []string{"t"}, Called: []string{"t", "t", "t", "t", "t", "t"}}}, 1.0},
	}
	for _, c := range cases {
		if got := ToolEfficiencyFactor(c.in); !approx(got, c.want) {
			t.Errorf("%s: ToolEfficiencyFactor=%v want %v", c.name, got, c.want)
		}
	}
}

func TestAggregateAppliesEfficiency(t *testing.T) {
	// A correct tool case observed overshooting its budget drops the composite
	// below the pure tool mean, while tool_mean itself stays pure accuracy.
	perCase := []protocol.CaseScore{
		{CaseID: "t1", Kind: protocol.KindTool, Observed: true, Score: 1.0,
			Expected: []string{"a"}, Called: []string{"a", "b", "c", "d", "e", "f"}}, // saturated
	}
	// Historical (v3..v6) curve: 15% saturated penalty. Pinned via an explicit
	// version — Aggregate follows CurrentBenchVersion, which is v7's deeper curve.
	r := AggregateForVersion("run", perCase, protocol.BenchVersionV5)
	if !approx(r.ToolMean, 1.0) {
		t.Fatalf("tool_mean should stay pure accuracy 1.0, got %v", r.ToolMean)
	}
	if !approx(r.Composite, 1-effMaxPenalty) {
		t.Fatalf("composite should be discounted by efficiency to %v, got %v", 1-effMaxPenalty, r.Composite)
	}
	// v7 tightens the same saturated overshoot to a 40% penalty.
	r7 := AggregateForVersion("run", perCase, protocol.BenchVersionV7)
	if !approx(r7.Composite, 1-v7EffMaxPenalty) {
		t.Fatalf("v7 composite should be discounted to %v, got %v", 1-v7EffMaxPenalty, r7.Composite)
	}
}
