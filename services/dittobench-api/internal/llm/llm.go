// Package llm holds the model identity the validator enforces. There are no
// LLM calls in the validator itself anymore: generation is non-LLM, scoring is
// the deterministic judge-free grader (dittobench-datagen/grade), and the only
// model in a run is the locked one the harness talks to through the gateway.
package llm

const (
	// LockedHarnessModel is the locked open-weight model every miner harness is
	// scored against, in its canonical harness-facing form. Locking the harness
	// to ONE model shrinks the exploit surface (no model routing to game) and is
	// consensus-critical: the median of the k=3 validators' scores only means
	// something if all three ran the same model. It is therefore a frozen code
	// constant, not env-tunable, so every validator scores against the same
	// model. Bump it here (a network-wide model change), then redeploy.
	LockedHarnessModel = "qwen/qwen3-32b"

	// V7HarnessModel is the consensus model for the v7 benchmark contract.
	// Historical versions remain pinned to LockedHarnessModel so replaying an
	// old ticket cannot silently cross the model boundary.
	V7HarnessModel = "openai/gpt-oss-20b"

	// LockedUpstreamModel is the same locked model in the form the Chutes
	// gateway serves it (TEE, hardware-attested). cmd/model-relay forces this id
	// upstream; LockedHarnessModel is what the harness and score reports name.
	LockedUpstreamModel = "Qwen/Qwen3-32B-TEE"

	// Relay profile revisions are part of the v5 efficiency baseline key. Any
	// serving-route or thinking-mode change must mint a new revision and a new
	// starter-kit baseline instead of comparing unlike contracts.
	ChutesRelayProfileRevision     = "chutes-qwen3-32b-tee-no-think-v1"
	OpenRouterRelayProfileRevision = "openrouter-nebius-qwen3-32b-no-think-v1"

	// V9AggregateProfileRevision is the reviewed aggregate OpenRouter route
	// contract where low/medium/high reasoning effort is agent-selected per
	// request and omission defaults to medium. It must never be confused with
	// the fixed-medium v7/v8 aggregate profile.
	V9AggregateProfileRevision = "openrouter-route-6a097486af3c178d-v1"
)

// HarnessModel returns the frozen locked model id every miner harness is scored
// against. It is the model-routing indirection: callers force this on the
// sandbox instead of letting the harness pick its own model. See
// LockedHarnessModel.
func HarnessModel() string { return LockedHarnessModel }

// HarnessModelForVersion returns the immutable model identity for a benchmark
// contract. The v7 boundary is intentionally explicit and not env-tunable.
func HarnessModelForVersion(benchVersion int) string {
	if benchVersion >= 7 {
		return V7HarnessModel
	}
	return LockedHarnessModel
}
