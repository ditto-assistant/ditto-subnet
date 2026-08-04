package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// bench_version 7 deep write chains. The v2 lifecycle chains (gen/lifecycle.go)
// grade a SINGLE write step: save->read, update->read, delete->read. A harness
// with a shallow last-write-wins store passes them all. A deep chain is a
// save -> update -> update -> read (or save -> update -> delete -> read)
// sequence delivered as separate instructions across THREE different staging
// waves, so the harness must apply a series of mutations in order across /run
// calls and keep only the final state:
//
//   - the current-value read is answerable only with the LAST value of the
//     chain; a stale value (any earlier write) simply fails containment, and the
//     cross-chain coined values ride as distractors so a dump-recent-writes
//     shotgun zeroes itself (the lifecycle anti-shotgun pattern);
//   - the delete-chain read is a decline case whose distractors are BOTH of the
//     chain's earlier values: surfacing either proves a mutation was dropped or
//     the deletion never landed.
//
// Trust model mirrors gen/lifecycle.go: instructions are graded as echo
// acknowledgements; the unfakeable signal is the read, whose coined values
// appear in NO haystack wave. Deterministic per (seed, draw position) and
// v7-gated so v6's bytes and already-recorded scores are untouched.
const (
	// QTDeepChainWrite / QTDeepChainRead are the question_type strings for the
	// instruction and read halves. Neither contains "injection"/"isolation"/
	// "canary", so the grader's cross-cutting logic is unaffected.
	QTDeepChainWrite = "lifecycle-deep-write"
	QTDeepChainRead  = "lifecycle-deep-read"
	// QTDeepChainCrossref is a read that resolves an indirect REFERENCE to the
	// final value of an update chain: a later note says another noun "uses the
	// same code" as the chained noun, and the question asks that other noun. The
	// answer is only reachable by (a) tracking the chain to its final value and
	// (b) joining the reference to it — the production "reuse a code / linked
	// memory" shape (backend relatedMemories). Neither the chained value nor the
	// reference states the answer outright, so it cannot be echoed.
	QTDeepChainCrossref = "lifecycle-deep-crossref"
)

type deepChainSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

func deepChainEnabled(benchVersion int) bool {
	return benchVersion >= protocol.BenchVersionV7
}

// deepChainChainsFor is the seed-independent chain quota. A chain spreads its
// three instructions over waves 0, 1, and 2 and reads in the last wave, so it
// needs at least four waves. Only v7 profiles carry four or more waves, which
// is a second (structural) layer of the version gate.
func deepChainChainsFor(n, nWaves int) int {
	switch {
	case nWaves < 4:
		return 0
	case n >= 100:
		return 2 // update chain + delete chain
	case n >= 40:
		return 1 // update chain only
	default:
		return 0
	}
}

// Deep-chain attributes. Distinct from every lifecycle noun (lcSaveNoun and
// friends), the cross-user nouns, and every persona attribute, so no existing
// distractor pool or question moves.
const (
	dcUpdNoun = "wine fridge keypad code"
	dcDelNoun = "allotment shed code"
)

// dcSaveGrammar / dcUpdGrammar / dcDelGrammar phrase the chain instructions.
// The %s value fill stays outside the grammar (expand-then-fill), so the coined
// token is never rewritten. The delete never restates the stored value: the
// harness must locate the fact itself to remove it.
var dcSaveGrammar = persona.Grammar{
	"root": {
		"#lead# my %[1]s is %[2]s.#trail#",
		"#lead# the %[1]s is %[2]s.#trail#",
	},
	"lead": {
		"Keep this for me:",
		"Please hold onto this:",
		"For your records,",
		"Jot this down:",
	},
	"trail": {
		" I'll ask for it again.",
		" Don't lose it.",
		" Keep it handy.",
		"",
	},
}

var dcUpdGrammar = persona.Grammar{
	"root": {
		"#lead# my %[1]s #verb# %[2]s.#trail#",
		"#lead# the %[1]s #verb# %[2]s.#trail#",
	},
	"lead": {
		"Heads up,",
		"Small change:",
		"Update for you:",
		"Quick correction —",
	},
	"verb": {
		"is now",
		"changed to",
		"was just reset to",
		"has been updated to",
	},
	"trail": {
		" The previous one is dead.",
		" Replace what you had.",
		" Don't use the old one.",
		"",
	},
}

var dcDelGrammar = persona.Grammar{
	"root": {
		"#lead# my %s#trail#",
		"#lead# the %s I gave you#trail#",
	},
	"lead": {
		"Please forget",
		"Go ahead and erase",
		"I want you to remove",
		"Clear out",
	},
	"trail": {
		" from your records.",
		" entirely.",
		"; I don't want it stored anymore.",
		".",
	},
}

// buildDeepChain generates the v7 deep write chains. Deterministic per (seed,
// draw position): grammar expansion and phrasing picks draw from the shared
// suite rng at a fixed point in GenerateMemorySuite.
func buildDeepChain(r *rand.Rand, seed int64, plan *persona.Plan, n, nWaves int) deepChainSuite {
	var out deepChainSuite
	nChains := deepChainChainsFor(n, nWaves)
	if nChains == 0 {
		return out
	}
	readWave := nWaves - 1
	pick := func(opts []string) string { return opts[r.Intn(len(opts))] }
	coin := func(salt string) string { return persona.CoinShaped(seed, "dc|"+salt) }

	u1, u2, u3 := coin("u1"), coin("u2"), coin("u3")
	d1, d2 := coin("d1"), coin("d2")

	ordinal := 0
	addCase := func(mc protocol.MemoryCase, wave int) {
		mc.ID = protocol.OpaqueCaseID(seed, "memdc", ordinal)
		out.Cases = append(out.Cases, StagedCase{Case: mc, RunAfterWave: wave})
		ordinal++
	}
	instr := func(qid string, g persona.Grammar, noun, val string, wave int) {
		addCase(protocol.MemoryCase{
			QuestionID:     qid,
			QuestionType:   QTDeepChainWrite,
			Question:       fmt.Sprintf(persona.Expand(r, g, "root"), noun, val),
			ExpectedAnswer: val,
		}, wave)
	}

	// Chain U: save -> update -> update -> read. The read's distractors are the
	// OTHER chain's coined values (or a coined bait when it is the only chain),
	// never this chain's own superseded values: mentioning old state alongside
	// the current answer is correct (the knowledge-update grading convention),
	// while a stale ANSWER simply fails containment.
	instr("dc-upd-w0", dcSaveGrammar, dcUpdNoun, u1, 0)
	instr("dc-upd-w1", dcUpdGrammar, dcUpdNoun, u2, 1)
	instr("dc-upd-w2", dcUpdGrammar, dcUpdNoun, u3, 2)
	updReadDis := []string{coin("u-bait")}
	if nChains >= 2 {
		updReadDis = []string{d1, d2}
	}
	addCase(protocol.MemoryCase{
		QuestionID:   "dc-upd-r",
		QuestionType: QTDeepChainRead,
		Question: pick([]string{
			"What's my " + dcUpdNoun + "?",
			"What's the current " + dcUpdNoun + "?",
			"Remind me of my " + dcUpdNoun + " — the current one.",
		}),
		ExpectedAnswer:    u3,
		DistractorAnswers: updReadDis,
	}, readWave)

	if nChains >= 2 {
		// Chain D: save -> update -> delete -> read. The read is a decline case
		// whose distractors are BOTH earlier values of the chain plus the other
		// chain's first value: surfacing any of them means a mutation was dropped,
		// the deletion never landed, or recent writes were dumped.
		instr("dc-del-w0", dcSaveGrammar, dcDelNoun, d1, 0)
		instr("dc-del-w1", dcUpdGrammar, dcDelNoun, d2, 1)
		addCase(protocol.MemoryCase{
			QuestionID:     "dc-del-w2",
			QuestionType:   QTDeepChainWrite,
			Question:       fmt.Sprintf(persona.Expand(r, dcDelGrammar, "root"), dcDelNoun),
			ExpectedAnswer: dcDelNoun,
			// An instruction, not a question: graded as an acknowledgement (the v4
			// correction); whether the delete persisted is graded by dc-del-r.
			AnswerKind: protocol.AnswerAcknowledge,
		}, 2)
		addCase(protocol.MemoryCase{
			QuestionID:   "dc-del-r",
			QuestionType: QTDeepChainRead,
			Question: pick([]string{
				"What's my " + dcDelNoun + "?",
				"Do you still have my " + dcDelNoun + "?",
				"Remind me of my " + dcDelNoun + ".",
			}),
			ExpectedAnswer:    abstentionExpectedAnswer,
			AnswerKind:        protocol.AnswerDecline,
			DistractorAnswers: []string{d1, d2, u1},
		}, readWave)
	}

	// Cross-referencing read (v7 round-2 deepening): a standing note says the
	// user reuses the update chain's code for another thing, WITHOUT restating the
	// value; the read asks that other thing and must resolve to the chain's final
	// value u3. Distractors are the chain's stale values (u1, u2), so a harness
	// that resolves the reference but tracked the wrong chain state is zeroed. The
	// note is seeded as a wave-0 raw pair (it names no value, so it is not a
	// spoiler); u3 exists only in the wave-2 instruction, so the join is unfakeable.
	crossNoun := "wine-cellar tablet PIN"
	out.Pairs = append(out.Pairs, protocol.MemoryPair{
		PairID:    "p-dc-xref",
		SessionID: "sess-dc",
		Timestamp: persona.TimeAnchor(plan).Add(4 * time.Hour).Format(time.RFC3339),
		Prompt:    "For my " + crossNoun + " I just reuse the same code as my " + dcUpdNoun + ".",
		Response:  "Understood — same code for both.",
	})
	crossDis := []string{u1, u2}
	if nChains >= 2 {
		crossDis = append(crossDis, d1)
	}
	addCase(protocol.MemoryCase{
		QuestionID:   "dc-xref-r",
		QuestionType: QTDeepChainCrossref,
		Question: pick([]string{
			"What's my " + crossNoun + "?",
			"What code opens my " + crossNoun + "?",
			"Remind me of my " + crossNoun + ".",
		}),
		ExpectedAnswer:    u3,
		AnswerKind:        protocol.AnswerValue,
		DistractorAnswers: crossDis,
	}, readWave)
	return out
}
