package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v13 program cases. v13 inherits the ENTIRE v12 open-program contract —
// the scenario draw, the four sampled program shapes, the per-seed schema
// labels, the renderer classes, the shuffled prose records, the distractor set
// and the metamorphic-group machinery are the v12 ones, unchanged, produced by
// the same code path (generateV12FamilyPrograms). What v13 changes is two
// answerability defects in the v12 surface, both of which cost a CORRECT reader
// points for reasons that have nothing to do with memory:
//
//   - Defect 1 (minor-unit instruction the grader rejects). Every v12 open
//     program closes with a v12UnitFrames sentence instructing the reader to
//     answer "as minor units" — the internal representation the expected answer
//     is stored in. The grader's money comparison (grade.moneyHit) deliberately
//     does the opposite: it accepts the ordinary decimal renderings of a
//     currency amount and INTENTIONALLY rejects a bare integer equal to the
//     minor-unit value, because the contract never asks a human to speak in the
//     platform's internal units. A reader that followed the instruction exactly
//     scored zero on every open program. v13 asks for what the grader accepts:
//     a decimal amount in the currency's major unit, to two places. The stored
//     expected answer stays the minor-unit integer, so nothing downstream of the
//     grader changes.
//
//   - Defect 2 (ambiguous counterfactual selector). A v12 metamorphic group
//     renders TWO distinct record sets into the same haystack: the base
//     scenario (three variants) and its causal counterfactual, whose alias is
//     the base alias with a "-revision" suffix and whose graded answer is
//     deliberately different. Both carry a settled payment, both use the same
//     per-seed schema labels, and all four variants ask for the subject with the
//     SAME relational descriptor ("the workstream in this batch that has a
//     settled payment on record"). The descriptor therefore resolves to two
//     threads with two different correct answers, and a reader that resolves it
//     to the other one is graded wrong. v13 files each record set on a named
//     docket — original or reissued — states that in the binding record, and
//     has the question name the docket it wants. The binding stays relational:
//     no alias is echoed and no printed amount is looked up, so Gap 3 of the v12
//     contract is intact.
//
// Both changes are gated on benchVersion, so v10/v11/v12 generation is
// byte-identical to what it was (the v12 known-vector and determinism tests run
// unchanged against the shared code path).

// V13ProvenanceRevision is the generator-spec revision recorded on v13 open
// programs. v12 cases keep V12ProvenanceRevision.
const V13ProvenanceRevision = "dittobench-v13-generator-spec-v1"

func v12FamilyProvenanceRevision(benchVersion int) string {
	if benchVersion >= protocol.BenchVersionV13 {
		return V13ProvenanceRevision
	}
	return V12ProvenanceRevision
}

// v13UnitFrames close the open program by asking for a decimal amount in the
// currency's major unit, with the currency code substituted for %s. Compare
// v12UnitFrames, which ask for minor units. Two decimal places is what
// grade.parseMoneyToken accepts alongside a whole-unit integer.
var v13UnitFrames = []string{
	"Give the result as a decimal %s amount, to two decimal places.",
	"Answer with a decimal %s figure carrying two decimal places.",
	"Report the amount in %s as a decimal to two decimal places.",
	"State the balance as a decimal %s amount, two decimal places.",
}

// v13UnitFrame renders the closing unit sentence for the case's contract. The
// pick salt is the v12 one, so the frame index is the same draw in both
// contracts and only the bank differs.
func v13UnitFrame(seed int64, group, variant int, unit string, benchVersion int) string {
	salt := fmt.Sprintf("qunit-%d-%d", group, variant)
	if benchVersion < protocol.BenchVersionV13 {
		return fmt.Sprintf(v12Pick(seed, salt, v12UnitFrames), unit)
	}
	return fmt.Sprintf(v12Pick(seed, salt, v13UnitFrames), v13MajorUnit(unit))
}

// v13MajorUnit turns a minor-unit noun ("USD cents") into the currency code the
// reader is asked to answer in ("USD"). The records keep stating amounts in
// minor units; converting them is part of the program.
func v13MajorUnit(unit string) string {
	if code, _, ok := strings.Cut(unit, " "); ok && code != "" {
		return code
	}
	return unit
}

// The two dockets a v12-family group renders. Index 0 is the base scenario
// (shared by the base, renderer-invariant and distractor-invariant variants);
// index 1 is the causal counterfactual, which mutates the operands and so has a
// different graded answer.
const (
	v13DocketOriginal = 0
	v13DocketReissued = 1
)

// v13DocketFor maps a metamorphic relation to the docket its record set is
// filed on. Only the causal counterfactual carries mutated operands.
func v13DocketFor(relation string) int {
	if relation == "causal_counterfactual" {
		return v13DocketReissued
	}
	return v13DocketOriginal
}

// v13DocketMarkers are appended to the binding record so the docket is readable
// from the prose. They name no amount and echo no alias beyond the binding that
// was already there.
var v13DocketMarkers = [2]string{
	"Filed on the original docket for this workstream; no reissue replaces it.",
	"Filed on the reissued docket for this workstream, replacing its original docket.",
}

// v13DocketQualifiers are appended to the relational subject descriptor so the
// question selects exactly one docket.
var v13DocketQualifiers = [2]string{
	", filed on the original docket",
	", filed on the reissued docket",
}
