package main

import (
	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 restraint provenance and group rule postures (issue #1846). Both
// ship in SHADOW: the evidence is recorded on the case notes and the verdict is
// annotated, but no score moves until the operator flips the posture to
// enforce after calibration on real cleared agents (see the bench-version-bump
// skill: review -> penalize -> enforce, never enforce first).
const (
	// v13RestraintProvenanceEnv selects off | shadow | enforce for the
	// symmetric-provenance rule (swallowed_model_call, restraint_without_offer).
	v13RestraintProvenanceEnv = "DITTOBENCH_V13_RESTRAINT_PROVENANCE"
	// v13RestraintGroupEnv selects off | shadow | enforce for the decision_twin
	// concordant-zero group rule.
	v13RestraintGroupEnv = "DITTOBENCH_V13_RESTRAINT_GROUP_RULE"
)

func v13RestraintProvenancePosture() scorer.V13Posture {
	return scorer.V13PostureFromEnv(v13RestraintProvenanceEnv)
}

func v13RestraintGroupPosture() scorer.V13Posture {
	return scorer.V13PostureFromEnv(v13RestraintGroupEnv)
}

// applyV13RestraintProvenance folds the broker's session-scoped provenance for
// this case into the restraint rule. Today the only per-case broker signal is
// the settled ToolProvenanceEvidence (model-emitted calls the harness never
// executed); the offered-catalog evidence (CatalogPresent) is wired by the
// relay catalog-capture change and stays nil — unknown, never a finding —
// until that lands. A nil evidence ledger is likewise unknown here: the v10
// provenance rule already fails the case closed on a missing ledger.
func applyV13RestraintProvenance(benchVersion int, c protocol.ToolCase, cs protocol.CaseScore, execution runner.CaseExecution) protocol.CaseScore {
	if benchVersion < protocol.BenchVersionV13 || c.Restraint == nil || execution.ToolProvenance == nil {
		return cs
	}
	evidence := scorer.V13RestraintEvidence{
		ModelSelectedNotExecuted: execution.ToolProvenance.ModelSelectedNotExecuted,
	}
	return scorer.ApplyV13RestraintProvenance(benchVersion, c, cs, evidence, v13RestraintProvenancePosture())
}
