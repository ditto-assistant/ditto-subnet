package protocol

// PricedCostMinBenchVersion is the first bench version whose composite carries
// the priced inference cost factor (details.priced_cost). It is a floor: every
// later version inherits the rule unless a later contract replaces it.
const PricedCostMinBenchVersion = 16

// PricedCostBenchVersion reports whether bench version v prices its run.
func PricedCostBenchVersion(v int) bool { return v >= PricedCostMinBenchVersion }

// PricedCostSummary is the details.priced_cost block of a bench >= 16 score
// report, mirrored by ditto_screening_protocol.priced_cost.
//
// The run's inference is priced at the contract's locked public per-token
// rates (never a live provider bill), normalised to a NormalizationCases-case
// run, and mapped through the published piecewise-linear curve to FactorBPS.
// Every input is here, so any reader recomputes the factor from this block and
// the published constants alone. AppliedFactorBPS is what the composite
// actually carries (inside details.dueling.datagen_gate on a dueling run):
// FactorBPS under the enforce posture, 10000 under shadow or without complete
// evidence.
type PricedCostSummary struct {
	ContractID string `json:"contract_id"`
	Posture    string `json:"posture"`
	Applied    bool   `json:"applied"`
	// Result is "priced" or "insufficient_evidence" (incomplete chat
	// accounting, or no scored case); the latter always carries 10000.
	Result string `json:"result"`
	// Window is "whole_run": a dueling run prices its datagen half and its
	// generator slice together.
	Window string `json:"window"`
	// Source names where the chat counts came from: "relay" (the scored run's
	// whole-run relay accounting) or "broker_session" (a practice run's broker
	// session counters).
	Source string `json:"source"`
	Cases  int    `json:"cases"`

	PromptTokens         uint64 `json:"prompt_tokens"`
	CompletionTokens     uint64 `json:"completion_tokens"`
	EmbeddingTokens      uint64 `json:"embedding_tokens"`
	DecisionCalls        uint64 `json:"decision_calls"`
	DecisionInputTokens  uint64 `json:"decision_input_tokens"`
	DecisionOutputTokens uint64 `json:"decision_output_tokens"`

	Prices PricedCostPrices `json:"prices"`

	PromptCostNanoUSD     uint64 `json:"prompt_cost_nano_usd"`
	CompletionCostNanoUSD uint64 `json:"completion_cost_nano_usd"`
	EmbeddingCostNanoUSD  uint64 `json:"embedding_cost_nano_usd"`
	DecisionCostNanoUSD   uint64 `json:"decision_cost_nano_usd"`
	CostNanoUSD           uint64 `json:"cost_nano_usd"`

	NormalizationCases    int    `json:"normalization_cases"`
	NormalizedCostNanoUSD uint64 `json:"normalized_cost_nano_usd"`
	ReferenceNanoUSD      uint64 `json:"reference_nano_usd"`
	RatioBPS              int    `json:"ratio_bps"`

	Curve            []PricedCostCurvePoint `json:"curve"`
	FloorBPS         int                    `json:"floor_bps"`
	FactorBPS        int                    `json:"factor_bps"`
	AppliedFactorBPS int                    `json:"applied_factor_bps"`
}

// PricedCostPrices are the locked public rates, in nano-USD per token.
type PricedCostPrices struct {
	ChatModel                   string `json:"chat_model"`
	PromptToken                 uint64 `json:"prompt_token"`
	CompletionToken             uint64 `json:"completion_token"`
	EmbeddingModel              string `json:"embedding_model"`
	EmbeddingToken              uint64 `json:"embedding_token"`
	DecisionModel               string `json:"decision_model"`
	DecisionInputToken          uint64 `json:"decision_input_token"`
	DecisionOutputToken         uint64 `json:"decision_output_token"`
	CompletionIncludesReasoning bool   `json:"completion_includes_reasoning"`
}

// PricedCostCurvePoint is one breakpoint of the published curve: at
// RatioBPS = normalized_cost / reference (in basis points) the factor is
// FactorBPS. Between points the factor is linear; below the first point it is
// the first point's factor and beyond the last it is the last (the floor).
type PricedCostCurvePoint struct {
	RatioBPS  int `json:"ratio_bps"`
	FactorBPS int `json:"factor_bps"`
}
