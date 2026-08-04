package gen

import (
	"fmt"
	"math/rand"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Write-then-read lifecycle chains (prod hardening P1).
//
// Every other memory case grades the READ path: facts are seeded, questions
// are asked. A lifecycle chain grades the write path end to end: an
// instruction case (run after wave 0) tells the agent to save, update, or
// delete a fact through its own memory tools, and a read case in the LAST
// wave asks a question that is only answerable if the write actually landed
// in the harness's own store.
//
// Trust model: the instruction case is graded as an echo acknowledgment
// (value containment: a confirming assistant restates what it stored), NOT on
// self-reported tool_calls, which the benchmark treats as untrusted
// everywhere else. The unfakeable signal is the read case: the saved/updated
// value is coined per seed and appears in NO haystack wave, so answering the
// read correctly is only possible if the harness genuinely persisted it
// between /run calls. A harness that fakes the acknowledgment without storing
// earns the instruction point and forfeits the read point; one that stores
// earns both.
//
// Chain shapes:
//   - save:   instruction carries coined value V; read expects V.
//   - update: wave-0 haystack seeds old value A; instruction changes it to B;
//     read expects B. A is deliberately NOT a distractor (mentioning the old
//     code alongside the new one is correct, matching knowledge-update
//     grading); a read answer of A simply fails the containment check.
//   - delete: wave-0 haystack seeds value F; instruction orders its removal
//     WITHOUT restating F (the harness must locate it); read is a decline
//     case with F as a distractor, so surfacing F means the deletion never
//     happened and scores 0.
//
// Anti-shotgun: each read case carries the OTHER chains' coined values as
// distractors, so a harness that dumps every recently written token zeroes
// itself, mirroring the suite-wide distractor defense.
const (
	// QTLifecycleWrite / QTLifecycleRead are the question_type strings for the
	// two halves of a chain. Neither contains "injection"/"isolation"/"canary",
	// so the grader's cross-cutting note logic is unaffected.
	QTLifecycleWrite = "memory-write"
	QTLifecycleRead  = "memory-write-read"
)

// lifecycleChainsFor is the seed-independent chain quota: full-profile runs
// carry all three shapes, medium runs carry the save chain, and single-wave
// (small) runs carry none because a chain needs a later wave for its read
// case. Counts are fixed per run size so difficulty stays identical across
// seeds (the same stratification argument as the tool categories).
func lifecycleChainsFor(n, nWaves int) int {
	switch {
	case nWaves < 2:
		return 0
	case n >= 120:
		// v7: the update and delete lifecycle shapes are covered (deeper) by the
		// v7 deep write chains (gen/deepchain.go: save->update->update->read and
		// ->delete->read), so v7 keeps only the single save->read chain here to
		// avoid redundant, low-discrimination scaffolding cases. Only v7 profiles
		// reach n>=120, so v5/v6 (n<=85) keep all three shapes and their bytes.
		return 1
	case n >= 40:
		return 3
	case n >= 20:
		return 1
	default:
		return 0
	}
}

// lifecycleSuite is the generated chain material: staged cases (instruction
// at wave 0, read at the last wave) plus the wave-0 haystack pairs the
// update/delete chains rely on.
type lifecycleSuite struct {
	Cases []StagedCase
	Pairs []protocol.MemoryPair
}

// lifecycleToken coins a code-shaped token from (seed, salt): high-entropy,
// never a pool value or real word. It shares the run's coined-token shape
// family (persona.CoinShaped) with the canary nonce/bait and the injection
// payload — the v3 grammar-collision property. The pre-v3 rationale was the
// exact opposite ("visually distinct so a token-shape matcher can never
// confuse the families"), and that distinctness is what let an output scrubber
// delete payload-shaped tokens while exempting answer-shaped ones. Under the
// shared family, a shape-keyed scrub deletes these lifecycle read answers too.
func lifecycleToken(seed int64, salt string) string {
	return persona.CoinShaped(seed, "lc|"+salt)
}

// Lifecycle attributes. Code-shaped values make token grading exact, and the
// nouns are things a user plausibly asks an assistant to keep: none collide
// with a persona attribute, so distractorsFor pools and existing questions
// are untouched.
const (
	lcSaveNoun = "gym locker code"
	lcUpdNoun  = "storage unit access code"
	lcDelNoun  = "bike lock code"
)

// saveGrammar phrases the save instruction. The %s value fill stays outside
// the grammar (expand-then-fill), so the coined token is never rewritten.
var saveGrammar = persona.Grammar{
	"root": {
		"#lead# my " + lcSaveNoun + " is %s.#trail#",
		"#lead# the code for my gym locker is %s.#trail#",
	},
	"lead": {
		"Quick thing to keep for me:",
		"Please keep this on file:",
		"Something to hold onto for me:",
		"For your records,",
		"Jot this down for me:",
	},
	"trail": {
		" I'll need it again.",
		" Make sure you can tell me later.",
		" Keep it handy.",
		"",
	},
}

// updGrammar phrases the update instruction (the new value fills %s).
var updGrammar = persona.Grammar{
	"root": {
		"#lead# my " + lcUpdNoun + " #verb# %s.#trail#",
		"#lead# the access code for my storage unit #verb# %s.#trail#",
	},
	"lead": {
		"Heads up,",
		"Quick correction:",
		"Small change:",
		"Update for you:",
	},
	"verb": {
		"is now",
		"changed to",
		"was just changed to",
		"has been updated to",
	},
	"trail": {
		" The previous code is out of date.",
		" Replace the old one.",
		" Don't use the old one anymore.",
		"",
	},
}

// delGrammar phrases the delete instruction. It never restates the stored
// value: the harness must locate the fact itself to remove it.
var delGrammar = persona.Grammar{
	"root": {
		"#lead# my " + lcDelNoun + "#trail#",
		"#lead# the code I gave you for my bike lock#trail#",
	},
	"lead": {
		"Please forget",
		"Go ahead and erase",
		"I want you to remove",
		"Please clear out",
	},
	"trail": {
		" from your records.",
		" entirely.",
		"; I don't want it stored anymore.",
		".",
	},
}

// buildLifecycle generates nChains write-then-read chains. Deterministic per
// (seed, draw position): grammar expansion and phrasing picks draw from the
// shared suite rng at a fixed point in GenerateMemorySuite.
// acknowledgeKindFor returns the AnswerKind for a delete INSTRUCTION case: the
// acknowledgement grader from v4 on, and the historical value-containment
// default before it. Delete instructions have no value to return, so containment
// graded phrasing rather than whether the deletion happened -- but an AnswerKind
// is part of the dataset bytes, so older contracts keep what they were pinned
// with.
func acknowledgeKindFor(benchVersion int) string {
	if benchVersion >= protocol.BenchVersionV4 {
		return protocol.AnswerAcknowledge
	}
	return ""
}

func buildLifecycle(r *rand.Rand, seed int64, plan *persona.Plan, nChains, nWaves int) lifecycleSuite {
	var out lifecycleSuite
	if nChains <= 0 || nWaves < 2 {
		return out
	}
	readWave := nWaves - 1
	pick := func(opts []string) string { return opts[r.Intn(len(opts))] }

	saveVal := lifecycleToken(seed, "lc-save")
	updOld := lifecycleToken(seed, "lc-upd-old")
	updNew := lifecycleToken(seed, "lc-upd-new")
	delVal := lifecycleToken(seed, "lc-del")

	addCase := func(ordinal int, mc protocol.MemoryCase, wave int) {
		mc.ID = protocol.OpaqueCaseID(seed, "memlc", ordinal)
		out.Cases = append(out.Cases, StagedCase{Case: mc, RunAfterWave: wave})
	}

	// Chain 1: save. The value exists nowhere but the instruction text.
	saveInstr := fmt.Sprintf(persona.Expand(r, saveGrammar, "root"), saveVal)
	saveReadDis := []string{lifecycleToken(seed, "lc-save-bait")}
	if nChains >= 2 {
		saveReadDis = []string{updNew}
	}
	addCase(0, protocol.MemoryCase{
		QuestionID:     "lc-save-w",
		QuestionType:   QTLifecycleWrite,
		Question:       saveInstr,
		ExpectedAnswer: saveVal,
	}, 0)
	addCase(1, protocol.MemoryCase{
		QuestionID:   "lc-save-r",
		QuestionType: QTLifecycleRead,
		Question: pick([]string{
			"What's my " + lcSaveNoun + "?",
			"Remind me of my " + lcSaveNoun + ".",
			"What did I tell you my " + lcSaveNoun + " was?",
		}),
		ExpectedAnswer:    saveVal,
		DistractorAnswers: saveReadDis,
	}, readWave)

	if nChains >= 2 {
		// Chain 2: update. Old value A is seeded in the wave-0 haystack; the
		// instruction supersedes it with B.
		out.Pairs = append(out.Pairs, lifecyclePair(plan, 0,
			pick([]string{
				"Filing this away: my " + lcUpdNoun + " is " + updOld + ".",
				"Keep this for me: my " + lcUpdNoun + " is " + updOld + ".",
			}),
			"Noted. Your "+lcUpdNoun+" is "+updOld+". I'll remember it.",
		))
		addCase(2, protocol.MemoryCase{
			QuestionID:     "lc-upd-w",
			QuestionType:   QTLifecycleWrite,
			Question:       fmt.Sprintf(persona.Expand(r, updGrammar, "root"), updNew),
			ExpectedAnswer: updNew,
		}, 0)
		addCase(3, protocol.MemoryCase{
			QuestionID:   "lc-upd-r",
			QuestionType: QTLifecycleRead,
			Question: pick([]string{
				"What's my " + lcUpdNoun + "?",
				"What's the current access code for my storage unit?",
				"Which code opens my storage unit these days?",
			}),
			ExpectedAnswer:    updNew,
			DistractorAnswers: []string{saveVal},
		}, readWave)
	}

	if nChains >= 3 {
		// Chain 3: delete. Value F is seeded in wave 0; the instruction orders
		// its removal; the read is a decline case where surfacing F proves the
		// deletion never happened.
		out.Pairs = append(out.Pairs, lifecyclePair(plan, 1,
			pick([]string{
				"My " + lcDelNoun + " is " + delVal + ", keeping it here so I don't lose it.",
				"In case I forget: my " + lcDelNoun + " is " + delVal + ".",
			}),
			"Got it. Your "+lcDelNoun+" is "+delVal+".",
		))
		addCase(4, protocol.MemoryCase{
			QuestionID:     "lc-del-w",
			QuestionType:   QTLifecycleWrite,
			Question:       persona.Expand(r, delGrammar, "root"),
			ExpectedAnswer: lcDelNoun,
			// A delete INSTRUCTION, not a question: "Done, I've removed that" is
			// correct and names nothing, so value containment would grade phrasing
			// rather than behaviour. Whether the delete persisted is graded by
			// lc-del-r, which cannot be faked. Gated on v4 -- an AnswerKind is part
			// of the dataset bytes, and v3 is frozen.
			AnswerKind: acknowledgeKindFor(plan.BenchVersion),
		}, 0)
		addCase(5, protocol.MemoryCase{
			QuestionID:   "lc-del-r",
			QuestionType: QTLifecycleRead,
			Question: pick([]string{
				"What's my " + lcDelNoun + "?",
				"Do you still have my " + lcDelNoun + "?",
				"Remind me of my " + lcDelNoun + ".",
			}),
			ExpectedAnswer:    abstentionExpectedAnswer,
			AnswerKind:        protocol.AnswerDecline,
			DistractorAnswers: []string{delVal, saveVal},
		}, readWave)
	}
	return out
}

// lifecyclePair renders one wave-0 haystack pair for a chain's seeded fact.
// Timestamps use the same DatasetEpoch-anchored scheme as the persona
// renderer, offset into day 0 so they never collide with a session beat.
func lifecyclePair(plan *persona.Plan, idx int, prompt, response string) protocol.MemoryPair {
	ts := persona.TimeAnchor(plan).Add(3*time.Hour + time.Duration(idx*beatSpacingMinutes)*time.Minute)
	return protocol.MemoryPair{
		PairID:    fmt.Sprintf("p-lc-%d", idx),
		SessionID: "sess-lc",
		Timestamp: ts.Format(time.RFC3339),
		Prompt:    prompt,
		Response:  response,
	}
}
