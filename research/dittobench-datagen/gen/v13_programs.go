package gen

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 open-program wiring (business side). The v13 memory envelope
// spends part of the fixed case budget on typed semantic business-event
// programs (universe/v13_contract.go) instead of the v10–v12 monetary
// programs. These helpers are the opt-in entry points the v13 suite assembly
// calls; nothing below v13 reaches them, so v2–v12 bytes are untouched.

// v13ProgramCaseCount is the bounded share of the v13 memory mix spent on
// business-event programs: seven metamorphic groups of four on the full
// profile (every family once), a multiple of four on every profile.
func v13ProgramCaseCount(n int) int {
	switch {
	case n >= 100:
		return 4 * len(universe.V13Families)
	case n >= 40:
		return 12
	default:
		return 4
	}
}

// V13BusinessProgramCases generates the v13 business-event programs for a
// seed and stages them for the memory suite: every member becomes a
// StagedCase carrying its validator-side provenance, and every evidence
// record joins the returned wave-0 pairs.
func V13BusinessProgramCases(seed int64, count int) ([]StagedCase, []protocol.MemoryPair, error) {
	generated, err := universe.GenerateV13Programs(seed, count)
	if err != nil {
		return nil, nil, fmt.Errorf("v13 business programs: %w", err)
	}
	cases, pairs := stageV13Programs(generated)
	return cases, pairs, nil
}

// stageV13Programs converts generated metamorphic cases into staged suite
// members. Mirrors the v10 loop in generateV8WorldMemorySuite so the envelope
// assembly has one shape to append.
func stageV13Programs(generated []universe.V10GeneratedCase) ([]StagedCase, []protocol.MemoryPair) {
	cases := make([]StagedCase, 0, len(generated))
	var pairs []protocol.MemoryPair
	for i := range generated {
		g := &generated[i]
		provenance := g.Provenance
		cases = append(cases, StagedCase{
			Case: g.Plan.Case, RunAfterWave: 0,
			RequiredPairIDs: append([]string(nil), g.Plan.RequiredPairIDs...),
			V10Provenance:   &provenance,
		})
		pairs = append(pairs, g.Pairs...)
	}
	return cases, pairs
}
