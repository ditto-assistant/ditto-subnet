// Package retrieval is the DittoRetrieval (Mechanism 1) handler for the
// reference harness. Implementations own their own memory store, embedding
// model, and retriever. The fixture user identified by
// ChallengeRequest.UserFixtureID has a validator-provided seeded corpus
// mounted into the container at the path named by the DITTO_FIXTURES_PATH
// env var (default /fixtures).
package retrieval

import (
	"context"
	"os"
	"sync"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// StubHandler is the default Mechanism 1 handler. It returns a refusal for
// every case so the binary compiles and runs end-to-end before a real
// implementation lands. Replace Handle with your own memory-stack retrieval
// pipeline to compete.
type StubHandler struct {
	once         sync.Once
	fixturesPath string
}

// NewStubHandler constructs the default no-op handler.
func NewStubHandler() *StubHandler { return &StubHandler{} }

// Handle returns a refusal for every DittoRetrieval challenge.
//
// To implement DittoRetrieval:
//
//  1. On the first call, lazily load the seeded fixture manifest from
//     DITTO_FIXTURES_PATH (default /fixtures). Index it however your
//     retriever prefers (vector, BM25, hybrid).
//  2. For each request, embed req.Query against the seeded corpus for
//     req.UserFixtureID and return up to req.K evidence IDs in ranked
//     order (best first) in MinerResponse.EvidenceIDs.
//  3. When req.IncludeAnswer is true, also produce MinerResponse.FinalAnswer
//     grounded in the retrieved pairs.
//  4. For STM-only cases, return an empty EvidenceIDs and answer from
//     req.STMContext; UsedTools will be inferred from EvidenceIDs being empty.
func (h *StubHandler) Handle(ctx context.Context, req bittensor.ChallengeRequest) (bittensor.MinerResponse, error) {
	h.once.Do(h.loadFixtures)
	return bittensor.MinerResponse{Refusal: "mechanism_unsupported"}, nil
}

// loadFixtures records the seeded-fixture mount path for later use. The
// stub reads the env var so a real implementation only needs to swap in the
// indexing logic without rewriting bootstrap.
func (h *StubHandler) loadFixtures() {
	h.fixturesPath = os.Getenv("DITTO_FIXTURES_PATH")
	if h.fixturesPath == "" {
		h.fixturesPath = "/fixtures"
	}
}
