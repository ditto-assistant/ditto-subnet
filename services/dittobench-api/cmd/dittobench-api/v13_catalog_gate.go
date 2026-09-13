package main

import (
	"math"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// v13CatalogGatePostureEnv selects the Bench v13 catalog-gate posture. It is
// read once at process start. Anything but an exact "enforce" is SHADOW: the
// relay records what the harness offered, the scorer records findings and the
// run-level catalog_suppression_rate, and no score moves. Enforce is an
// operator decision gated on the fleet precondition (completions_total non-nil
// on >= 99% of cases across >= 3 v13-capable validators); the per-validator
// half of that precondition is published in the report's catalog_gate summary.
const v13CatalogGatePostureEnv = "DITTOBENCH_V13_CATALOG_GATE_POSTURE"

var v13CatalogGatePosture = scorer.ParseCatalogGatePosture(os.Getenv(v13CatalogGatePostureEnv))

// applyV13CatalogGate scores one tool case's restraint and expected-tool credit
// against the relay's record of what the harness OFFERED the model. It runs
// after applyV10ToolProvenance and before result-usage composition, so a zero
// here flows into the case score the same way a provenance zero does. Below
// Bench v13 it returns cs untouched.
func applyV13CatalogGate(
	benchVersion int,
	scope scorer.Scope,
	posture scorer.CatalogGatePosture,
	cs protocol.CaseScore,
	c protocol.ToolCase,
	catalog []protocol.ToolDefinition,
	observed []protocol.ObservedToolCall,
	execution runner.CaseExecution,
) protocol.CaseScore {
	if benchVersion < scorer.CatalogGateBenchVersion {
		return cs
	}
	cs, _ = scorer.ApplyCatalogGateForVersion(
		benchVersion, scope, posture, cs, c, catalog, execution.Catalog, observed,
	)
	return cs
}

// summarizeV13CatalogGate aggregates the per-case catalog evidence into the
// run's catalog_gate summary, including the published
// catalog_suppression_rate (catalog absent / attributed cases with at least one
// completion) and the attribution coverage that is this validator's half of
// the enforce precondition. nil below Bench v13 so earlier report bytes are
// unchanged. totals may be nil when no v13 session exists.
func summarizeV13CatalogGate(
	benchVersion int,
	posture scorer.CatalogGatePosture,
	perCase []protocol.CaseScore,
	totals *sessionCatalogTotals,
) *protocol.CatalogGateSummary {
	if benchVersion < scorer.CatalogGateBenchVersion {
		return nil
	}
	summary := &protocol.CatalogGateSummary{Posture: string(posture)}
	withCompletion := 0
	for _, cs := range perCase {
		if cs.Kind != protocol.KindTool {
			continue
		}
		summary.ToolCases++
		evidence := cs.Catalog
		if evidence == nil || evidence.CompletionsTotal == nil || !evidence.Complete {
			continue
		}
		summary.AttributedCases++
		if *evidence.CompletionsTotal == 0 {
			summary.NoCompletionCases++
		} else {
			withCompletion++
			if !evidence.CatalogPresent {
				summary.CatalogAbsentCases++
			}
		}
		for _, finding := range evidence.Findings {
			switch finding {
			case scorer.CatalogFindingSafeHarbor:
				summary.SafeHarborCases++
			case scorer.CatalogFindingRestraintWithoutOffer:
				summary.RestraintWithoutOffer++
			case scorer.CatalogFindingExpectedToolNotOffered:
				summary.ExpectedToolNotOffered++
			case scorer.CatalogFindingSwallowedModelCall:
				summary.SwallowedModelCall++
			case scorer.CatalogFindingZeroed:
				summary.ZeroedCases++
			}
		}
	}
	if withCompletion > 0 {
		summary.CatalogSuppressionRate = math.Round(float64(summary.CatalogAbsentCases)/float64(withCompletion)*1e6) / 1e6
	}
	if summary.ToolCases > 0 {
		summary.AttributionCoverageBPS = summary.AttributedCases * 10000 / summary.ToolCases
	}
	if totals != nil {
		summary.CompletionsTotal = int(totals.Completions)
		summary.CompletionsUnattributed = int(totals.Unattributed)
	}
	return summary
}
