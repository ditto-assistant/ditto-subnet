package gen

import (
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/internal/humandata"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Anti-family-compiler cases, v2 (Bench v13).
//
// The v12 family (gen/familycompiler.go) let the RECORD STRUCTURE decide the
// operation. The measured v12 board beat it anyway: the structural cue words
// ("credit memo", "written off", "higher of") are public renderer strings, so a
// solver reads the cue and picks the operation without reading the record. v2
// makes the cue useless on its own while keeping every case honest and fair:
//
//   - The record STATES its own sign convention. A credit memo "reduces what is
//     owed" on one account, "is billed on top" on another, and "is a memo entry
//     only; what is owed is unaffected" on a third. The convention varies per
//     record, so a cue → operation table is right about a third of the time,
//     while a general assistant is never asked to know bookkeeping norms — the
//     record tells it.
//   - Non-monetary units. Six of sixteen cases count nights, seats, hours,
//     licences, or percentage points, so the family no longer feeds the money
//     share of the bench.
//   - Multi-currency with explicit unit claims. Money cases are stated and asked
//     in EUR, GBP, USD, or CAD; the grader-only Claim carries the unit.
//   - Three-valued direction. Direction cases ask whether the cue raised,
//     lowered, or left the figure unchanged, plus the standing figure; the
//     direction is the sign of the quantity effect, graded through the
//     direction/value list items today and the sign of the quantity claim under
//     the v13 grader.
//   - Other-recipe results as distractors. The figure every other stated
//     convention would produce is a registered distractor, so a mis-mapped
//     solver scores 0 rather than a near miss.
//   - Six counterfactual pairs. Base and variant share every fact except the
//     stated convention; the sibling's answer is a distractor on each member, so
//     a solver that ignores the convention sentence is wrong on one of them.
//
// Every correct total is DERIVED (never verbatim in the record), so each case
// stays a computed case under AnswerVerbatimInEvidence. Everything is
// deterministic in seed; v2..v12 never reach this code.

// Family-compiler v2 QuestionTypes. Validator-internal; free of the
// injection/canary/isolation substrings the grader special-cases.
const (
	QTRecordQuantityMoney     = "record-quantity-money"
	QTRecordQuantityUnits     = "record-quantity-units"
	QTRecordQuantityDirection = "record-quantity-direction"
	QTRecordQuantityCFBase    = "record-quantity-cf-base"
	QTRecordQuantityCFVariant = "record-quantity-cf-variant"
)

// Stated sign conventions.
const (
	ConventionAdds    = "adds"
	ConventionReduces = "reduces"
	ConventionNone    = "none"
)

var familyV2Conventions = []string{ConventionAdds, ConventionReduces, ConventionNone}

// FamilyV2Unit describes one graded unit.
type FamilyV2Unit struct {
	Name     string // "EUR", "nights", ...
	Monetary bool
	Symbol   string // currency symbol for money; empty otherwise
	Noun     string // what the figure measures, for the convention sentence
}

// FamilyV2Currencies are the money units, cycled per case.
var FamilyV2Currencies = []FamilyV2Unit{
	{Name: "EUR", Monetary: true, Symbol: "€", Noun: "what is owed"},
	{Name: "GBP", Monetary: true, Symbol: "£", Noun: "what is owed"},
	{Name: "USD", Monetary: true, Symbol: "$", Noun: "what is owed"},
	{Name: "CAD", Monetary: true, Symbol: "CA$", Noun: "what is owed"},
}

// FamilyV2Units are the non-monetary units, cycled per case.
var FamilyV2Units = []FamilyV2Unit{
	{Name: "nights", Noun: "the booked nights"},
	{Name: "seats", Noun: "our reserved seats"},
	{Name: "hours", Noun: "the retainer hours"},
	{Name: "licences", Noun: "the licence pool"},
	{Name: "percentage points", Noun: "the discount"},
}

// FamilyV2Cue is one public cue phrase and the convention a bookkeeping-savvy
// solver would assume for it. The cue-reading acceptance adversary in the
// tests is built from exactly this bank.
type FamilyV2Cue struct {
	Phrase       string
	Conventional string
	Monetary     bool
}

// FamilyV2Cues is the cue bank. The record always states the convention that
// actually applies; Conventional is only what a solver would guess. The
// non-monetary cues are listed in FamilyV2Units order (one cue per unit).
var FamilyV2Cues = []FamilyV2Cue{
	{Phrase: "credit memo", Conventional: ConventionReduces, Monetary: true},
	{Phrase: "surcharge note", Conventional: ConventionAdds, Monetary: true},
	{Phrase: "retainer entry", Conventional: ConventionReduces, Monetary: true},
	{Phrase: "service credit", Conventional: ConventionReduces, Monetary: true},
	{Phrase: "late-fee line", Conventional: ConventionAdds, Monetary: true},
	{Phrase: "comp voucher", Conventional: ConventionAdds},
	{Phrase: "hold release", Conventional: ConventionReduces},
	{Phrase: "carry-over", Conventional: ConventionAdds},
	{Phrase: "float allocation", Conventional: ConventionAdds},
	{Phrase: "loyalty step", Conventional: ConventionAdds},
}

// familyV2ConventionForms render the stated convention. Args: cue phrase,
// noun. Three forms per convention; the pick is seeded per case.
var familyV2ConventionForms = map[string][]string{
	ConventionAdds: {
		"on this account a %[1]s is billed on top, so it adds to %[2]s",
		"in these books a %[1]s increases %[2]s",
		"here a %[1]s is added to %[2]s, not taken off it",
	},
	ConventionReduces: {
		"on this account a %[1]s reduces %[2]s",
		"in these books a %[1]s comes off %[2]s",
		"here a %[1]s is deducted from %[2]s",
	},
	ConventionNone: {
		"on this account a %[1]s is a memo entry only, so %[2]s is unaffected by it",
		"in these books a %[1]s is tracked separately and leaves %[2]s unchanged",
		"here a %[1]s does not change %[2]s at all",
	},
}

// FamilyV2Case is one generated v2 case plus the metadata the tests use to
// model the cue-reading and fixed-recipe solvers. Only Staged.Case (opaque id
// + question) and Pairs cross the harness wire.
type FamilyV2Case struct {
	Staged StagedCase
	Pairs  []protocol.MemoryPair
	// Shape is the question asked: plain (the standing figure) or direction
	// (raise / lower / unchanged plus the standing figure).
	Shape string
	// Paired marks a member of a counterfactual pair.
	Paired bool
	Unit   FamilyV2Unit
	Cue    FamilyV2Cue
	// Opening, Magnitude, Settled are the printed operands (major units).
	Opening, Magnitude, Settled int
	// Convention is the sign convention the record states.
	Convention string
	// Correct is the graded standing figure in major units.
	Correct int
	// Linked is the counterfactual sibling's Correct (0 otherwise).
	Linked int
}

// Shapes: the question asked of a case.
const (
	familyV2Plain     = "plain"
	familyV2Direction = "direction"
)

// v13FamilyCompilerCaseCount is the bounded share of the v13 memory mix spent
// on family compiler v2: 16 on the full profile (10 money, 6 non-monetary).
func v13FamilyCompilerCaseCount(n int) int {
	switch {
	case n >= 100:
		return 16
	case n >= 40:
		return 8
	default:
		return 4
	}
}

func v13FamilyCompilerRand(seed int64) *rand.Rand {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-family-compiler:%d", seed)
	return rand.New(rand.NewSource(int64(h.Sum64())))
}

func v13FamilyHash(seed int64, salt string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v13-family-compiler:%d:%s", seed, salt)
	return int64(h.Sum64() & ((1 << 63) - 1))
}

func v13FamilyPick(seed int64, salt string, bank []string) string {
	return bank[int(v13FamilyHash(seed, salt)%int64(len(bank)))]
}

// FamilyV2Effect applies a stated convention: the standing figure after the
// cue and the settled amount.
func FamilyV2Effect(convention string, opening, magnitude, settled int) int {
	switch convention {
	case ConventionAdds:
		return opening + magnitude - settled
	case ConventionReduces:
		return opening - magnitude - settled
	default:
		return opening - settled
	}
}

// familyV2Layout is the fixed 16-slot plan for the full profile: four money
// direction singles, three money counterfactual pairs, and three non-monetary
// counterfactual pairs (one of them asked as a direction). Smaller profiles
// take a prefix, so every profile keeps the money-first ordering.
type familyV2Slot struct {
	shape    string // plain | direction (the question asked)
	monetary bool
	paired   bool // member of a counterfactual pair
	variant  bool // second member of a counterfactual pair
}

func familyV2Layout(count int) []familyV2Slot {
	full := []familyV2Slot{
		{familyV2Direction, true, false, false}, {familyV2Direction, true, false, false},
		{familyV2Direction, true, false, false}, {familyV2Direction, true, false, false},
		{familyV2Plain, true, true, false}, {familyV2Plain, true, true, true},
		{familyV2Plain, true, true, false}, {familyV2Plain, true, true, true},
		{familyV2Plain, true, true, false}, {familyV2Plain, true, true, true},
		{familyV2Plain, false, true, false}, {familyV2Plain, false, true, true},
		{familyV2Direction, false, true, false}, {familyV2Direction, false, true, true},
		{familyV2Plain, false, true, false}, {familyV2Plain, false, true, true},
	}
	if count > len(full) {
		count = len(full)
	}
	// Never split a pair: a prefix that ends on a base member drops it.
	if count > 0 && full[count-1].paired && !full[count-1].variant {
		count--
	}
	return full[:count]
}

// BuildFamilyCompilerV13 returns the v13 family compiler v2 cases and the
// memory pairs they seed. Every value is deterministic in seed. The caller
// adds the pairs to wave 0 and the staged cases to the suite.
func BuildFamilyCompilerV13(seed int64, count int) []FamilyV2Case {
	layout := familyV2Layout(count)
	if len(layout) == 0 {
		return nil
	}
	r := v13FamilyCompilerRand(seed)
	out := make([]FamilyV2Case, 0, len(layout))
	ordinal := 0
	name := func() string {
		g := humandata.GivenName(r, ordinal)
		s := humandata.Surname(r, ordinal)
		ordinal++
		return g + " " + s
	}
	convOffset := int(v13FamilyHash(seed, "convention-offset") % 3)
	cueOffset := int(v13FamilyHash(seed, "cue-offset") % 5)
	unitOffset := int(v13FamilyHash(seed, "unit-offset") % int64(len(FamilyV2Units)))
	currencyOffset := int(v13FamilyHash(seed, "currency-offset") % int64(len(FamilyV2Currencies)))

	moneyCues := make([]FamilyV2Cue, 0, 5)
	unitCues := make([]FamilyV2Cue, 0, 5)
	for _, cue := range FamilyV2Cues {
		if cue.Monetary {
			moneyCues = append(moneyCues, cue)
		} else {
			unitCues = append(unitCues, cue)
		}
	}

	var pending *FamilyV2Case // base member awaiting its variant
	moneyIndex, unitIndex := 0, 0
	for i, slot := range layout {
		var unit FamilyV2Unit
		var cue FamilyV2Cue
		if slot.monetary {
			unit = FamilyV2Currencies[(currencyOffset+moneyIndex)%len(FamilyV2Currencies)]
			cue = moneyCues[(cueOffset+moneyIndex)%len(moneyCues)]
			if !slot.variant {
				moneyIndex++
			}
		} else {
			// Non-monetary cues are unit-appropriate (a comp voucher counts
			// nights, a hold release counts seats, ...), so the cue index follows
			// the unit; the stated convention still cycles independently.
			unitSlot := (unitOffset + unitIndex) % len(FamilyV2Units)
			unit = FamilyV2Units[unitSlot]
			cue = unitCues[unitSlot%len(unitCues)]
			if !slot.variant {
				unitIndex++
			}
		}
		// The convention cycles through all three values across the layout so
		// every cue meets every convention over the seeds, and a per-cue
		// fixed mapping is right about a third of the time.
		convention := familyV2Conventions[(convOffset+i)%3]
		subject := name()
		var opening, magnitude, settled int
		if slot.variant && pending != nil {
			// The variant keeps every printed operand and flips exactly one
			// fact: the stated convention.
			opening, magnitude, settled = pending.Opening, pending.Magnitude, pending.Settled
			alt := (familyV2IndexOf(familyV2Conventions, pending.Convention) + 1 + int(v13FamilyHash(seed, fmt.Sprintf("variant-%d", i))%2)) % 3
			convention = familyV2Conventions[alt]
		} else {
			// Every convention's result must be positive and distinct from every
			// printed operand so the answer stays derived and the other-recipe
			// distractors are real.
			for {
				opening, magnitude, settled = drawFamilyV2Operands(r, unit)
				if familyV2OperandsOK(opening, magnitude, settled) {
					break
				}
			}
		}
		correct := FamilyV2Effect(convention, opening, magnitude, settled)

		fc := FamilyV2Case{
			Shape: slot.shape, Paired: slot.paired, Unit: unit, Cue: cue,
			Opening: opening, Magnitude: magnitude, Settled: settled,
			Convention: convention, Correct: correct,
		}
		record := renderFamilyV2Record(seed, i, subject, unit, cue, convention, opening, magnitude, settled)
		questionType := QTRecordQuantityMoney
		if !unit.Monetary {
			questionType = QTRecordQuantityUnits
		}
		switch {
		case slot.paired && slot.variant:
			questionType = QTRecordQuantityCFVariant
		case slot.paired:
			questionType = QTRecordQuantityCFBase
		case slot.shape == familyV2Direction:
			questionType = QTRecordQuantityDirection
		}
		distractors := []int{opening}
		for _, other := range familyV2Conventions {
			if other != convention {
				distractors = append(distractors, FamilyV2Effect(other, opening, magnitude, settled))
			}
		}
		if slot.variant && pending != nil {
			fc.Linked = pending.Correct
			distractors = append(distractors, pending.Correct)
		}
		fc.Staged, fc.Pairs = stageFamilyV2Case(seed, i, questionType, subject, record, fc, distractors)
		out = append(out, fc)

		if slot.paired {
			if slot.variant && pending != nil {
				// Register the variant's answer on the base member.
				base := &out[len(out)-2]
				base.Linked = fc.Correct
				base.Staged.Case.DistractorAnswers = familyV2Distractors(base.Unit, base.Correct, append(familyV2DistractorInts(base), fc.Correct))
				pending = nil
			} else {
				pending = &out[len(out)-1]
			}
		}
	}
	return out
}

func familyV2DistractorInts(fc *FamilyV2Case) []int {
	distractors := []int{fc.Opening}
	for _, other := range familyV2Conventions {
		if other != fc.Convention {
			distractors = append(distractors, FamilyV2Effect(other, fc.Opening, fc.Magnitude, fc.Settled))
		}
	}
	return distractors
}

func familyV2IndexOf(bank []string, value string) int {
	for i, v := range bank {
		if v == value {
			return i
		}
	}
	return 0
}

func drawFamilyV2Operands(r *rand.Rand, unit FamilyV2Unit) (opening, magnitude, settled int) {
	draw := func(lo, span int) int { return lo + r.Intn(span) }
	switch unit.Name {
	case "nights":
		opening, magnitude = draw(8, 14), draw(1, 3)
	case "seats":
		opening, magnitude = draw(60, 180), draw(4, 26)
	case "hours":
		opening, magnitude = draw(60, 140), draw(4, 26)
	case "licences":
		opening, magnitude = draw(30, 90), draw(2, 13)
	case "percentage points":
		opening, magnitude = draw(14, 20), draw(2, 4)
	default: // money, whole major units
		opening, magnitude = draw(600, 2600), draw(40, 560)
	}
	settled = draw(1, opening/3)
	if unit.Name == "percentage points" {
		settled = draw(1, 4)
	}
	return opening, magnitude, settled
}

// familyV2OperandsOK requires every convention's result to be positive and
// distinct from every printed operand and from each other.
func familyV2OperandsOK(opening, magnitude, settled int) bool {
	results := map[int]bool{}
	for _, conv := range familyV2Conventions {
		v := FamilyV2Effect(conv, opening, magnitude, settled)
		if v <= 0 || results[v] || collidesWith(v, opening, magnitude, settled) {
			return false
		}
		results[v] = true
	}
	return true
}

func familyV2Amount(unit FamilyV2Unit, major int) string {
	if unit.Monetary {
		return fmt.Sprintf("%s%d", unit.Symbol, major)
	}
	return fmt.Sprintf("%d %s", major, unit.Name)
}

// renderFamilyV2Record composes the single seeded record: opening figure, the
// cue with its STATED convention, and the settled figure. Sentence forms are
// banks picked per case.
func renderFamilyV2Record(seed int64, i int, subject string, unit FamilyV2Unit, cue FamilyV2Cue, convention string, opening, magnitude, settled int) string {
	salt := fmt.Sprintf("record-%d", i)
	var openingForms, settledForms []string
	switch unit.Name {
	case "nights":
		openingForms = []string{"%[1]s's stay is booked for %[2]s.", "The booking under %[1]s covers %[2]s.", "%[1]s has %[2]s on the reservation."}
		settledForms = []string{"%[1]s of the stay have already been used.", "So far %[1]s have been slept.", "%[1]s are already behind us."}
	case "seats":
		openingForms = []string{"%[1]s's block holds %[2]s.", "The reservation for %[1]s is %[2]s.", "%[1]s reserved %[2]s for the event."}
		settledForms = []string{"%[1]s have already been assigned to names.", "Of those, %[1]s are taken.", "%[1]s are already allocated."}
	case "hours":
		openingForms = []string{"%[1]s's retainer stands at %[2]s.", "The retainer for %[1]s is %[2]s.", "%[1]s holds a retainer of %[2]s."}
		settledForms = []string{"%[1]s have already been invoiced against it.", "So far %[1]s have been billed.", "%[1]s are already used up."}
	case "licences":
		openingForms = []string{"%[1]s's pool is %[2]s.", "The licence pool for %[1]s holds %[2]s.", "%[1]s was granted %[2]s."}
		settledForms = []string{"%[1]s have already been assigned to users.", "Of those, %[1]s are in use.", "%[1]s are already handed out."}
	case "percentage points":
		openingForms = []string{"%[1]s's base discount is %[2]s.", "The discount on %[1]s's account starts at %[2]s.", "%[1]s opens at %[2]s of discount."}
		settledForms = []string{"%[1]s have already been consumed by the early-payment clause.", "%[1]s were used up on the first order.", "The early-payment clause has already taken %[1]s."}
	default:
		openingForms = []string{"%[1]s's invoice stands at %[2]s.", "The invoice on %[1]s's account came to %[2]s.", "%[1]s was billed %[2]s."}
		settledForms = []string{"%[1]s has been paid against it.", "A payment of %[1]s has cleared.", "%[1]s has already been received."}
	}
	openingText := fmt.Sprintf(v13FamilyPick(seed, salt+"-open", openingForms), subject, familyV2Amount(unit, opening))
	cueForms := []string{
		"A %[1]s for %[2]s is on file — %[3]s.",
		"There is a %[1]s of %[2]s on the account; %[3]s.",
		"One %[1]s, %[2]s, sits against it, and %[3]s.",
	}
	conventionText := fmt.Sprintf(v13FamilyPick(seed, salt+"-conv", familyV2ConventionForms[convention]), cue.Phrase, unit.Noun)
	cueText := fmt.Sprintf(v13FamilyPick(seed, salt+"-cue", cueForms), cue.Phrase, familyV2Amount(unit, magnitude), conventionText)
	settledText := fmt.Sprintf(v13FamilyPick(seed, salt+"-settled", settledForms), familyV2Amount(unit, settled))
	lead := v13FamilyPick(seed, salt+"-lead", []string{"Account notes for %s: ", "Notes on %s's account. ", "Filed for %s — "})
	return fmt.Sprintf(lead, subject) + openingText + " " + cueText + " " + settledText
}

var familyV2Openers = []string{
	"Quick bookkeeping question.",
	"Reconciling my records.",
	"Checking one of my accounts.",
	"Sorting out where things stand.",
}

// FamilyV2DirectionAccept is the accept cluster for the three-valued
// "unchanged" direction; increase/decrease use the grader's direction lexicon.
var FamilyV2DirectionAccept = []string{"unchanged", "no change", "stayed the same", "did not change", "left it unchanged", "no effect", "flat", "neither"}

func familyV2DirectionItem(convention string) (item string, kind string, accept []string) {
	switch convention {
	case ConventionAdds:
		return "increase", protocol.AnswerDirection, nil
	case ConventionReduces:
		return "decrease", protocol.AnswerDirection, nil
	default:
		return "unchanged", "", FamilyV2DirectionAccept
	}
}

func familyV2Distractors(unit FamilyV2Unit, correct int, candidates []int) []string {
	scale := 1
	if unit.Monetary {
		scale = 100
	}
	scaled := make([]int, 0, len(candidates))
	for _, c := range candidates {
		scaled = append(scaled, c*scale)
	}
	return centsStrings(dedupeDistractors(correct*scale, scaled))
}

func stageFamilyV2Case(seed int64, i int, questionType, subject, record string, fc FamilyV2Case, distractors []int) (StagedCase, []protocol.MemoryPair) {
	caseID := protocol.OpaqueCaseID(seed, "v13-family-compiler", i)
	pairID := protocol.OpaqueCaseID(seed, "v13-family-compiler-pair", i)
	pair := protocol.MemoryPair{
		PairID:    pairID,
		SessionID: protocol.OpaqueCaseID(seed, "v13-family-compiler-session", i),
		Timestamp: fmt.Sprintf("2026-03-%02dT%02d:%02d:00Z", 2+int(v13FamilyHash(seed, fmt.Sprintf("ts-day-%d", i))%25), 8+int(v13FamilyHash(seed, fmt.Sprintf("ts-hour-%d", i))%10), int(v13FamilyHash(seed, fmt.Sprintf("ts-min-%d", i))%60)),
		Prompt:    record,
		Response:  v13FamilyPick(seed, fmt.Sprintf("ack-%d", i), []string{"Noted — I've filed those account details.", "Got it; I'll read the convention off the record itself.", "Saved with the stated convention."}),
	}
	unit := fc.Unit
	opener := familyV2Openers[int(v13FamilyHash(seed, fmt.Sprintf("opener-%d", i))%int64(len(familyV2Openers)))]
	quantityUnitClause := fmt.Sprintf("Answer in %s.", unit.Name)
	if !unit.Monetary {
		quantityUnitClause = fmt.Sprintf("Answer as a number of %s.", unit.Name)
	}
	mc := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV13,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      questionType,
		DistractorAnswers: familyV2Distractors(unit, fc.Correct, distractors),
		WritingProtected:  []string{subject, fc.Cue.Phrase, unit.Name},
	}
	scale := 1
	answerKind := protocol.AnswerNumber
	if unit.Monetary {
		scale = 100
		answerKind = protocol.AnswerMoney
	}
	quantity := fmt.Sprintf("%d", fc.Correct*scale)
	quantityClaim := protocol.Claim{Kind: protocol.ClaimKindQuantity, Expected: fmt.Sprintf("%d", fc.Correct), Unit: unit.Name, Critical: true, Weight: 1}
	switch fc.Shape {
	case familyV2Direction:
		item, kind, accept := familyV2DirectionItem(fc.Convention)
		var what string
		if unit.Monetary {
			what = fmt.Sprintf("what is currently owed on %s's account", subject)
		} else {
			what = fmt.Sprintf("how many %s remain for %s", unit.Name, subject)
		}
		mc.Question = fmt.Sprintf("%s Did the %s raise, lower, or leave %s unchanged — and %s? %s", opener, fc.Cue.Phrase, unit.Noun, what, quantityUnitClause)
		mc.AnswerKind = protocol.AnswerList
		mc.ExpectedAnswer = item + "; " + quantity
		mc.AnswerItems = []string{item, quantity}
		mc.AnswerItemKinds = []string{kind, answerKind}
		mc.AnswerItemAcceptAny = [][]string{append([]string(nil), accept...), nil}
		// Direction and figure split the credit; neither alone is load-bearing
		// (wrong direction + right figure grades 0.5, see the direction vectors).
		quantityClaim.Weight = 0.5
		quantityClaim.Critical = false
		mc.Claims = []protocol.Claim{
			{Kind: protocol.ClaimKindDirection, Expected: item, Accept: append([]string(nil), accept...), Weight: 0.5},
			quantityClaim,
		}
	default:
		if unit.Monetary {
			mc.Question = fmt.Sprintf("%s What is currently owed on %s's account? %s", opener, subject, quantityUnitClause)
		} else {
			mc.Question = fmt.Sprintf("%s How many %s remain for %s? %s", opener, unit.Name, subject, quantityUnitClause)
		}
		mc.AnswerKind = answerKind
		mc.ExpectedAnswer = quantity
		mc.Claims = []protocol.Claim{quantityClaim}
	}
	return StagedCase{Case: mc, RunAfterWave: 0, RequiredPairIDs: []string{pairID}}, []protocol.MemoryPair{pair}
}

// FamilyV2CueIn reports which bank cue phrase a record carries (the input a
// cue-reading solver keys on). Empty when none matches.
func FamilyV2CueIn(record string) (FamilyV2Cue, bool) {
	lower := strings.ToLower(record)
	for _, cue := range FamilyV2Cues {
		if strings.Contains(lower, cue.Phrase) {
			return cue, true
		}
	}
	return FamilyV2Cue{}, false
}
