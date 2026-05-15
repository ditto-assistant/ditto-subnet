package bittensor

import (
	"math"
	"time"
)

// ToolCallScore is the raw per-case tool-call metrics emitted by a scorer
// pipeline and consumed by ScoreCore.
//
// NameF1 is the multiset-level F1 between expected and observed tool names.
// ArgF1 is the F1 over the argument-matcher engine. TrajectoryPenalty is in
// [0,1] and captures extra hops / tool overuse. AbstainCorrect is true on
// no-tool cases when no tool was called.
//
// Mirrors ditto.bench.runner.scoring.ToolCallScore in the Python port.
type ToolCallScore struct {
	NamePrecision     float64  `json:"name_precision"`
	NameRecall        float64  `json:"name_recall"`
	NameF1            float64  `json:"name_f1"`
	ArgF1             float64  `json:"arg_f1"`
	ArgMatcherScore   float64  `json:"arg_matcher_score,omitempty"`
	TrajectoryPenalty float64  `json:"trajectory_penalty"`
	AbstainCorrect    bool     `json:"abstain_correct,omitempty"`
	Score             float64  `json:"score,omitempty"`
	Notes             []string `json:"notes,omitempty"`
}

// RetrievalScore is the raw per-case IR metrics emitted by a retrieval
// scorer pipeline and consumed by ScoreRetrieval.
//
// Mirrors ditto.bench.runner.scoring.RetrievalScore in the Python port.
type RetrievalScore struct {
	NDCG5             float64 `json:"ndcg_5"`
	NDCG10            float64 `json:"ndcg_10"`
	MRR               float64 `json:"mrr"`
	Recall5           float64 `json:"recall_5"`
	Recall10          float64 `json:"recall_10"`
	NeedleHit         bool    `json:"needle_hit"`
	AbstainCorrect    bool    `json:"abstain_correct"`
	ContradictionPass bool    `json:"contradiction_pass"`
	NumRelevant       int     `json:"num_relevant,omitempty"`
	NumReturned       int     `json:"num_returned,omitempty"`
	NumForbiddenHit   int     `json:"num_forbidden_hit,omitempty"`
}

// CoreScoreInputs is the data a validator collects per DittoCore challenge.
//
// The case shape is intentionally flattened to primitive fields so this
// package has no dependency on any fixture loader; the validator pipeline
// fills them in from whatever case struct it loaded.
//
// Visibility carries the partition bucket the case was drawn from
// ("public" | "private" | "canary"). It is stamped onto the resulting
// Score so downstream aggregators can split a miner's public-vs-canary
// mean for memorisation detection.
type CoreScoreInputs struct {
	CaseID           string
	Category         string
	Domain           string
	Visibility       string
	NumExpectedTools int

	Tool ToolCallScore

	LatencyMs       int64
	BudgetLatencyMs int64 // soft cap; latency_score decays beyond this
}

// RetrievalScoreInputs is the data a validator collects per DittoRetrieval
// challenge. JudgeScore is meaningful only when JudgePresent is true (i.e.
// the challenge asked for a grounded final answer and a judge model ran).
// Visibility, like in CoreScoreInputs, is the partition bucket the case
// was drawn from and is stamped onto the resulting Score.
type RetrievalScoreInputs struct {
	CaseID              string
	Category            string
	Visibility          string
	NumExpectedPairIDs  int
	NumForbiddenPairIDs int
	ExpectNoTools       bool

	Retrieval RetrievalScore

	JudgeScore     float64 // 0..1 grounded answer score
	JudgePresent   bool
	UsedTools      bool    // STM/LTM routing signal
	MCPParityScore float64 // 0..1 set-equivalence between chat and MCP responses

	LatencyMs       int64
	BudgetLatencyMs int64
}

// ScoreCore computes a 0..1 DittoCore case score from the per-case observations.
//
// Weights (also documented in ditto/bench/docs/scoring.md):
//
//	0.50 tool_selection_f1
//	0.25 arg_quality_f1
//	0.15 sequence_score (= 1 - trajectory_penalty)
//	0.10 latency_score
//
// No-tool ("abstain") cases collapse tool_selection_f1 to 1.0 when the miner
// correctly refused to call a tool and to 0.0 when any tool was invoked, so
// a single spurious tool call drops the case score to zero.
func ScoreCore(in CoreScoreInputs) Score {
	seqScore := 1.0 - in.Tool.TrajectoryPenalty
	if seqScore < 0 {
		seqScore = 0
	}
	latencyScore := latencyComponent(in.LatencyMs, in.BudgetLatencyMs)

	selection := in.Tool.NameF1
	if in.NumExpectedTools == 0 {
		if in.Tool.AbstainCorrect {
			selection = 1.0
		} else {
			selection = 0.0
		}
	}

	composite := 0.50*selection +
		0.25*in.Tool.ArgF1 +
		0.15*seqScore +
		0.10*latencyScore

	composite = clamp01(composite)

	return Score{
		SchemaVersion: SchemaVersion,
		CaseID:        in.CaseID,
		Category:      in.Category,
		Domain:        in.Domain,
		Visibility:    in.Visibility,
		Mechanism:     MechanismCore,
		Score:         composite,
		Breakdown: map[string]float64{
			"tool_selection_f1": selection,
			"arg_quality_f1":    in.Tool.ArgF1,
			"sequence_score":    seqScore,
			"latency_score":     latencyScore,
		},
		GradedAt: time.Now().UTC(),
	}
}

// ScoreRetrieval computes a 0..1 DittoRetrieval case score from per-case
// observations.
//
// Weights (also documented in ditto/bench/docs/scoring.md):
//
//	0.45 evidence_metrics (NDCG@5 + MRR + Recall@5 + NeedleHit)
//	0.25 grounded_answer (judge_score | exact_match)
//	0.15 abstain_contradiction
//	0.10 stm_ltm_routing
//	0.05 latency_score
//
// MCP parity is reported as a hard gate; failures below 0.9 emit an
// mcp_parity_below_gate note but do not directly reduce the composite
// (operators use this for dashboard surfacing).
func ScoreRetrieval(in RetrievalScoreInputs) Score {
	r := in.Retrieval
	evidence := 0.4*r.NDCG5 + 0.3*r.MRR + 0.2*r.Recall5
	if r.NeedleHit {
		evidence += 0.1
	}
	if evidence > 1 {
		evidence = 1
	}

	grounded := 0.0
	if in.JudgePresent {
		grounded = in.JudgeScore
	} else {
		grounded = evidence
	}

	abstainContradiction := 0.0
	if r.AbstainCorrect {
		abstainContradiction += 0.5
	}
	if r.ContradictionPass {
		abstainContradiction += 0.5
	}
	if in.NumForbiddenPairIDs == 0 && in.NumExpectedPairIDs > 0 {
		// Cases that aren't abstention/contradiction get full credit on this
		// component so they aren't doubly penalised by the evidence metrics.
		abstainContradiction = 1.0
	}

	stmLtm := 1.0
	if in.ExpectNoTools && in.UsedTools {
		stmLtm = 0.0
	}

	latencyScore := latencyComponent(in.LatencyMs, in.BudgetLatencyMs)

	composite := 0.45*evidence +
		0.25*grounded +
		0.15*abstainContradiction +
		0.10*stmLtm +
		0.05*latencyScore

	composite = clamp01(composite)

	var notes []string
	if in.Category == CategoryMCPParity && in.MCPParityScore < 0.9 && in.MCPParityScore > 0 {
		notes = append(notes, "mcp_parity_below_gate")
	}

	return Score{
		SchemaVersion: SchemaVersion,
		CaseID:        in.CaseID,
		Category:      in.Category,
		Visibility:    in.Visibility,
		Mechanism:     MechanismRetrieval,
		Score:         composite,
		Breakdown: map[string]float64{
			"evidence_metrics":      evidence,
			"grounded_answer":       grounded,
			"abstain_contradiction": abstainContradiction,
			"stm_ltm_routing":       stmLtm,
			"latency_score":         latencyScore,
			"mcp_parity":            in.MCPParityScore,
		},
		Notes:    notes,
		GradedAt: time.Now().UTC(),
	}
}

// latencyComponent returns 1.0 when latency is at or below the budget and
// decays linearly to 0 at 5x the budget. A zero or negative budget or a
// zero/negative latency disables the component (returns 1.0).
func latencyComponent(latencyMs, budgetMs int64) float64 {
	if budgetMs <= 0 || latencyMs <= 0 {
		return 1.0
	}
	if latencyMs <= budgetMs {
		return 1.0
	}
	excess := float64(latencyMs-budgetMs) / float64(budgetMs)
	score := 1.0 - excess/4.0
	if score < 0 {
		score = 0
	}
	return score
}

// clamp01 returns x clamped to [0,1]; NaN collapses to 0.
func clamp01(x float64) float64 {
	if math.IsNaN(x) {
		return 0
	}
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}
