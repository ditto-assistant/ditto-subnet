package main

// Production counterfactualCaseRunner: it re-runs one scored case over the SAME
// long-lived harness through a Bench v12 LaneInference ablation lease. The lease
// installs the ablation responder on the broker session (inference_broker.go
// beginAblationCase, v12 branch) so the harness's model calls for this re-run are
// served the FULL-ABLATION completion (no usable content, AblatedChat), while
// embeddings stay on the live platform lane (the v12 counterfactual scope
// substitutes chat only). The caller grades the returned response against the
// case's expected answer to settle the correctness-based dependence verdict.
//
// This runner is exercised only at runtime against a real harness+platform; the
// deterministic slice/verdict logic it feeds is unit-tested with a modeled
// runner in v12_counterfactual_test.go.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"

	"github.com/ditto-assistant/dittobench-api/internal/ablation"
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type brokerCounterfactualRunner struct {
	server             *server
	inferenceSessionID string
	runID              string
	harnessURL         string
	benchVersion       int
}

// counterfactualNamespace derives a non-empty opaque namespace for the ablation
// lease. It is required by beginAblationCase but, unlike the v9 confirmation
// path, carries no user-graph isolation here (the re-run reuses the already
// seeded scored graph), so a per-case constant suffices.
func counterfactualNamespace(spec counterfactualCaseSpec) string {
	digest := sha256.Sum256([]byte("v12-counterfactual\x00" + spec.caseID + "\x00" + spec.userID))
	return "cf_" + hex.EncodeToString(digest[:16])
}

func (r *brokerCounterfactualRunner) CounterfactualResponse(ctx context.Context, spec counterfactualCaseSpec, responder *ablation.Responder) (protocol.RunResponse, bool) {
	if r == nil || r.server == nil || r.server.broker == nil || !spec.populated() || responder == nil {
		return protocol.RunResponse{}, false
	}
	lease, err := r.server.broker.beginAblationCase(r.inferenceSessionID, r.runID, ablation.RunRequest{
		Lane:                ablation.LaneInference,
		CaseID:              spec.caseID,
		OpaqueUserNamespace: counterfactualNamespace(spec),
		Responder:           responder,
	})
	if err != nil {
		return protocol.RunResponse{}, false
	}
	defer lease.Close()

	// Re-run the case over the same harness. The harness reaches the broker on its
	// launch-configured run-wide inference URL, where the freshly installed
	// LaneInference scope serves the FULL-ABLATION completion. No per-case inference
	// URL override is set, matching the clean run-wide attribution path. The full
	// response is returned so the caller can grade it against the expected answer.
	resp, _, runErr := runner.RunCaseWithTelemetry(ctx, r.harnessURL, spec.caseID, spec.prompt, spec.tools, runner.CaseOptions{
		ToolEndpoint: spec.toolEndpoint,
		UserID:       spec.userID,
		BenchVersion: r.benchVersion,
	})
	// Discriminate an administered-but-failed re-run from a relay/transport gap by
	// how many FULL-ABLATION completions the broker actually served the agent on
	// this responder's ledger. ChatApplied>0 means the ablation was administered
	// (the agent received the empty completions and still could not answer);
	// ChatApplied==0 means the counterfactual never reached the agent at all.
	served := responder.Snapshot().ChatApplied
	return settleAblatedRun(resp, runErr, served)
}

// settleAblatedRun converts one ablated case re-run into the (response, ok) the
// counterfactual settle logic grades. It is the administered-vs-transport
// discriminator that keeps a retry-on-empty genuine agent from being false-gated:
//
//   - runErr == nil: the re-run completed. Its answer (which may be empty because
//     the harness had no usable model output to reason from) is graded as-is; an
//     empty/unusable answer under full ablation grades incorrect -> DEPENDENT.
//   - runErr != nil AND the ablation WAS administered (servedCompletions>0, i.e.
//     the broker served at least one empty completion on this responder's ledger):
//     the agent received the ablated completion(s) and then drained its budget,
//     timed out, or otherwise produced no usable answer. It could not answer
//     without the model -> that IS model-dependence. Settle it as ablated-INCORRECT
//     (an empty response grades incorrect) with ok=true -> DEPENDENT. This is the
//     retry-on-empty case that previously fell through to pending and false-gated
//     the run to 0.
//   - runErr != nil AND the ablation was NOT administered (servedCompletions==0):
//     the broker never served a single ablated completion (broker unreachable,
//     session error) -- a genuine relay/transport gap. An honest agent must never
//     be penalized for a stack/relay gap, so leave the case PENDING (ok=false),
//     which keeps SliceAttributionComplete false and the gate fail-open.
func settleAblatedRun(resp protocol.RunResponse, runErr error, servedCompletions uint64) (protocol.RunResponse, bool) {
	if runErr == nil {
		return resp, true
	}
	if servedCompletions > 0 {
		// Administered then failed to answer -> dependent (empty grades incorrect).
		return protocol.RunResponse{}, true
	}
	// Nothing was administered -> genuine transport gap -> pending (fail open).
	return protocol.RunResponse{}, false
}

var _ counterfactualCaseRunner = (*brokerCounterfactualRunner)(nil)
