package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/scoregates"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/grade"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v13ClaimProvenancePostureEnv selects the ONE posture shared by the Bench v13
// claim-span provenance gate and the causal answer_in_prompt gate (issues
// #1849, #1833). It is read once at process start. Anything but an exact
// "enforce" is SHADOW: the relay records, the scorer emits per-case evidence,
// notes, and the run summary, and no score moves. Enforce is an operator
// decision gated on the honest cohort showing zero false zeros; the
// attribution coverage that is this validator's half of that precondition is
// published in the report's claim_provenance summary.
const v13ClaimProvenancePostureEnv = "DITTOBENCH_V13_CLAIM_PROVENANCE_POSTURE"

var v13ClaimProvenancePosture = scoregates.ParseClaimProvenancePosture(os.Getenv(v13ClaimProvenancePostureEnv))

// v13RecordTokens builds the run-wide exemption set for the causal gate: the
// value tokens of every record the validator delivered to the harness through
// /seed (memory pairs, subjects, tool prerequisite pairs) across every wave and
// the isolation graph. A value present in any delivered record may legitimately
// be quoted into a prompt by a RAG harness, so it can never be answer_in_prompt.
// The union is deliberately run-wide rather than per case: a harness retrieves
// from its whole store, and a wider exemption can only reduce flags. nil below
// Bench v13 so no work is done for earlier contracts.
func v13RecordTokens(benchVersion int, waves []protocol.SeedRequest, toolCases []protocol.ToolCase) scoregates.TokenSet {
	if benchVersion < protocol.BenchVersionV13 {
		return nil
	}
	tokens := make(scoregates.TokenSet)
	addPairs := func(pairs []protocol.MemoryPair) {
		for _, pair := range pairs {
			tokens.AddText(scoregates.NormalizeSpan(pair.Prompt))
			tokens.AddText(scoregates.NormalizeSpan(pair.Response))
		}
	}
	for _, wave := range waves {
		addPairs(wave.Pairs)
		for _, subject := range wave.Subjects {
			tokens.AddText(scoregates.NormalizeSpan(subject.SubjectText))
			tokens.AddText(scoregates.NormalizeSpan(subject.DescriptionText))
		}
	}
	for _, c := range toolCases {
		addPairs(c.PrerequisitePairs)
	}
	return tokens
}

// v13ClaimProvenanceReader is the relay read the gate consumes; the broker
// implements it and tests substitute a fixture.
type v13ClaimProvenanceReader interface {
	sessionClaimSpanEvidence(id, caseID string) (claimSpanEvidence, bool)
}

// applyV13ClaimProvenance runs both v13 claim gates for one graded memory case:
// the served claim span the grader credited must be model-emitted
// (served_text_not_model_emitted otherwise) and must not have been authored by
// the harness into the prompt (answer_in_prompt otherwise). It runs after
// gradeProjectedMemoryCase and applyV10ToolProvenance, so a zero here flows
// into the case score the same way a provenance zero does. Under shadow it
// appends evidence and notes only; under enforce in scored scope a settled
// flagged case scores 0. Unavailable or incomplete relay evidence always fails
// OPEN. Below Bench v13 it returns cs untouched.
func applyV13ClaimProvenance(
	benchVersion int,
	scope scorer.Scope,
	posture scoregates.ClaimProvenancePosture,
	cs protocol.CaseScore,
	mc protocol.MemoryCase,
	graded protocol.RunResponse,
	validatorSystemPrompt string,
	records scoregates.TokenSet,
	reader v13ClaimProvenanceReader,
	sessionID string,
) (out protocol.CaseScore) {
	if benchVersion < protocol.BenchVersionV13 || cs.Kind != protocol.KindMemory {
		return cs
	}
	evidence := &protocol.ClaimProvenanceEvidence{Posture: string(posture)}
	var findings []string
	// Every return path below carries the evidence; the named result is what
	// the deferred attach writes to, after the returned score is settled.
	defer func() {
		evidence.Findings = scoregates.SortedFindings(findings)
		out.ClaimProvenance = evidence
	}()

	var ledger *scoregates.ClaimSpanLedger
	complete := false
	if reader != nil && sessionID != "" {
		if read, ok := reader.sessionClaimSpanEvidence(sessionID, mc.ID); ok {
			ledger, complete = read.Ledger, read.Complete
		}
	}
	if ledger != nil {
		evidence.ToolResults = ledger.ToolResults
		if complete {
			completions := ledger.Completions
			evidence.Completions = &completions
		}
		evidence.Complete = complete
	}

	// Nothing to check when nothing was credited: the grader's own zero stands.
	if cs.Score <= 0 {
		findings = append(findings, scoregates.FindingClaimNotApplicable)
		return cs
	}
	if ledger == nil {
		findings = append(findings, scoregates.FindingClaimProvenanceUnavailable)
		return cs
	}
	if !complete {
		findings = append(findings, scoregates.FindingClaimProvenanceIncomplete)
		return cs
	}
	// The grader names the span it credited and the forms it accepts for this
	// claim; re-grading the same sanitized response is pure and deterministic.
	// A v13 case is graded under the v13 policy, which populates Provenance.
	verdict := grade.Memory(mc, graded)
	if verdict.Provenance == nil || verdict.Score <= 0 {
		findings = append(findings, scoregates.FindingClaimNotApplicable)
		return cs
	}
	question := make(scoregates.TokenSet)
	question.AddText(scoregates.NormalizeSpan(mc.Question))
	question.AddText(scoregates.NormalizeSpan(validatorSystemPrompt))
	exemptions := []scoregates.TokenSet{question}
	if records != nil {
		exemptions = append(exemptions, records)
	}
	claim := scoregates.EvaluateClaim(verdict.Provenance.Span, verdict.Provenance.Alternatives, ledger, exemptions...)
	if !claim.Applicable {
		findings = append(findings, scoregates.FindingClaimNotApplicable)
		return cs
	}
	evidence.ClaimTokens = claim.ClaimTokens
	modelEmitted, answerInPrompt := claim.ModelEmitted, claim.AnswerInPrompt
	evidence.ModelEmitted, evidence.AnswerInPrompt = &modelEmitted, &answerInPrompt
	if !modelEmitted {
		findings = append(findings, scoregates.FindingServedTextNotModelEmitted)
		if ledger.Completions == 0 {
			findings = append(findings, scoregates.FindingNoModelCompletion)
		}
	}
	if answerInPrompt {
		findings = append(findings, scoregates.FindingAnswerInPrompt)
	}
	if modelEmitted && !answerInPrompt {
		return cs
	}
	flagged := strings.Join(scoregates.SortedFindings(findings), ", ")
	if posture == scoregates.ClaimProvenanceEnforce && scope == scorer.ScopeScored {
		findings = append(findings, scoregates.FindingClaimProvenanceZeroed)
		cs.Score = 0
		cs.Correct = false
		cs.Notes = append(cs.Notes, fmt.Sprintf("v13 claim provenance failed (%s); case receives zero credit", flagged))
		return cs
	}
	cs.Notes = append(cs.Notes, fmt.Sprintf("v13 claim provenance flagged (%s); shadow posture, score unchanged", flagged))
	return cs
}

// summarizeV13ClaimProvenance aggregates the per-case claim evidence into the
// run's claim_provenance summary, including the attribution coverage that is
// this validator's half of the enforce precondition. nil below Bench v13 so
// earlier report bytes are unchanged.
func summarizeV13ClaimProvenance(
	benchVersion int,
	posture scoregates.ClaimProvenancePosture,
	perCase []protocol.CaseScore,
) *protocol.ClaimProvenanceSummary {
	if benchVersion < protocol.BenchVersionV13 {
		return nil
	}
	summary := &protocol.ClaimProvenanceSummary{Posture: string(posture)}
	for _, cs := range perCase {
		if cs.Kind != protocol.KindMemory {
			continue
		}
		summary.MemoryCases++
		evidence := cs.ClaimProvenance
		if evidence == nil {
			continue
		}
		if evidence.Complete {
			summary.AttributedCases++
		}
		if evidence.ModelEmitted == nil {
			for _, finding := range evidence.Findings {
				if finding == scoregates.FindingClaimProvenanceIncomplete || finding == scoregates.FindingClaimProvenanceUnavailable {
					summary.UnsettledCases++
					break
				}
			}
			continue
		}
		summary.ApplicableCases++
		summary.SettledCases++
		for _, finding := range evidence.Findings {
			switch finding {
			case scoregates.FindingServedTextNotModelEmitted:
				summary.NotModelEmittedCases++
			case scoregates.FindingAnswerInPrompt:
				summary.AnswerInPromptCases++
			case scoregates.FindingNoModelCompletion:
				summary.NoModelCompletionCases++
			case scoregates.FindingClaimProvenanceZeroed:
				summary.ZeroedCases++
			}
		}
	}
	if summary.MemoryCases > 0 {
		summary.AttributionCoverageBPS = summary.AttributedCases * scoregates.BasisPointScale / summary.MemoryCases
	}
	return summary
}

// v13ClaimProvenanceGateInput folds the per-case evidence into the run-level
// scoregates input that AttachClaimProvenance signs. Unsettled cases are the
// credited memory cases whose relay evidence was unavailable or incomplete;
// attribution is complete only when there are none.
func v13ClaimProvenanceGateInput(posture scoregates.ClaimProvenancePosture, perCase []protocol.CaseScore) scoregates.ClaimProvenanceInput {
	in := scoregates.ClaimProvenanceInput{Posture: posture, TelemetryComplete: true}
	for _, cs := range perCase {
		if cs.Kind != protocol.KindMemory {
			continue
		}
		in.AdministeredCases++
		evidence := cs.ClaimProvenance
		if evidence == nil {
			continue
		}
		if evidence.ModelEmitted != nil {
			in.EligibleCases++
			if !*evidence.ModelEmitted {
				in.NotModelEmittedCases++
			}
			if evidence.AnswerInPrompt != nil && *evidence.AnswerInPrompt {
				in.AnswerInPromptCases++
			}
			for _, finding := range evidence.Findings {
				if finding == scoregates.FindingClaimProvenanceZeroed {
					in.ZeroedCases++
				}
			}
			continue
		}
		for _, finding := range evidence.Findings {
			if finding == scoregates.FindingClaimProvenanceIncomplete || finding == scoregates.FindingClaimProvenanceUnavailable {
				in.UnsettledCases++
				break
			}
		}
	}
	in.AttributionComplete = in.UnsettledCases == 0
	return in
}
