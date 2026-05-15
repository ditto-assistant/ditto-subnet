// Package bittensor defines the over-the-wire types exchanged between
// DittoBench miners and validators on the Bittensor subnet.
//
// The protocol is intentionally JSON-only and human-readable so miners can
// reimplement it in any language and validators can replay disputed
// challenges deterministically. Every payload is versioned via SchemaVersion
// so future format changes can ship without breaking old miners.
//
// Two on-chain mechanisms are defined:
//
//   - Mechanism 0 (DittoCore): tool-calling recall and accuracy.
//   - Mechanism 1 (DittoRetrieval): memory retrieval and grounded answer quality.
//
// Each request belongs to exactly one mechanism. A miner that does not
// implement a mechanism may return a non-fatal Refusal response; the validator
// will assign zero score for that case without penalising the miner's other
// mechanism.
package bittensor

import "time"

// SchemaVersion is the current protocol version. Increment on any
// backward-incompatible change to the wire types. The Python source of
// truth mirrors this constant in ditto/bench/__init__.py.
const SchemaVersion = "dittobench/1"

// Mechanism enumerates the on-chain incentive mechanisms supported by the
// DittoBench subnet.
type Mechanism string

const (
	// MechanismCore (Mechanism 0): tool-calling recall and accuracy.
	MechanismCore Mechanism = "ditto_core"
	// MechanismRetrieval (Mechanism 1): memory retrieval and grounded answer
	// quality.
	MechanismRetrieval Mechanism = "ditto_retrieval"
)

// CategoryMCPParity is the retrieval-case category that triggers the MCP
// parity gate in the scorer. Its string value mirrors the on-disk fixture
// taxonomy in ditto/bench/loader/taxonomy.py (RetrievalCategory.MCP_PARITY).
const CategoryMCPParity = "mcp_parity"

// ChallengeRequest is the JSON payload validators send miners for each case.
//
// Visibility encodes the dataset split this challenge was drawn from:
// "public" cases are in the open repo; "private" cases live in the
// validator-only manifest and are rotated; "canary" cases are hidden cases
// used to detect benchmark memorisation. Miners never see the Visibility
// value at submission time — it is stamped by the validator after scoring.
type ChallengeRequest struct {
	SchemaVersion string    `json:"schema_version"`
	ChallengeID   string    `json:"challenge_id"` // validator-chosen opaque ID; not the fixture ID
	Mechanism     Mechanism `json:"mechanism"`
	CaseID        string    `json:"case_id"`  // fixture ID; may differ for paraphrased cases
	Category      string    `json:"category"` // from ToolCallCase / RetrievalCase fixtures
	Domain        string    `json:"domain,omitempty"`
	Visibility    string    `json:"-"` // never serialised; stamped post-scoring

	// Core-mechanism fields.
	Prompt      string        `json:"prompt,omitempty"`
	ToolSchemas []ToolSchema  `json:"tool_schemas,omitempty"`
	STMContext  []ChatMessage `json:"stm_context,omitempty"`

	// Retrieval-mechanism fields.
	Query         string `json:"query,omitempty"`
	K             int    `json:"k,omitempty"`
	UserFixtureID string `json:"user_fixture_id,omitempty"`
	FixtureBundle string `json:"fixture_bundle,omitempty"` // URI / hash for the seeded corpus
	IncludeAnswer bool   `json:"include_answer,omitempty"` // miner should produce a final answer in addition to evidence IDs

	// Anti-gaming controls.
	ValidatorSeed string    `json:"validator_seed"` // per-challenge randomness; miners must echo in response
	IssuedAt      time.Time `json:"issued_at"`
	DeadlineMs    int64     `json:"deadline_ms,omitempty"` // miner must respond within this budget
}

// ChatMessage is a single STM context turn.
type ChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ToolSchema is a minimal OpenAI-style function schema. Validators send the
// canonical Ditto chat v2 tool schemas so miners cannot diverge on tool
// surface area.
type ToolSchema struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Parameters  map[string]any `json:"parameters"`
}

// MinerResponse is the JSON payload miners return per challenge.
type MinerResponse struct {
	SchemaVersion string `json:"schema_version"`
	ChallengeID   string `json:"challenge_id"`
	MinerHotkey   string `json:"miner_hotkey,omitempty"`
	ValidatorSeed string `json:"validator_seed"` // echoed back to detect dropped/replayed challenges

	// Tool-call trace for DittoCore challenges. Order matters; each call records
	// the hop (1-indexed tool-call batch). Arguments are emitted as a raw JSON
	// string exactly as the underlying LLM produced them; the validator parses.
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`

	// Evidence IDs for DittoRetrieval challenges, in ranked order (best first).
	EvidenceIDs []string `json:"evidence_ids,omitempty"`

	// Optional final answer (always emitted for IncludeAnswer challenges).
	FinalAnswer string `json:"final_answer,omitempty"`

	// Timing/cost metadata. These feed the latency component of both
	// mechanism composites and are also used to weed out miners that respond
	// suspiciously fast (cached/precomputed) or never (timed out).
	StartedAt        time.Time `json:"started_at"`
	FinishedAt       time.Time `json:"finished_at"`
	TotalLatencyMs   int64     `json:"total_latency_ms"`
	FirstTokenMs     int64     `json:"first_token_ms,omitempty"`
	PromptTokens     int64     `json:"prompt_tokens,omitempty"`
	OutputTokens     int64     `json:"output_tokens,omitempty"`
	EstimatedCostUSD float64   `json:"estimated_cost_usd,omitempty"`

	// Optional structured error / refusal signal. When set, the validator
	// scores the case as zero for this mechanism only.
	Refusal string `json:"refusal,omitempty"`
}

// ToolCall is one observed call inside a MinerResponse.ToolCalls trace.
type ToolCall struct {
	Hop  int    `json:"hop"`  // 1-indexed tool-call batch
	Name string `json:"name"` // tool name
	Args string `json:"args"` // raw JSON arguments from the LLM
}

// Score is the per-case score the validator computes after grading a
// MinerResponse. The mechanism-level weight applied to each Score is set by
// the validator and is independent of the per-case components published here
// (so subnet owners can re-weight components without re-running miners).
type Score struct {
	SchemaVersion string             `json:"schema_version"`
	ChallengeID   string             `json:"challenge_id"`
	Mechanism     Mechanism          `json:"mechanism"`
	CaseID        string             `json:"case_id"`
	Visibility    string             `json:"visibility"` // public | private | canary
	Category      string             `json:"category"`
	Domain        string             `json:"domain,omitempty"`
	Score         float64            `json:"score"` // 0..1 final case score
	Breakdown     map[string]float64 `json:"breakdown,omitempty"`

	// Forensic / debug fields. Validators publish these so other validators
	// can audit weights and miners can debug regressions.
	Notes    []string  `json:"notes,omitempty"`
	GradedAt time.Time `json:"graded_at"`
}

// AggregateWeights is the final per-miner weight vector a validator commits
// to the chain at the end of a tempo.
type AggregateWeights struct {
	SchemaVersion string             `json:"schema_version"`
	Mechanism     Mechanism          `json:"mechanism"`
	ValidatorHK   string             `json:"validator_hotkey"`
	WindowStart   time.Time          `json:"window_start"`
	WindowEnd     time.Time          `json:"window_end"`
	Weights       map[string]float64 `json:"weights"` // miner hotkey -> 0..1 weight
}
