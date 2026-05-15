// Package core is the DittoCore (Mechanism 0) handler for the reference
// harness. Implementations own their own LLM client, tool dispatcher, and
// any internal caching. The validator does NOT execute the Ditto tool
// surface for the miner; the harness must run or simulate tool calls and
// return the observed trace in MinerResponse.ToolCalls.
package core

import (
	"context"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// StubHandler is the default Mechanism 0 handler shipped with the template.
// It returns a structured refusal for every case so the binary compiles and
// runs end-to-end before a real implementation lands. Replace Handle with a
// real LLM-driven tool-routing loop to compete.
type StubHandler struct{}

// NewStubHandler constructs the default no-op handler.
func NewStubHandler() *StubHandler { return &StubHandler{} }

// Handle returns a refusal for every DittoCore challenge. A miner that
// returns refusal is scored as zero for the case at hand but is not
// penalised on the DittoRetrieval mechanism.
//
// To implement DittoCore:
//
//  1. Maintain an OpenAI-compatible chat client (DITTO_LLM_ENDPOINT /
//     DITTO_LLM_API_KEY in the environment).
//  2. Send req.Prompt + req.STMContext to the model along with req.ToolSchemas.
//  3. Capture the tool-call sequence the model emits and return it in
//     MinerResponse.ToolCalls (hop is 1-indexed batch order).
//  4. For ditto_core, MinerResponse.EvidenceIDs and MinerResponse.FinalAnswer
//     are optional; tool_calls is the primary scored signal.
func (h *StubHandler) Handle(ctx context.Context, req bittensor.ChallengeRequest) (bittensor.MinerResponse, error) {
	return bittensor.MinerResponse{Refusal: "mechanism_unsupported"}, nil
}
