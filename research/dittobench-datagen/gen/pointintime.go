package gen

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Bench v13 same-turn point-in-time twins (issue #1844).
//
// v8-v12 waves are empty and every case runs after wave 0, so the temporal
// staging the wave contract was built for was never exercised. Waves also
// cannot defeat ingest-time compilation on their own: a harness receives each
// wave as a /seed call and rebuilds its index before the next /run. The only
// correction a pre-compiled index cannot absorb is one that arrives INSIDE the
// /run turn. v13's primary point-in-time carrier is therefore the "as of
// <anchor>" context in the question itself (universe/v13_asof.go): the anchor
// exists in no seeded record, the before-half answer is the superseded value
// (the current value is its planted distractor), and the after-half asks the
// same chain on the other side of the correction. Both halves carry
// TwinRelationAsOf and one TwinPairID; a current-state index answers exactly
// one half, and the scorer's relation post-pass grades the pair.
//
// This is the port of persona's pointInTimeQuestions (v2-v7) onto the shared
// v8 world and universe.QuestionPlan: the persona chain became the world's
// original+correction records, the persona day offsets became the records'
// timestamps, and validatePlan proves the resolution. Every lever is gated on
// bench_version >= 13, so v12 and earlier regenerate byte-identically.

// QTPointInTime prefixes the v13 as-of question types ("point-in-time-contact",
// "point-in-time-invoice", "point-in-time-trip-leg"). Validator-internal; the
// harness only ever sees the opaque case id and the question.
const QTPointInTime = "point-in-time-"

// v13PointInTimeCaseCount is the fixed as-of case budget for a run size: two
// cases per as_of_twin pair, carved out of the world-question budget so the
// memory envelope is unchanged.
func v13PointInTimeCaseCount(n int) int {
	scale, _ := v8WorldProfile(n)
	people, projects, trips := universe.V13AsOfPairCounts(scale)
	return 2 * (people + projects + trips)
}

// buildV13PointInTime renders the as-of pairs of a v13 world into staged
// cases. Pairs are returned in [before, after] order per chain; the caller
// places them with placeV13TwinPairs so the two halves are never adjacent.
func buildV13PointInTime(seed int64, world universe.World, a universe.V13Allocation, benchVersion int) ([][2]StagedCase, error) {
	pairs, err := world.V13AsOfPairs(a)
	if err != nil {
		return nil, fmt.Errorf("v13 point-in-time: %w", err)
	}
	out := make([][2]StagedCase, 0, len(pairs))
	for k, pair := range pairs {
		pairID := protocol.OpaqueCaseID(seed, "v13-as-of-twin", k)
		questionType := QTPointInTime + pointInTimeKind(pair.Kind)
		var staged [2]StagedCase
		for half, plan := range []universe.QuestionPlan{pair.Before, pair.After} {
			plan.Case.BenchVersion = benchVersion
			plan.Case.QuestionType = questionType
			plan.Case.TwinRelation = protocol.TwinRelationAsOf
			plan.Case.TwinPairID = pairID
			plan.Case.WritingProtected = append(append([]string(nil), plan.Constraints...), plan.AsOfAnchor().Format("January 2, 2006"))
			staged[half] = StagedCase{Case: plan.Case, RunAfterWave: 0, RequiredPairIDs: append([]string(nil), plan.RequiredPairIDs...)}
		}
		out = append(out, staged)
	}
	return out, nil
}

func pointInTimeKind(oracleKind string) string {
	switch oracleKind {
	case "as-of-contact":
		return "contact"
	case "as-of-invoice":
		return "invoice"
	default:
		return "trip-leg"
	}
}
