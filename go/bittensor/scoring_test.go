package bittensor

import "testing"

func TestScoreCore_PerfectCase(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "x",
		Category:         "memory_lookup",
		Domain:           "personal_recall_routing",
		NumExpectedTools: 1,
		Tool:             ToolCallScore{NameF1: 1.0, ArgF1: 1.0, TrajectoryPenalty: 0},
		LatencyMs:        100,
		BudgetLatencyMs:  1000,
	}
	s := ScoreCore(in)
	if s.Score < 0.99 {
		t.Fatalf("expected near-perfect core score, got %v (breakdown=%v)", s.Score, s.Breakdown)
	}
	if s.Mechanism != MechanismCore {
		t.Fatalf("expected MechanismCore, got %v", s.Mechanism)
	}
}

func TestScoreCore_AbstainCorrect(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "abstain",
		Category:         "no_tool",
		Domain:           "tool_use_abstention",
		NumExpectedTools: 0,
		Tool:             ToolCallScore{NameF1: 0, ArgF1: 1.0, TrajectoryPenalty: 0, AbstainCorrect: true},
		LatencyMs:        50,
		BudgetLatencyMs:  1000,
	}
	s := ScoreCore(in)
	if s.Breakdown["tool_selection_f1"] != 1.0 {
		t.Fatalf("expected abstain to yield selection=1, got %v", s.Breakdown)
	}
	if s.Score < 0.99 {
		t.Fatalf("expected near-perfect score for correct abstain, got %v", s.Score)
	}
}

func TestScoreCore_AbstainViolated(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "x",
		NumExpectedTools: 0,
		Tool:             ToolCallScore{NameF1: 0, ArgF1: 1.0, TrajectoryPenalty: 0.5, AbstainCorrect: false},
		LatencyMs:        50,
		BudgetLatencyMs:  1000,
	}
	s := ScoreCore(in)
	if s.Breakdown["tool_selection_f1"] != 0 {
		t.Fatalf("expected abstain violation to zero selection, got %v", s.Breakdown)
	}
}

func TestScoreCore_BreakdownKeys(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "x",
		NumExpectedTools: 1,
		Tool:             ToolCallScore{NameF1: 0.5, ArgF1: 0.5, TrajectoryPenalty: 0.1},
		LatencyMs:        500,
		BudgetLatencyMs:  1000,
	}
	s := ScoreCore(in)
	want := []string{"tool_selection_f1", "arg_quality_f1", "sequence_score", "latency_score"}
	for _, k := range want {
		if _, ok := s.Breakdown[k]; !ok {
			t.Fatalf("missing breakdown key %q in %v", k, s.Breakdown)
		}
	}
	if len(s.Breakdown) != len(want) {
		t.Fatalf("unexpected breakdown keys: %v", s.Breakdown)
	}
}

func TestScoreCore_ClampedToUnitInterval(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "x",
		NumExpectedTools: 1,
		Tool:             ToolCallScore{NameF1: 2.0, ArgF1: 2.0, TrajectoryPenalty: -1.0},
		LatencyMs:        10,
		BudgetLatencyMs:  1000,
	}
	s := ScoreCore(in)
	if s.Score < 0 || s.Score > 1 {
		t.Fatalf("expected composite within [0,1], got %v", s.Score)
	}
}

func TestScoreRetrieval_AbstainCorrect(t *testing.T) {
	in := RetrievalScoreInputs{
		CaseID:    "x",
		Category:  "stale_outside_window",
		Retrieval: RetrievalScore{AbstainCorrect: true, ContradictionPass: true},
		LatencyMs: 10, BudgetLatencyMs: 1000,
	}
	s := ScoreRetrieval(in)
	if s.Breakdown["abstain_contradiction"] != 1.0 {
		t.Fatalf("expected abstain_contradiction=1, got %v", s.Breakdown)
	}
}

func TestScoreRetrieval_STMRoutingViolation(t *testing.T) {
	in := RetrievalScoreInputs{
		CaseID:        "stm",
		Category:      "stm_only",
		ExpectNoTools: true,
		Retrieval:     RetrievalScore{ContradictionPass: true},
		UsedTools:     true,
		LatencyMs:     10, BudgetLatencyMs: 1000,
	}
	s := ScoreRetrieval(in)
	if s.Breakdown["stm_ltm_routing"] != 0 {
		t.Fatalf("expected stm_ltm_routing=0 when tools used on STM-only case, got %v", s.Breakdown)
	}
}

func TestScoreRetrieval_ContradictionPasses(t *testing.T) {
	in := RetrievalScoreInputs{
		CaseID:              "c",
		Category:            "contradiction_update",
		NumExpectedPairIDs:  1,
		NumForbiddenPairIDs: 1,
		Retrieval: RetrievalScore{
			NDCG5: 1.0, MRR: 1.0, Recall5: 1.0, NeedleHit: true, ContradictionPass: true,
		},
		LatencyMs: 10, BudgetLatencyMs: 1000,
	}
	s := ScoreRetrieval(in)
	if s.Score < 0.9 {
		t.Fatalf("expected high score on perfect contradiction case, got %v (%v)", s.Score, s.Breakdown)
	}
}

func TestScoreRetrieval_MCPParityBelowGateNote(t *testing.T) {
	in := RetrievalScoreInputs{
		CaseID:             "mcp",
		Category:           CategoryMCPParity,
		NumExpectedPairIDs: 1,
		Retrieval:          RetrievalScore{NDCG5: 1.0, MRR: 1.0, Recall5: 1.0, NeedleHit: true},
		MCPParityScore:     0.5,
		LatencyMs:          10, BudgetLatencyMs: 1000,
	}
	s := ScoreRetrieval(in)
	var sawNote bool
	for _, n := range s.Notes {
		if n == "mcp_parity_below_gate" {
			sawNote = true
		}
	}
	if !sawNote {
		t.Fatalf("expected mcp_parity_below_gate note, got %v", s.Notes)
	}
}

func TestScoreCore_StampsVisibility(t *testing.T) {
	in := CoreScoreInputs{
		CaseID:           "x",
		Visibility:       "canary",
		NumExpectedTools: 1,
		Tool:             ToolCallScore{NameF1: 1.0, ArgF1: 1.0},
		LatencyMs:        100, BudgetLatencyMs: 1000,
	}
	s := ScoreCore(in)
	if s.Visibility != "canary" {
		t.Fatalf("expected Visibility=canary, got %q", s.Visibility)
	}
}

func TestScoreRetrieval_StampsVisibility(t *testing.T) {
	in := RetrievalScoreInputs{
		CaseID:             "x",
		Visibility:         "private",
		NumExpectedPairIDs: 1,
		Retrieval:          RetrievalScore{NDCG5: 1.0, MRR: 1.0, Recall5: 1.0, NeedleHit: true},
		LatencyMs:          10, BudgetLatencyMs: 1000,
	}
	s := ScoreRetrieval(in)
	if s.Visibility != "private" {
		t.Fatalf("expected Visibility=private, got %q", s.Visibility)
	}
}

func TestLatencyComponent(t *testing.T) {
	if v := latencyComponent(100, 1000); v != 1.0 {
		t.Fatalf("latency below budget should yield 1.0, got %v", v)
	}
	if v := latencyComponent(5000, 1000); v != 0 {
		t.Fatalf("latency 5x budget should yield 0, got %v", v)
	}
	if v := latencyComponent(0, 1000); v != 1.0 {
		t.Fatalf("zero latency should yield 1.0, got %v", v)
	}
	if v := latencyComponent(2000, 1000); v <= 0.7 || v >= 0.8 {
		t.Fatalf("latency 2x budget should be ~0.75, got %v", v)
	}
}
