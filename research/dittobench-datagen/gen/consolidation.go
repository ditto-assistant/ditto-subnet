package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 6 longitudinal passive learning / consolidation (plan 4.4,
// datagen-realizable subset). Production differentiates on the chat -> async KG-sync
// -> subject-promotion loop: facts stated conversationally across sessions are
// captured passively (no save instruction) and later retrieved as one consolidated
// thread. The full live-multi-turn form with a synchronous sync barrier is a
// cross-repo protocol change (datagen turn-sequence type + api runner + reference
// harness); per the plan it should first ship as an observational, judge-free probe.
// This is that probe: one evolving TOPIC accrues several attributes across
// NON-ADJACENT sessions with no save verb, and the query asks for the EARLIEST
// attribute — a recency-biased or single-session reader returns the latest value
// (a seeded distractor), while genuine passive capture + long-horizon consolidation
// retrieves the early one. A harness that only persists on a hardcoded save-cue list
// never captured any of it and fails.
//
// Deterministic per (seed, draw position); v6-gated so v5's bytes are untouched.

const QTConsolidation = "passive-consolidation"

type consolidationSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func consolidationEnabled(benchVersion int) bool { return benchVersion >= protocol.BenchVersionV6 }

// consolidationCasesFor is the seed-independent case quota. Each case spreads a
// topic's attributes over three sessions, so it needs multiple waves.
func consolidationCasesFor(n, nWaves int) int {
	switch {
	case nWaves < 3:
		return 0
	case n >= 100:
		return 5
	case n >= 70:
		return 2
	case n >= 40:
		return 1
	default:
		return 0
	}
}

// consTopic is an evolving topic whose attributes are revealed across sessions. The
// three attrs are stated (passively, no save verb) in three different sessions; the
// question asks for the FIRST attribute stated.
type consTopic struct {
	topic string   // "move", "reunion", ...
	attrs []string // three attribute labels, in the order they are stated
	// state[i] fmt: (value) — how attribute i is mentioned; askFirst asks attr 0.
	state    []string
	askFirst string // fmt: (none)
}

var consTopics = []consTopic{
	{topic: "move",
		attrs: []string{"the city", "the street", "the move-in date"},
		state: []string{
			"We finally settled on moving to %s — still feels unreal.",
			"Turns out our new place is on %s.",
			"Move-in is set for %s now.",
		},
		askFirst: "Which city did I first say we were moving to?"},
	{topic: "reunion",
		attrs: []string{"the host", "the venue", "the headcount"},
		state: []string{
			"%s offered to organize the family reunion.",
			"We booked %s for the reunion.",
			"Looks like %s people are coming to the reunion.",
		},
		askFirst: "Who did I first say was organizing the reunion?"},
	{topic: "side project",
		attrs: []string{"the codename", "the stack", "the launch target"},
		state: []string{
			"I started a side project — calling it %s.",
			"Building it on %s.",
			"Aiming to launch it by %s.",
		},
		askFirst: "What codename did I first give my side project?"},
	{topic: "puppy",
		attrs: []string{"the name", "the breed", "the vet"},
		state: []string{
			"We named the puppy %s.",
			"She's a %s, by the way.",
			"Her vet is %s now.",
		},
		askFirst: "What did I first say we named the puppy?"},
}

// buildConsolidation generates the v6 passive-consolidation suite.
func buildConsolidation(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) consolidationSuite {
	var out consolidationSuite
	nCases := consolidationCasesFor(n, nWaves)
	if nCases == 0 {
		return out
	}
	pairIdx := 0
	ordinal := 0
	seedPair := func(session int, prompt, response string) {
		ts := persona.TimeAnchor(plan).Add(time.Duration(6+session)*time.Hour + time.Duration(pairIdx*beatSpacingMinutes)*time.Minute)
		out.Pairs = append(out.Pairs, protocol.MemoryPair{
			PairID:    fmt.Sprintf("p-cons-%d", pairIdx),
			SessionID: fmt.Sprintf("sess-cons-%d", session),
			Timestamp: ts.Format(time.RFC3339),
			Prompt:    prompt,
			Response:  response,
		})
		pairIdx++
	}

	topicPerm := r.Perm(len(consTopics))
	for c := 0; c < nCases; c++ {
		topic := consTopics[topicPerm[c%len(topicPerm)]]
		// Three coined attribute values, stated in three DIFFERENT (increasing)
		// sessions so the topic is consolidated across the timeline. The answer is the
		// FIRST value; the later two are distractors (a recency reader returns them).
		vals := make([]string, 3)
		for i := range vals {
			vals[i] = persona.CoinShaped(seed, fmt.Sprintf("cons|val|%d|%d", c, i))
		}
		for i, tmpl := range topic.state {
			// Sessions 0, 2, 4 (non-adjacent) so the thread spans the timeline.
			seedPair(2*i, fmt.Sprintf(tmpl, vals[i]), "Thanks for the update.")
		}
		out.Cases = append(out.Cases, StagedCase{Case: protocol.MemoryCase{
			ID:                protocol.OpaqueCaseID(seed, "memcons", ordinal),
			QuestionType:      QTConsolidation,
			Question:          topic.askFirst,
			ExpectedAnswer:    vals[0],
			AnswerKind:        protocol.AnswerValue,
			DistractorAnswers: []string{vals[1], vals[2]}, // the later (recency) values
		}, RunAfterWave: nWaves - 1})
		ordinal++
	}
	return out
}
