package gen

import (
	"math/rand"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// GenerateTools builds n fresh tool-calling cases from datagen's templated
// ground truth. Fully deterministic and LLM-free: each prompt is one of the
// category's hand-written phrasing variants, chosen by the seeded rng inside
// datagen (`cat.templates[r.Intn(len(...))]`), and expected_tools /
// expected_behavior are templated. The surface anti-memorization variation is
// the template-variant selection itself, so a given seed always yields the
// identical dataset. It returns a zero ParaphraseStats, a retained wire field.
func GenerateTools(r *rand.Rand, seed int64, n int) ([]protocol.ToolCase, protocol.ParaphraseStats) {
	// Pass the run's master seed (not a fresh draw): the case IDs and each
	// result-usage prompt's needle subject derive from it, and the mock endpoint
	// serves/scores the needle from the SAME seed (BuildFixture), so the entity a
	// prompt asks about is exactly the one the tool returns.
	cases, _ := datagen.GenerateCasesWithFillers(r, seed, n)
	return cases, protocol.ParaphraseStats{}
}
