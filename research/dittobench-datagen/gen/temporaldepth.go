package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Temporal-depth beyond latest-value (bench_version 5; v5 plan section 4.6).
// knowledge-update and point-in-time test the LATEST value (or the value as of a
// date); a grounded champion aces both. Temporal-depth asks for the
// SECOND-most-recent among three or more coexisting values:
//
//	session 1: "My favorite color is teal."
//	session 2: "I've switched my favorite color to amber."
//	session 3: "My favorite color is now <coined-blue>."
//	question:  "What was my favorite color just BEFORE I changed it to <coined-blue>?"  -> amber
//
// The answer is present in the store but is neither the recency-top pair (that is
// the latest value) nor a lexical match for the query (which names the LATEST
// value). Grep and naive recency both fail; only ordered reasoning over the
// timeline succeeds. This extends the reversal/ordering cases one level deeper and
// is a strong parser AND champion discriminator: the latest value and the oldest
// value are both distractors, so surfacing either zeros.
const (
	// QTTemporalDepth asks for the Nth-from-latest value in an update chain, not
	// the current one.
	QTTemporalDepth = "temporal-depth"
)

type temporalDepthSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func temporalDepthEnabled(benchVersion int) bool { return benchVersion >= protocol.BenchVersionV5 }

// temporalDepthCasesFor is the seed-independent quota; a chain needs three seeded
// values before the question and benefits from spanning waves, so single-wave runs
// carry none.
func temporalDepthCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 100:
		return 3
	case n >= 70:
		return 2
	case n >= 40:
		return 1
	case n >= 20:
		return 1
	default:
		return 0
	}
}

// tdNouns are update-chain attributes. A wide pool so the attribute space is not
// enumerable into a fixed cue list; the values are COINED tokens (exact grading, no
// haystack collision).
var tdNouns = []string{
	"favorite color", "go-to coffee order", "workout of choice", "primary side project",
	"default ringtone", "screensaver photo", "commute route", "favorite lunch spot",
	"preferred note-taking app", "morning playlist", "desktop wallpaper", "weekend hobby",
	"favorite tea", "running shoe brand", "password manager", "main writing font",
	"favorite podcast", "go-to pizza topping", "bike lock spot", "meal-prep dish",
}

// tdChangeGrammar phrases each of the three chain updates; tdAskGrammar phrases the
// "value just before the latest" question. Grammar-varied so one noun yields many
// distinct surfaces (%s noun, %s value; the ask takes %s noun, %s latest-value).
var tdChangeGrammar = persona.Grammar{
	"v1":   {"My %s is %s.", "For the record, my %s is %s.", "My %s these days is %s.", "Right now my %s is %s."},
	"v2":   {"#lead# I've switched my %s to %s.", "#lead# I moved my %s to %s.", "#lead# I changed my %s to %s.", "#lead# my %s is %s now."},
	"v3":   {"#lead# my %s is now %s.", "#lead# I've swapped my %s to %s.", "#lead# my %s just became %s.", "#lead# updated my %s to %s."},
	"lead": {"Update:", "Heads up,", "Oh,", "FYI,", "Actually,", ""},
}

var tdAskGrammar = persona.Grammar{
	"root": {
		"#lead# what was my %s just before I changed it to %s?",
		"#lead# what was my %s right before it became %s?",
		"#lead# before I switched my %s to %s, what was it?",
		"#lead# what did I have as my %s immediately before %s?",
	},
	"lead": {"", "Quick one:", "Trying to remember —", "Hey,"},
}

// buildTemporalDepth generates the temporal-depth cases. Deterministic per (seed,
// draw position); v5-gated at a fixed point in GenerateMemorySuite.
func buildTemporalDepth(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) temporalDepthSuite {
	var out temporalDepthSuite
	nCases := temporalDepthCasesFor(n, nWaves)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	// Three values seeded across three distinct sessions with increasing day
	// offsets, so the timeline order is unambiguous from the timestamps.
	seedPair := func(session, dayOffset int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(dayOffset)*24*time.Hour + time.Duration(session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-td-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-td-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	for c := 0; c < nCases; c++ {
		noun := tdNouns[r.Intn(len(tdNouns))]
		v1 := persona.CoinShaped(seed, fmt.Sprintf("td|v1|%d", c))
		v2 := persona.CoinShaped(seed, fmt.Sprintf("td|v2|%d", c)) // the ANSWER (second-most-recent)
		v3 := persona.CoinShaped(seed, fmt.Sprintf("td|v3|%d", c)) // the latest (named in the question)

		seedPair(3*c, c, fmt.Sprintf(persona.Expand(r, tdChangeGrammar, "v1"), noun, v1), "Noted.")
		seedPair(3*c+1, c+2, fmt.Sprintf(persona.Expand(r, tdChangeGrammar, "v2"), noun, v2), "Got it.")
		seedPair(3*c+2, c+5, fmt.Sprintf(persona.Expand(r, tdChangeGrammar, "v3"), noun, v3), "Updated.")

		// Metamorphic-invariance twin: ask "the value before the latest" two ways,
		// sharing a TwinGroup, so a phrasing-brittle solver is penalized by
		// MetamorphicConsistencyFactor. The latest value (v3) and the oldest (v1) are
		// both wrong distractors: surfacing the current value (naive recency) or the
		// original (grep the earliest) zeros.
		twin := fmt.Sprintf("tdtwin-%d-%d", seed, c)
		q1 := fmt.Sprintf(persona.Expand(r, tdAskGrammar, "root"), noun, v3)
		var q2 string
		for i := 0; i < 6; i++ {
			q2 = fmt.Sprintf(persona.Expand(r, tdAskGrammar, "root"), noun, v3)
			if q2 != q1 {
				break
			}
		}
		for _, q := range []string{q1, q2} {
			out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
				ID:                protocol.OpaqueCaseID(seed, "memtd", ordinal),
				QuestionType:      QTTemporalDepth,
				Question:          q,
				ExpectedAnswer:    v2,
				AnswerKind:        protocol.AnswerValue,
				DistractorAnswers: []string{v3, v1},
				TwinGroup:         twin,
			}, RunAfterWave: nWaves - 1})
			ordinal++
		}
	}
	return out
}
