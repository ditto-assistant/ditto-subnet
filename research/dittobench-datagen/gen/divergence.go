package gen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Parser-divergence canary cases (Bench v12, countermeasure C11).
//
// The adversary review of the held benchmark-emulator agents found the cheapest
// reliable detector to be a case where the DETERMINISTIC / TEMPLATE reading of a
// stored memory diverges from the CORRECT reading. A harness that keys on the
// surface template ("my dentist is <NAME>", "the code was <N>") confidently
// emits the salient template value; a genuine reasoner emits the value that only
// survives careful reading. The generator is public and deterministic, so the
// correct answer here is still a single, gradeable value — these are NOT
// non-deterministic cases. What makes them a trap is that the OBVIOUS surface
// answer is a specific WRONG value, registered as a distractor, so a template
// parser that emits it scores 0 while an honest reader scores 1.
//
// Four divergence types, one per family member, each resolvable by exactly one
// careful reading:
//
//   - Negation: "my dentist is not Dr. A. My dentist is Dr. B." -> B, not A.
//   - Retraction / correction-supersession: "you used to bank at A; last month I
//     moved to B" -> B, not A.
//   - Hypothetical / counterfactual: "if the invoice had been approved it would
//     be $X, but it was rejected; the billed amount is $Y" -> Y, not X.
//   - Reported / unreliable speech: "Dana insisted the code was A, but my own
//     note says B" -> B, not A.
//
// Entities are drawn from the large humandata wordbanks (10k given names /
// surnames), so the confusable surface is not a tiny fixed set that could be
// enumerated. The cases are wire-INDISTINGUISHABLE from ordinary scored cases:
// only the opaque case_id + question reach the harness (RunRequest), and neither
// the QuestionType, the ExpectedAnswer, nor the DistractorAnswers ever crosses
// the wire. No canary marker is planted in the seeded memory or the question.
//
// These are a COST/DETECTION addition, not the structural defense (that is the
// causal model-dependence gate). They are v12-gated; v2..v11 never reach this
// code, so their bytes are unchanged.

const (
	QTParserDivergenceNegation     = "parser-divergence-negation"
	QTParserDivergenceRetraction   = "parser-divergence-retraction"
	QTParserDivergenceHypothetical = "parser-divergence-hypothetical"
	QTParserDivergenceReported     = "parser-divergence-reported-speech"
)

// v12DivergenceCaseCount is the bounded share of the v12 memory mix spent on the
// parser-divergence family, a multiple of four (one of each divergence type per
// round) so every type is always represented. The count is carved out of the
// world-question budget in generateV8WorldMemorySuite, so the total memory-case
// envelope is unchanged.
func v12DivergenceCaseCount(n int) int {
	switch {
	case n >= 100:
		return 12 // full: three rounds of four
	case n >= 40:
		return 8 // medium: two rounds of four
	default:
		return 4 // small: one round of four
	}
}

var v12BankSuffixes = []string{"Savings", "Credit Union", "Federal", "Bank & Trust", "Financial"}
var v12ProjectSuffixes = []string{"rollout", "migration", "account", "onboarding", "refresh"}

// v12DivergenceRand derives the deterministic entity stream for the family. It
// is an independent seed stream (a hash of the master seed) so drawing names for
// these cases cannot perturb any other generator RNG.
func v12DivergenceRand(seed int64) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v12-parser-divergence:%d", seed)
	return rand.New(rand.NewSource(int64(h.Sum64())))
}

// buildParserDivergence returns the v12 parser-divergence cases and the memory
// pairs they seed (which the caller adds to wave 0). count must be a multiple of
// four. Every value is deterministic in seed.
func buildParserDivergence(seed int64, count int) ([]StagedCase, []protocol.MemoryPair) {
	if count <= 0 {
		return nil, nil
	}
	r := v12DivergenceRand(seed)
	rounds := count / 4
	cases := make([]StagedCase, 0, count)
	pairs := make([]protocol.MemoryPair, 0, count)
	ordinal := 0

	// add wires one case: a single seeded memory pair carrying both the surface
	// (wrong) value and the correct value, plus the graded case whose distractor
	// is exactly that surface value. protectedWords lists the tokens the v8 typo
	// projector must never corrupt (the load-bearing values and the divergence
	// cue), so the trap stays fair and resolvable.
	add := func(round int, questionType, prompt, response, question, expected, surface, answerKind string, protectedWords []string) {
		caseID := protocol.OpaqueCaseID(seed, "v12-parser-divergence", ordinal)
		pairID := protocol.OpaqueCaseID(seed, "v12-parser-divergence-pair", ordinal)
		ordinal++
		pair := protocol.MemoryPair{
			PairID:    pairID,
			SessionID: protocol.OpaqueCaseID(seed, "v12-parser-divergence-session", ordinal),
			Timestamp: fmt.Sprintf("2026-02-%02dT%02d:15:00Z", 1+(ordinal%27), 8+(ordinal%12)),
			Prompt:    prompt,
			Response:  response,
		}
		pairs = append(pairs, pair)
		mc := protocol.MemoryCase{
			BenchVersion:      protocol.BenchVersionV12,
			ID:                caseID,
			QuestionID:        caseID,
			QuestionType:      questionType,
			Question:          question,
			ExpectedAnswer:    expected,
			AnswerKind:        answerKind,
			DistractorAnswers: []string{surface},
			WritingProtected:  protectedWords,
		}
		cases = append(cases, StagedCase{Case: mc, RunAfterWave: 0, RequiredPairIDs: []string{pairID}})
	}

	for round := 0; round < rounds; round++ {
		// ── Negation ──────────────────────────────────────────────────────────
		wrongGiven, wrongSur := humandata.GivenName(r, 2*ordinal), humandata.Surname(r, 2*ordinal)
		rightGiven, rightSur := v12DistinctName(r, ordinal, wrongGiven, wrongSur)
		wrongName := wrongGiven + " " + wrongSur
		rightName := rightGiven + " " + rightSur
		add(round, QTParserDivergenceNegation,
			fmt.Sprintf("One thing to keep straight in my records: my dentist is not Dr. %s. My current dentist is Dr. %s.", wrongName, rightName),
			"Understood — I'll keep only your current dentist on file.",
			"Who is my current dentist? Just the name is fine.",
			rightName, wrongName, protocol.AnswerValue,
			[]string{rightName, wrongName, rightGiven, rightSur, wrongGiven, wrongSur})

		// ── Retraction / correction-supersession ──────────────────────────────
		oldBank := v12Bank(r)
		newBank := v12DistinctBank(r, oldBank)
		add(round, QTParserDivergenceRetraction,
			fmt.Sprintf("Update to my finances: you used to have me down as banking with %s. As of last month I moved every account over to %s, so %s is my bank now.", oldBank, newBank, newBank),
			"Got it — updating your bank to the new one.",
			"Which bank do I use now?",
			newBank, oldBank, protocol.AnswerValue,
			append([]string{newBank, oldBank}, strings.Fields(newBank+" "+oldBank)...))

		// ── Hypothetical / counterfactual ─────────────────────────────────────
		project := humandata.Surname(r, 3*ordinal) + " " + v12ProjectSuffixes[r.Intn(len(v12ProjectSuffixes))]
		hypoDollars := 3000 + r.Intn(7000)
		realDollars := 500 + r.Intn(2500)
		add(round, QTParserDivergenceHypothetical,
			fmt.Sprintf("Note on the %s invoice: if it had been approved it would have come to $%d, but it was rejected. The amount actually billed is $%d.", project, hypoDollars, realDollars),
			"Noted — I'll record the amount that was actually billed.",
			fmt.Sprintf("What was actually billed on the %s invoice?", project),
			fmt.Sprintf("%d", realDollars*100), fmt.Sprintf("%d", hypoDollars*100), protocol.AnswerMoney,
			append([]string{project}, strings.Fields(project)...))

		// ── Reported / unreliable speech ──────────────────────────────────────
		narrator := humandata.GivenName(r, 4*ordinal)
		wrongCode := 1000 + r.Intn(9000)
		rightCode := v12DistinctCode(r, wrongCode)
		add(round, QTParserDivergenceReported,
			fmt.Sprintf("%s insisted the front-door code was %d, but my own note says it is %d. My note is the one to trust.", narrator, wrongCode, rightCode),
			"Understood — I'll trust your own note for the door code.",
			"What is my front-door code?",
			fmt.Sprintf("%d", rightCode), fmt.Sprintf("%d", wrongCode), protocol.AnswerNumber,
			[]string{narrator})
	}
	return cases, pairs
}

// v12DistinctName draws a full name that differs from (given, surname) in both
// parts, so the correct answer and the surface distractor never share a token.
func v12DistinctName(r *rand.Rand, ordinal int, avoidGiven, avoidSurname string) (string, string) {
	for i := 0; ; i++ {
		g := humandata.GivenName(r, 2*ordinal+1+i)
		s := humandata.Surname(r, 2*ordinal+1+i)
		if !strings.EqualFold(g, avoidGiven) && !strings.EqualFold(s, avoidSurname) {
			return g, s
		}
	}
}

func v12Bank(r *rand.Rand) string {
	return humandata.Surname(r, 0) + " " + v12BankSuffixes[r.Intn(len(v12BankSuffixes))]
}

func v12DistinctBank(r *rand.Rand, avoid string) string {
	for {
		b := v12Bank(r)
		if !strings.EqualFold(b, avoid) {
			return b
		}
	}
}

func v12DistinctCode(r *rand.Rand, avoid int) int {
	for {
		c := 1000 + r.Intn(9000)
		if c != avoid {
			return c
		}
	}
}
