package gen

import (
	"strconv"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Package note (surface realization):
// the persona plan (the persona package) is pure ground truth; this file renders
// its session beats into natural user/assistant MemoryPairs from each beat's
// deterministic template surface. Generation is fully non-LLM: the anti-
// memorization surface variation comes from the plan's per-beat template
// variants (pickStr over phrasing sets) and the fresh per-submission seed, not a
// post-hoc paraphrase. The dataset is byte-reproducible from (seed,
// bench_version).

// beatSpacingMinutes is the fixed intra-session spacing between consecutive
// beats' timestamps (deterministic ordering within a session).
const beatSpacingMinutes = 7

// RenderHaystack realizes a persona plan into a haystack of MemoryPairs (one per
// beat) and returns the fact→pair evidence map so the question-derivation layer
// can locate (or, for abstention, withhold) a fact's evidence. Every beat is
// rendered from its deterministic template surface (the plan already varied that
// surface via seeded phrasing variants). Timestamps derive from each session's
// seed-set DayOffset anchored backward from protocol.DatasetEpoch, never the
// wall clock.
func RenderHaystack(plan *persona.Plan) ([]protocol.MemoryPair, map[string]string) {
	evidence := make(map[string]string)

	// Anchoring is shared with persona.TimeAnchor so any date a question
	// renders (point-in-time) agrees with these pair timestamps exactly.
	anchor := persona.TimeAnchor(plan)

	pairs := make([]protocol.MemoryPair, 0)
	for _, s := range plan.Sessions {
		sessionID := "sess-" + strconv.Itoa(s.Index)
		sessionStart := anchor.Add(time.Duration(s.DayOffset) * 24 * time.Hour)
		for bi, b := range s.Beats {
			ts := sessionStart.Add(time.Duration(bi*beatSpacingMinutes) * time.Minute)
			pairID := "p-" + strconv.Itoa(s.Index) + "-" + strconv.Itoa(bi)
			pairs = append(pairs, protocol.MemoryPair{
				PairID:    pairID,
				SessionID: sessionID,
				Timestamp: ts.Format(time.RFC3339),
				Prompt:    b.UserText,
				Response:  b.AsstText,
			})
			if b.Kind == persona.BeatFact {
				evidence[b.FactID] = pairID
			}
		}
	}
	return pairs, evidence
}
