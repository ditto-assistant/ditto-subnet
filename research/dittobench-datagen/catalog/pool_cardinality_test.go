package catalog

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/poolcanary"
)

// TestPoolCardinalityCanary is the tool catalog's pool-cardinality canary
// (#492, item 4). See internal/poolcanary for the floor and the ratchet.
//
// The appearance inventory (inventory.go) feeds both the served
// discover_capabilities options and the v13 declarative preference answers.
// The decoy shapes (v13.go) feed the near-miss capability cases.
func TestPoolCardinalityCanary(t *testing.T) {
	poolcanary.Check(t, ".", poolcanary.Ledger{
		Scored: []poolcanary.Pool{
			{Name: "colourCorpus", Size: len(colourCorpus), Role: "answer (accent preference)"},
			{Name: "fontCorpus", Size: len(fontCorpus), Role: "answer (font preference)"},
			{Name: "accentNearMissPairs", Size: len(accentNearMissPairs), Role: "answer and distractor (near-miss accent)"},
			{Name: "fontNearMissPairs", Size: len(fontNearMissPairs), Role: "answer and distractor (near-miss font)"},
			{Name: "legacyAccents", Size: len(legacyAccents), Role: "distractor option (served inventory)"},
			{Name: "legacyFonts", Size: len(legacyFonts), Role: "distractor option (served inventory)"},
			{Name: "decoyShapes", Size: len(decoyShapes), Role: "question cue (near-miss capability family)"},
		},

		// Known-small scored pools when the canary landed (#492). Each value is
		// the exact current size, and the list only shrinks. These pools are
		// frozen into the v13/v14 known vectors: a fix ships with the next
		// bench_version.
		Allow: map[string]int{
			"accentNearMissPairs": 14, // one pair per seed
			"fontNearMissPairs":   14,
			"legacyAccents":       8, // the v8..v12 pools, deliberately kept as served options
			"legacyFonts":         8,
			"decoyShapes":         23, // one short of the floor
		},

		Exempt: map[string]string{
			"v13Schemas":      poolcanary.Keyed, // keyed by tool name
			"v13RetiredTools": poolcanary.Internal,
		},
	})
}
