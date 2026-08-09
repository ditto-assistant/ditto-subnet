package main

import (
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const invalidV9IdentityCapabilityTool = "invalid_v9_identity_capability"

const invalidV9IdentityCapabilityNote = "invalid harness-authored v9 identity capability; case scored zero"

type projectedHarnessCase struct {
	Response protocol.RunResponse
	Observed []protocol.ObservedToolCall
	Invalid  bool
}

// projectHarnessCase reverses only harness-authored capability-bearing data.
// Unknown capabilities are miner output, not validator infrastructure: replace
// them with a deterministic sentinel so the case can be scored and replayed as
// zero without persisting an unrecognized opaque value or aborting the run.
// Validator-owned case and user IDs deliberately stay outside this helper and
// continue to fail the whole attempt if their private mapping is inconsistent.
func projectHarnessCase(
	projection *gen.HarnessProjection,
	response protocol.RunResponse,
	observed []protocol.ObservedToolCall,
) projectedHarnessCase {
	if projection == nil {
		return projectedHarnessCase{Response: response, Observed: observed}
	}
	projectedObserved, observedErr := projection.InternalizeCalls(observed)
	projectedCalls, reportedErr := projection.InternalizeCalls(response.ToolCalls)
	if observedErr != nil || reportedErr != nil {
		response.FinalText = ""
		response.Answer = ""
		response.Abstain = false
		response.ToolCalls = invalidV9CapabilityCalls()
		return projectedHarnessCase{
			Response: response,
			Observed: invalidV9CapabilityCalls(),
			Invalid:  true,
		}
	}
	response.ToolCalls = projectedCalls
	response.FinalText = projection.InternalizeText(response.FinalText)
	response.Answer = projection.InternalizeText(response.Answer)
	return projectedHarnessCase{Response: response, Observed: projectedObserved}
}

func invalidV9CapabilityCalls() []protocol.ObservedToolCall {
	return []protocol.ObservedToolCall{{Name: invalidV9IdentityCapabilityTool}}
}

func zeroInvalidV9CapabilityScore(score protocol.CaseScore, observed, injectionCompliance bool) protocol.CaseScore {
	score.Score = 0
	score.ToolScore = 0
	score.Quality = 0
	score.ResultUsage = 0
	score.Correct = false
	score.Called = []string{invalidV9IdentityCapabilityTool}
	score.Observed = observed
	// Invalid identity-bearing fields must never mask independent moderation
	// evidence detected from the raw response before it was sanitized.
	score.Injection = score.Injection || injectionCompliance
	score.Notes = append(score.Notes, invalidV9IdentityCapabilityNote)
	return score
}

// gradeProjectedMemoryCase keeps the score and transcript on the sanitized
// response while independently carrying forward the single moderation bit that
// an invalid identity capability must not be allowed to mask.
func gradeProjectedMemoryCase(
	mc protocol.MemoryCase,
	rawResponse protocol.RunResponse,
	projected projectedHarnessCase,
	observed bool,
) protocol.CaseScore {
	score := scorer.GradeMemory(mc, projected.Response)
	if projected.Invalid {
		score = zeroInvalidV9CapabilityScore(
			score,
			observed,
			scorer.MemoryInjectionCompliance(mc, rawResponse),
		)
	}
	return score
}

func injectionAttemptCount(scores []protocol.CaseScore) int {
	attempts := 0
	for _, score := range scores {
		if score.Injection {
			attempts++
		}
	}
	return attempts
}
