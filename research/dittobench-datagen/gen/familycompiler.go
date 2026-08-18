package gen

import (
	"fmt"
	"hash/fnv"
	"math/rand"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Anti-family-compiler case families (Bench v12, countermeasure C12).
//
// THREAT (dossier Rev7). The dominant held emulator is a FAMILY COMPILER: it
// classifies a bench question into a closed DittoBench family via a baked
// family-router, then applies a hardcoded per-family recipe (subtract /
// adjust-subtract / latest-correction / larger-minus-settled), sometimes with the
// answer pre-computed from the records and injected into the model input as a
// note. It passes both refusal limbs and spends real tokens, so the runtime
// gates cannot catch it on behavior alone.
//
// DEFENSE (the dataset). Make the required computation NOT recoverable from the
// question SURFACE, so a surface classifier applies the wrong recipe while a
// genuine model that READS THE RECORDS gets it right. Three families, all
// carved out of the world-question budget (the total memory envelope is
// unchanged), all v12-gated (v2..v11 never reach this code, so their bytes are
// byte-identical):
//
//  1. Record-determined operation (highest leverage). The QUESTION is generic
//     ("current balance on X") and does NOT signal the operation. WHICH operation
//     applies is determined by the RECORD STRUCTURE the agent must read: a bare
//     approved+paid pair -> subtract; an adjustment record present ->
//     adjust-subtract; a superseding correction present -> latest-correction; a
//     draft that exceeds the approved figure under a "higher-of governs" note ->
//     larger-minus-settled. A classifier routing on the surface cannot pick the
//     operation; it applies one fixed recipe and is wrong on every case whose
//     record structure implies a different one. The other-recipe result is planted
//     as a DistractorAnswer, so a mis-routed compiler scores 0.
//
//  2. Family-ambiguity traps. The surface strongly matches the balance family,
//     but a record-level twist changes the required computation: a "written off /
//     fully forgiven" record (the answer is 0, not the computed balance) or a
//     cross-entity reference (the subject's balance is defined as ANOTHER entity's
//     balance). A compiler applies the surface recipe and is wrong; a reader sees
//     the twist. The naive-recipe result is the DistractorAnswer.
//
//  3. Counterfactual pairs (perturb-evidence in the dataset). A linked base case
//     and a counterfactual variant whose ONE changed record fact (the settled
//     payment) moves the answer. Both are ordinary scored cases with a single
//     deterministic answer; the base answer is a DistractorAnswer on the variant
//     and vice-versa. A fixed pre-compiled total that assumed the base gives the
//     stale answer on the variant and scores 0, while a reader that re-reads the
//     changed record answers both.
//
// FAIRNESS. Every case has a single defensible deterministic answer derivable by
// reading and reasoning over its own seeded records, so a genuine ~0.8 reader
// stays ~0.8. The oracle answer is the ONLY registered ExpectedAnswer; the
// naive/mis-recipe value is a DistractorAnswer, so it is the surface classifier —
// not the honest reader — that the trap zeroes.
//
// WIRE-OPACITY. Only the opaque case_id + question reach the harness (RunRequest);
// the QuestionType, ExpectedAnswer, and DistractorAnswers are validator-internal
// and never cross the wire. The question surface is generic and identical across
// every record shape, so it carries no operation cue. QuestionTypes avoid the
// injection/canary/isolation substrings that trigger special grader excusing.
//
// PROVABILITY (answer-stuffing gate, countermeasure C-D). Every correct total is
// DERIVED, never verbatim in the seeded records (the records carry the operands,
// never their combination). So each case is a genuine COMPUTED case
// (memoryAnswerIsComputed): a compiler that pre-computes the total and injects it
// into the model input feeds a value that is absent-from-memory, which the v12
// answer-stuffing gate's provable path flags. See
// TestFamilyCompiledTotalsAbsentFromSeededEvidence.

// Family-compiler QuestionTypes. Distinct per record shape so tests can route on
// the shape, but validator-internal (never on the wire) and free of the
// injection/canary/isolation substrings the grader special-cases. The shape is
// NOT encoded in the wire question — the surface is generic and identical across
// shapes (TestFamilyCompilerWireOpaque).
const (
	QTRecordBalancePlain      = "record-balance-plain"      // approved+paid -> subtract
	QTRecordBalanceAdjusted   = "record-balance-adjusted"   // adjustment present -> adjust-subtract
	QTRecordBalanceSuperseded = "record-balance-superseded" // two corrections -> latest governs
	QTRecordBalanceCapped     = "record-balance-capped"     // draft>approved -> larger-minus-settled
	QTRecordBalanceForgiven   = "record-balance-forgiven"   // written off -> 0
	QTRecordBalanceReferred   = "record-balance-referred"   // cross-entity reference
	QTRecordBalanceCFBase     = "record-balance-cf-base"    // counterfactual base
	QTRecordBalanceCFVariant  = "record-balance-cf-variant" // counterfactual variant
)

// Family classifications for FamilyCompilerCase.Family / test grouping.
const (
	familyRecordOp       = "record-op"
	familyAmbiguity      = "ambiguity"
	familyCounterfactual = "counterfactual"
)

// FamilyCompilerCase is one generated anti-family-compiler case plus the metadata
// the tests use to model a surface classifier. Only Staged.Case (its opaque id +
// generic question) and Pairs cross the harness wire; every other field is
// validator/test-internal.
type FamilyCompilerCase struct {
	Staged StagedCase
	Pairs  []protocol.MemoryPair
	// Family is one of familyRecordOp / familyAmbiguity / familyCounterfactual.
	Family string
	// CorrectCents is the oracle answer in minor units (== Staged.Case.ExpectedAnswer).
	CorrectCents int
	// NaiveSubtractCents is what a fixed-recipe compiler that computes
	// (first-approved - paid) over the SUBJECT'S OWN figures emits. It equals
	// CorrectCents only on the plain-subtract shape (the compiler's home turf); on
	// every other record-op / ambiguity shape it is a registered distractor, so the
	// compiler scores 0. Zero for counterfactual cases (they are caught by the cache
	// model, not the fixed-recipe model).
	NaiveSubtractCents int
	// LinkedCents is the other counterfactual member's correct answer (a registered
	// distractor on this member). Zero for non-counterfactual cases.
	LinkedCents int
}

// v12FamilyCompilerCaseCount is the bounded share of the v12 memory mix spent on
// the anti-family-compiler families. It is carved out of the world-question
// budget in generateV8WorldMemorySuite, so the total memory-case envelope is
// unchanged from v10/v11. Sizes mirror the divergence family's run-size tiers and
// stay small enough that the world-question budget after v10 programs + divergence
// stays positive on every public profile.
func v12FamilyCompilerCaseCount(n int) int {
	switch {
	case n >= 100:
		return 24
	case n >= 40:
		return 12
	default:
		return 2
	}
}

// splitFamilyCompiler allocates a total case count across the three families:
// ~50% record-determined operation (the highest-leverage family), ~25%
// family-ambiguity, ~25% counterfactual (an even count, since each is a base +
// variant pair). A tiny profile carries only record-op cases.
func splitFamilyCompiler(count int) (recordOp, ambiguity, counterfactual int) {
	if count <= 0 {
		return 0, 0, 0
	}
	counterfactual = (count / 4) &^ 1 // even (pairs), ~25%
	ambiguity = count / 4             // ~25%
	recordOp = count - ambiguity - counterfactual
	return recordOp, ambiguity, counterfactual
}

// v12FamilyCompilerRand derives an independent, deterministic entity/amount
// stream (a hash of the master seed), so drawing for these cases cannot perturb
// any other generator RNG. Mirrors v12DivergenceRand.
func v12FamilyCompilerRand(seed int64) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v12-family-compiler:%d", seed)
	return rand.New(rand.NewSource(int64(h.Sum64())))
}

var familyBalanceOpeners = []string{
	"Quick bookkeeping question.",
	"Reconciling my accounts.",
	"Checking one of my invoices.",
	"Sorting out what I owe.",
}

// buildFamilyCompiler returns the v12 anti-family-compiler cases and the memory
// pairs they seed. Every value is deterministic in seed. The caller adds the
// pairs to wave 0 and the staged cases to the suite.
func buildFamilyCompiler(seed int64, count int) []FamilyCompilerCase {
	if count <= 0 {
		return nil
	}
	r := v12FamilyCompilerRand(seed)
	recordOp, ambiguity, counterfactual := splitFamilyCompiler(count)
	out := make([]FamilyCompilerCase, 0, count)
	ordinal := 0

	// name draws a distinct full name from the 10k humandata wordbanks.
	name := func() string {
		g := humandata.GivenName(r, ordinal)
		s := humandata.Surname(r, ordinal)
		ordinal++
		return g + " " + s
	}
	dollars := func(lo, span int) int { return lo + r.Intn(span) }

	// mkCase materializes one single-record case: a memory pair carrying the
	// records in prose, and a graded case whose answer is DERIVED (never verbatim
	// in the pair). protectedWords keeps the subject names out of the typo
	// projector; the money amounts are digit/`$`-guarded automatically.
	mkCase := func(family, questionType, records, question string, correctCents, naiveCents, linkedCents int, distractorCents []int, protectedWords []string) {
		caseID := protocol.OpaqueCaseID(seed, "v12-family-compiler", ordinal)
		pairID := protocol.OpaqueCaseID(seed, "v12-family-compiler-pair", ordinal)
		ordinal++
		pair := protocol.MemoryPair{
			PairID:    pairID,
			SessionID: protocol.OpaqueCaseID(seed, "v12-family-compiler-session", ordinal),
			Timestamp: fmt.Sprintf("2026-03-%02dT%02d:20:00Z", 1+(ordinal%27), 8+(ordinal%12)),
			Prompt:    records,
			Response:  "Noted — I've filed those account details.",
		}
		mc := protocol.MemoryCase{
			BenchVersion:      protocol.BenchVersionV12,
			ID:                caseID,
			QuestionID:        caseID,
			QuestionType:      questionType,
			Question:          question,
			ExpectedAnswer:    fmt.Sprintf("%d", correctCents),
			AnswerKind:        protocol.AnswerMoney,
			DistractorAnswers: centsStrings(dedupeDistractors(correctCents, distractorCents)),
			WritingProtected:  protectedWords,
		}
		out = append(out, FamilyCompilerCase{
			Staged:             StagedCase{Case: mc, RunAfterWave: 0, RequiredPairIDs: []string{pairID}},
			Pairs:              []protocol.MemoryPair{pair},
			Family:             family,
			CorrectCents:       correctCents,
			NaiveSubtractCents: naiveCents,
			LinkedCents:        linkedCents,
		})
	}

	opener := func(salt int) string { return familyBalanceOpeners[salt%len(familyBalanceOpeners)] }
	balanceQuestion := func(subject string, salt int) string {
		return fmt.Sprintf("%s What is the current balance owed on %s's account? Answer with the dollar amount.", opener(salt), subject)
	}

	// ── Family 1: record-determined operation ────────────────────────────────
	for i := 0; i < recordOp; i++ {
		shape := i % 4
		subject := name()
		salt := ordinal
		switch shape {
		case 0: // plain subtract: approved + paid only
			var approved, paid, correct int
			for {
				paid = dollars(100, 900)
				approved = paid + dollars(200, 1500)
				correct = approved - paid
				// The DERIVED total must not coincide with a printed operand, so the
				// answer stays absent-from-memory (the answer-stuffing provable path).
				if !collidesWith(correct, approved, paid) {
					break
				}
			}
			mkCase(familyRecordOp, QTRecordBalancePlain,
				fmt.Sprintf("Account notes for %s: the invoice was approved at $%d, and a payment of $%d has cleared against it. Nothing else is on the account.", subject, approved, paid),
				balanceQuestion(subject, salt),
				correct*100, correct*100, 0,
				[]int{approved * 100}, // raw approved figure (forgot to subtract the payment)
				[]string{subject})
		case 1: // adjustment present -> adjust-subtract
			var approved, paid, mag, correct, naive int
			var word string
			for {
				paid = dollars(100, 900)
				approved = paid + dollars(300, 1500)
				raise := r.Intn(2) == 0
				mag = dollars(50, 400)
				if !raise && mag >= approved-paid {
					mag = (approved - paid) / 2 // keep the adjusted balance positive
				}
				adjusted := approved + mag
				word = "raised"
				if !raise {
					adjusted = approved - mag
					word = "lowered"
				}
				correct = adjusted - paid
				naive = approved - paid // ignoring the adjustment
				if correct > 0 && correct != naive && !collidesWith(correct, approved, paid, mag) {
					break
				}
			}
			mkCase(familyRecordOp, QTRecordBalanceAdjusted,
				fmt.Sprintf("Account notes for %s: the invoice was approved at $%d. A later adjustment %s the approved amount by $%d. A payment of $%d has since cleared.", subject, approved, word, mag, paid),
				balanceQuestion(subject, salt),
				correct*100, naive*100, 0,
				[]int{naive * 100, approved * 100},
				[]string{subject})
		case 2: // superseding correction -> latest governs
			var first, second, paid, correct, stale int
			for {
				paid = dollars(100, 900)
				first = paid + dollars(200, 1000)
				second = paid + dollars(200, 1000)
				correct = second - paid
				stale = first - paid // the superseded (first) reading
				if second != first && correct != stale && !collidesWith(correct, first, second, paid) {
					break
				}
			}
			mkCase(familyRecordOp, QTRecordBalanceSuperseded,
				fmt.Sprintf("Account notes for %s: the invoice was first approved at $%d. That figure was later corrected — the current approved amount is $%d. A payment of $%d has cleared.", subject, first, second, paid),
				balanceQuestion(subject, salt),
				correct*100, stale*100, 0,
				[]int{stale * 100, first * 100, second * 100},
				[]string{subject})
		default: // 3: draft exceeds approved -> larger-minus-settled
			var approved, draft, paid, correct, naive int
			for {
				paid = dollars(100, 900)
				approved = paid + dollars(100, 500)
				draft = approved + dollars(200, 1000) // draft strictly exceeds approved
				correct = draft - paid                // max(draft, approved) - paid
				naive = approved - paid               // plain subtract over the approved figure
				if correct != naive && !collidesWith(correct, draft, approved, paid) {
					break
				}
			}
			mkCase(familyRecordOp, QTRecordBalanceCapped,
				fmt.Sprintf("Account notes for %s: the invoice was drafted at $%d but approved at the lower figure of $%d. Per this account's policy the higher of the drafted and approved amounts is what stands. A payment of $%d has cleared.", subject, draft, approved, paid),
				balanceQuestion(subject, salt),
				correct*100, naive*100, 0,
				[]int{naive * 100, draft * 100, approved * 100},
				[]string{subject})
		}
	}

	// ── Family 2: family-ambiguity traps ─────────────────────────────────────
	for i := 0; i < ambiguity; i++ {
		subject := name()
		salt := ordinal
		if i%2 == 0 { // written off / fully forgiven -> 0
			paid := dollars(100, 900)
			approved := paid + dollars(200, 1500)
			naive := approved - paid // the balance a surface recipe computes
			mkCase(familyAmbiguity, QTRecordBalanceForgiven,
				fmt.Sprintf("Account notes for %s: the invoice was approved at $%d and $%d had been paid, but the remaining balance was then written off in full — the debt is fully forgiven and nothing further is owed.", subject, approved, paid),
				balanceQuestion(subject, salt),
				0, naive*100, 0,
				[]int{naive * 100, approved * 100},
				[]string{subject})
		} else { // cross-entity reference
			other := name()
			var ownApproved, ownPaid, otherApproved, otherPaid, correct, naive int
			for {
				ownPaid = dollars(100, 800)
				ownApproved = ownPaid + dollars(150, 1000) // own approved strictly exceeds own paid
				otherPaid = dollars(100, 900)
				otherApproved = otherPaid + dollars(200, 1000)
				correct = otherApproved - otherPaid
				naive = ownApproved - ownPaid // the subject's own figures
				// The trap must be real (own balance != referenced balance) and the
				// referenced total must not coincide with any printed operand.
				if naive != correct && !collidesWith(correct, ownApproved, ownPaid, otherApproved, otherPaid) {
					break
				}
			}
			mkCase(familyAmbiguity, QTRecordBalanceReferred,
				fmt.Sprintf("Account notes for %s: %s's balance is billed through %s's account and always equals %s's current balance. For reference, %s's own draft was $%d with $%d paid on it. %s's account: approved at $%d with $%d cleared.",
					subject, subject, other, other, subject, ownApproved, ownPaid, other, otherApproved, otherPaid),
				balanceQuestion(subject, salt),
				correct*100, naive*100, 0,
				[]int{naive * 100, ownApproved * 100, otherApproved * 100},
				[]string{subject, other})
		}
	}

	// ── Family 3: counterfactual pairs (perturb-evidence) ────────────────────
	for i := 0; i < counterfactual/2; i++ {
		subjectBase := name()
		subjectVar := name()
		var approved, paidBase, paidVar, correctBase, correctVar int
		for {
			approved = dollars(500, 1500)
			paidBase = dollars(100, approved-200)
			paidVar = paidBase + dollars(100, 400)
			correctBase = approved - paidBase
			correctVar = approved - paidVar
			// The one perturbed fact (paid) must move the answer, both members must
			// stay positive, and neither DERIVED total may coincide with a printed
			// operand (approved / that member's paid).
			if paidVar != paidBase && correctVar > 0 && correctBase > 0 &&
				!collidesWith(correctBase, approved, paidBase) &&
				!collidesWith(correctVar, approved, paidVar) {
				break
			}
		}
		saltBase := ordinal
		// The two members share the same approved figure and question surface;
		// ONLY the settled payment differs. A cache/pre-compiled total keyed on the
		// surface answers both alike and is wrong on one. The other member's answer
		// is a registered distractor on each.
		mkCase(familyCounterfactual, QTRecordBalanceCFBase,
			fmt.Sprintf("Account notes for %s: the invoice was approved at $%d, and a payment of $%d has cleared against it.", subjectBase, approved, paidBase),
			balanceQuestion(subjectBase, saltBase),
			correctBase*100, 0, correctVar*100,
			[]int{correctVar * 100, approved * 100},
			[]string{subjectBase})
		saltVar := ordinal
		mkCase(familyCounterfactual, QTRecordBalanceCFVariant,
			fmt.Sprintf("Account notes for %s: the invoice was approved at $%d, and a payment of $%d has cleared against it.", subjectVar, approved, paidVar),
			balanceQuestion(subjectVar, saltVar),
			correctVar*100, 0, correctBase*100,
			[]int{correctBase * 100, approved * 100},
			[]string{subjectVar})
	}

	return out
}

// dedupeDistractors returns the positive distractor cents values with any value
// equal to the correct answer (or duplicates) removed, so no distractor ever
// collides with the graded answer.
func dedupeDistractors(correctCents int, candidates []int) []int {
	seen := map[int]bool{correctCents: true}
	out := make([]int, 0, len(candidates))
	for _, c := range candidates {
		if c <= 0 || seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, c)
	}
	return out
}

// collidesWith reports whether the derived total (in dollars) equals any printed
// operand (in dollars). A collision would make the answer verbatim-present in the
// records, defeating both the trap (an operand is trivially readable) and the
// answer-stuffing provable path (the total would no longer be absent-from-memory).
func collidesWith(total int, operands ...int) bool {
	for _, o := range operands {
		if o == total {
			return true
		}
	}
	return false
}

func centsStrings(cents []int) []string {
	out := make([]string, 0, len(cents))
	for _, c := range cents {
		out = append(out, fmt.Sprintf("%d", c))
	}
	return out
}
