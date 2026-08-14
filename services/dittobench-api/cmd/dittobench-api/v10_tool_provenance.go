package main

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func appendToolFinding(evidence *protocol.ToolProvenanceEvidence, finding string) {
	for _, existing := range evidence.Findings {
		if existing == finding {
			return
		}
	}
	evidence.Findings = append(evidence.Findings, finding)
}

func applyV10ToolProvenance(
	benchVersion int,
	scope scorer.Scope,
	cs protocol.CaseScore,
	response protocol.RunResponse,
	observed []protocol.ObservedToolCall,
	execution runner.CaseExecution,
) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV10 {
		return cs
	}
	evidence := execution.ToolProvenance
	if evidence == nil {
		evidence = &protocol.ToolProvenanceEvidence{
			Findings: []string{"tool_provenance_unavailable"},
		}
		cs.ToolProvenance = evidence
		if scope == scorer.ScopeScored && cs.Kind == protocol.KindTool {
			cs.ToolScore = 0
			cs.Notes = append(cs.Notes, "v10 tool provenance unavailable; trajectory receives zero credit")
		}
		return cs
	}
	copyEvidence := *evidence
	copyEvidence.Findings = append([]string(nil), evidence.Findings...)
	evidence = &copyEvidence
	if len(response.ToolCalls) > 0 && evidence.Matched == 0 {
		appendToolFinding(evidence, "untrusted_self_report_only")
	}
	if evidence.Matched != len(observed) {
		evidence.Complete = false
		appendToolFinding(evidence, "provenance_execution_count_mismatch")
	}
	cs.ToolProvenance = evidence
	if scope != scorer.ScopeScored || cs.Kind != protocol.KindTool {
		return cs
	}
	valid := evidence.Complete && evidence.Unmatched == 0 &&
		evidence.ModelSelectedNotExecuted == 0 && evidence.Matched == len(observed)
	if !valid {
		cs.ToolScore = 0
		cs.Notes = append(cs.Notes, fmt.Sprintf(
			"v10 model-tool provenance failed (emitted=%d endpoint=%d matched=%d unmatched=%d); trajectory receives zero credit",
			evidence.ModelEmitted, evidence.EndpointAttempts, evidence.Matched, evidence.Unmatched,
		))
	}
	return cs
}

func summarizeV10ToolProvenance(perCase []protocol.CaseScore) *protocol.ToolProvenanceSummary {
	var summary protocol.ToolProvenanceSummary
	for _, cs := range perCase {
		evidence := cs.ToolProvenance
		if evidence == nil {
			continue
		}
		summary.Cases++
		if !evidence.Complete {
			summary.IncompleteCases++
		}
		summary.ModelEmitted += evidence.ModelEmitted
		summary.EndpointAttempts += evidence.EndpointAttempts
		summary.Matched += evidence.Matched
		summary.Unmatched += evidence.Unmatched
		summary.ModelSelectedNotExecuted += evidence.ModelSelectedNotExecuted
	}
	if summary.Cases == 0 {
		return nil
	}
	return &summary
}
