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
			{Name: "v16ConnectorScripts", Size: len(v16ConnectorScripts), Role: "question cue (v16 linked and named connector apps)"},
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
			"v13Schemas":             poolcanary.Keyed, // keyed by tool name
			"v13RetiredTools":        poolcanary.Internal,
			"v16DescriptionRewrites": poolcanary.Keyed,    // keyed by the v15 description it rewrites
			"V16LinkStatePhrases":    poolcanary.Internal, // contract-test denylist, never served
			// v18 production catalog: the bulk menu comes from the pinned
			// snapshot; these are its sampling filter and paraphrase banks. No
			// scored case expects a snapshot tool.
			"catalogSizes":            poolcanary.Internal, // per-contract menu size, never rendered
			"v18LinkedToolkitAliases": poolcanary.Keyed,    // keyed by v16 linkable app
			"v18Synonyms":             "meaning-preserving paraphrase swaps for the non-answer snapshot menu",
		},
	})
}
