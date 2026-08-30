package universe

import (
	"fmt"
	"hash/fnv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v12 program cases. v12 inherits the v11 program semantics wholesale —
// the four sampled query shapes, the metamorphic-group machinery, the per-seed
// schema labels, the renderer classes, and the validator-side provenance are
// identical to v11 (this file reuses v11Answer, v11Program, and the v11Scenario
// / v11Schema shapes). What v12 changes is the SURFACE substrate the two
// harness archetypes measured at the top of the v11 board still gripped:
//
//   - Gap 1 (byte-stable KV ledger). v11 emitted its records in a fixed order
//     with positionally-bindable roles: a harness could read row 5 for the
//     approved value and row 6 for the settled payment without parsing prose.
//     v12 carries every monetary amount inside composed prose (there is no
//     `label=amount` ledger row at all — the only `=` binds an entity to its
//     alias and carries no value), and it shuffles the record order per seed, so
//     role binding requires reading randomized prose rather than counting rows.
//   - Gap 2 (format tells). v11 leaked program shape through a `%+d` sign on the
//     adjustment row and a second `->` on the latest-correction row. v12 never
//     emits `%+d` or `->`: signed adjustments are stated as "raises/lowers by N"
//     over the absolute magnitude, and supersession is stated in prose, so the
//     operative value is interleaved into randomized prose and the shape is not
//     detectable from row structure.
//   - Gap 2b (larger-minus-settled was ~60% free). v11 made the approved figure
//     exceed the draft on most cases, so max==approved==plain-subtract. v12
//     forces the approved figure BELOW the draft for the larger-minus-settled
//     shape, so max genuinely differs from the plain subtract on every such
//     case and the plain-subtract result is a planted distractor.
//   - Gap 3 (even/odd alias leak). v11 bound the subject descriptively only for
//     even groups, and even then by a unique draft NUMBER that also appears
//     verbatim in the rows. v12 binds the subject descriptively for EVERY group,
//     by a relational property (the workstream that carries a settled payment) —
//     a role that resolves through the glossary, never by echoing an alias and
//     never by looking up a value printed in the records.
//
// Everything remains deterministic in (seed, group, variant): the oracle test
// proves a correct reader scores 1.0 and the determinism test proves two
// generations agree byte-for-byte.

const V12ProvenanceRevision = "dittobench-v12-generator-spec-v1"

// Expanded, per-seed label superset.
// v10/v11 labels sample a 24x20 space. v12 inserts a middle syllable bank,
// widening the per-seed label superset to 24*16*20 = 7680 so no fixed dispatch
// table keyed on a label pays across seeds. Generator role names never leak into
// the wire: session identifiers are opaque hashes, not "v12-conversation-3".

var v12LabelMids = []string{
	"a", "e", "i", "o", "u", "ar", "en", "il",
	"or", "ux", "yn", "el", "ad", "ov", "ib", "um",
}

func v12Label(seed int64, salt string) string {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v12-label:%d:%s", seed, salt)
	v := h.Sum64()
	start := v10LabelStarts[v%uint64(len(v10LabelStarts))]
	mid := v12LabelMids[(v/uint64(len(v10LabelStarts)))%uint64(len(v12LabelMids))]
	end := v10LabelEnds[(v/uint64(len(v10LabelStarts))/uint64(len(v12LabelMids)))%uint64(len(v10LabelEnds))]
	return start + mid + end
}

// generateV12Schema mirrors generateV11Schema but draws collision-free labels
// from the widened v12 superset.
func generateV12Schema(seed int64) v11Schema {
	used := map[string]bool{}
	next := func(salt string) string {
		for attempt := 0; ; attempt++ {
			candidate := v12Label(seed, fmt.Sprintf("%s-%d", salt, attempt))
			if !used[candidate] {
				used[candidate] = true
				return candidate
			}
		}
	}
	base := v10Schema{
		Entity: next("entity"), Alias: next("alias"), Draft: next("draft"),
		Approved: next("approved"), Paid: next("paid"), Unit: next("unit"),
		Correction: next("correction"),
	}
	return v11Schema{base: base, Adjustment: next("adjustment")}
}

// ── Compositional surface grammar (v12) ──────────────────────────────────────
//
// As in v11, every list below is a component bank and a surface form is a
// seeded choice from each; no complete sentence is stored, so no literal prefix
// survives two seeds. These banks are v12-specific so no v11 literal carries
// over, and they are frozen with the contract.

func v12Pick(seed int64, salt string, bank []string) string {
	return bank[int(v10Seed(seed, "v12:"+salt)%int64(len(bank)))]
}

// v12Perm returns a seeded Fisher-Yates permutation of [0,n). It is what breaks
// the fixed record order: the operative prose clause lands in a different slot
// each seed, so a positional reader cannot bind roles to rows.
func v12Perm(seed int64, salt string, n int) []int {
	idx := make([]int, n)
	for i := range idx {
		idx[i] = i
	}
	for i := n - 1; i > 0; i-- {
		j := int(v10Seed(seed, fmt.Sprintf("v12:%s:%d", salt, i)) % int64(i+1))
		idx[i], idx[j] = idx[j], idx[i]
	}
	return idx
}

// Subject descriptors bind the subject by a relational role — the workstream
// that carries a settled payment — never by its alias and never by a printed
// number. The unrelated decoy workstream carries only an approved figure and no
// settled payment, so this description resolves uniquely.
var v12SubjectForms = []string{
	"the workstream in this batch that has a settled payment on record",
	"the entry whose history logs an amount already paid out",
	"the workstream carrying a cleared disbursement alongside its draft",
	"the account in these records that shows a payment already settled",
}

var v12DraftForms = []string{
	"The %[2]s first drafted for %[1]s came to %[3]d %[4]s.",
	"For %[1]s, an initial %[2]s of %[3]d %[4]s was drafted.",
	"%[1]s opened with a drafted %[2]s of %[3]d %[4]s.",
}

var v12PaidForms = []string{
	"Against %[1]s, a %[2]s of %[3]d %[4]s has already cleared.",
	"%[1]s has settled a %[2]s of %[3]d %[4]s.",
	"A %[2]s of %[3]d %[4]s was paid out on %[1]s.",
}

// Governing-value clauses per shape. Every value is plain %d in prose; no shape
// emits a %+d sign or a `->` token, so the shape is not byte-detectable.
var v12ApprovedForms = []string{
	"On review, the %[2]s for %[1]s was approved at %[3]d %[4]s.",
	"%[1]s's %[2]s was sanctioned at %[3]d %[4]s.",
	"The approved %[2]s standing for %[1]s is %[3]d %[4]s.",
}

// v12AdjustForms: %[1]=alias %[2]=Approved %[3]=approved val %[4]=unit
// %[5]=Adjustment label %[6]=direction word %[7]=|adjustment|
var v12AdjustForms = []string{
	"The %[2]s for %[1]s was approved at %[3]d %[4]s; a logged %[5]s then %[6]s that figure by %[7]d %[4]s before settlement.",
	"%[1]s's %[2]s was approved at %[3]d %[4]s, after which a recorded %[5]s %[6]s it by %[7]d %[4]s.",
	"For %[1]s the %[2]s stood at %[3]d %[4]s until a %[5]s %[6]s the approved amount by %[7]d %[4]s.",
}

// v12LatestForms: %[1]=alias %[2]=Approved %[3]=first val %[4]=unit
// %[5]=Correction label %[6]=second (governing) val
var v12LatestForms = []string{
	"An earlier note approved %[1]s's %[2]s at %[3]d %[4]s, but under this batch's %[5]s a later revision supersedes it at %[6]d %[4]s.",
	"%[1]s's %[2]s was first put at %[3]d %[4]s; a subsequent %[5]s replaces that, and the standing figure is now %[6]d %[4]s.",
	"For %[1]s the %[2]s once read %[3]d %[4]s, but the newest %[5]s governs: %[6]d %[4]s.",
}

// v12LargerForms: %[1]=alias %[2]=Approved %[3]=approved val %[4]=unit %[5]=Draft label
var v12LargerForms = []string{
	"The %[2]s for %[1]s was approved at %[3]d %[4]s; the governing figure is whichever of the %[5]s and the %[2]s is larger.",
	"%[1]s's %[2]s came in at %[3]d %[4]s — but the amount that stands is the greater of its %[5]s and its %[2]s.",
	"For %[1]s, take whichever is larger, the %[5]s or the %[2]s (approved at %[3]d %[4]s), as the governing figure.",
}

var v12QuestionOpeners = []string{
	"Work only from this batch's own field meanings.",
	"Read the local glossary before naming any field.",
	"Ground every label in this workspace's conventions.",
	"Induce the per-run schema, then compute.",
	"Do not assume standard field names; use the ones defined here.",
}

var v12UnitFrames = []string{
	"Give the result in %s as minor units.",
	"Answer as a minor-unit figure under %s.",
	"Report minor units, per the %s convention.",
	"State the balance in minor units (%s).",
}

var v12AckLeads = []string{
	"Noted.", "Recorded.", "Logged.", "Kept.", "Tracked.",
}

var v12AckBodies = []string{
	"I'll bind each figure to its role through this batch's glossary.",
	"Reading amounts from the prose, not from row position.",
	"Later revisions will govern; I'll take the superseding value.",
	"These local conventions decide which label means what.",
	"I'll resolve the subject by role rather than by name.",
}

// v12CorrectionClause renders the shape-specific governing prose for a scenario.
func v12CorrectionClause(seed int64, group int, shape v11ProgramShape, schema v11Schema, scenario v11Scenario) string {
	b := schema.base
	alias := scenario.base.Alias
	unit := scenario.base.Unit
	switch shape {
	case v11ShapeSubtract:
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("corr-%d", group), v12ApprovedForms),
			alias, b.Approved, scenario.base.Approved, unit)
	case v11ShapeAdjustSubtract:
		dir := "raises"
		mag := scenario.Adjustment
		if mag < 0 {
			dir = "lowers"
			mag = -mag
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("corr-%d", group), v12AdjustForms),
			alias, b.Approved, scenario.base.Approved, unit, schema.Adjustment, dir, mag)
	case v11ShapeLatestCorrection:
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("corr-%d", group), v12LatestForms),
			alias, b.Approved, scenario.base.Approved, unit, b.Correction, scenario.Approved2)
	case v11ShapeLargerMinusSettled:
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("corr-%d", group), v12LargerForms),
			alias, b.Approved, scenario.base.Approved, unit, b.Draft)
	default:
		panic("unhandled v12 program shape")
	}
}

// v12OperationClause describes the sampled program in prose over the descriptive
// subject (never the alias). Wording never repeats a fixed template per seed.
func v12OperationClause(seed int64, group int, shape v11ProgramShape, schema v11Schema, subject string) string {
	b := schema.base
	switch shape {
	case v11ShapeSubtract:
		forms := []string{
			"take the governing %[2]s value for %[1]s and remove the %[3]s amount",
			"start from the standing %[2]s figure on %[1]s, then deduct %[3]s",
			"for %[1]s, reconcile the current %[2]s value against the %[3]s amount",
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid)
	case v11ShapeAdjustSubtract:
		forms := []string{
			"apply the recorded %[4]s to the %[2]s figure on %[1]s, then remove %[3]s",
			"for %[1]s, fold the %[4]s into the %[2]s amount before settling %[3]s against it",
			"adjust %[1]s's %[2]s by its %[4]s, then deduct %[3]s",
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, schema.Adjustment)
	case v11ShapeLatestCorrection:
		forms := []string{
			"more than one %[4]s touches %[1]s's %[2]s; only the most recent governs — deduct %[3]s from it",
			"%[1]s carries a revised %[2]s after a later %[4]s; use the standing value and remove %[3]s",
			"resolve which %[2]s value currently stands for %[1]s, then take away %[3]s",
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, b.Correction)
	case v11ShapeLargerMinusSettled:
		forms := []string{
			"for %[1]s, keep whichever of %[4]s and %[2]s is larger, then deduct %[3]s",
			"compare %[1]s's %[4]s against its %[2]s, retain the greater, and settle %[3]s",
			"the governing figure for %[1]s is the larger of %[4]s and %[2]s; remove %[3]s from it",
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, b.Draft)
	default:
		panic("unhandled v12 program shape")
	}
}

// GenerateV12Programs returns count scored cases in complete four-member
// metamorphic groups (base, renderer invariant, distractor invariant, causal
// counterfactual), like the v10/v11 contracts. Count must be a positive
// multiple of four.
func GenerateV12Programs(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v12 program count must be a positive multiple of four, got %d", count)
	}
	schema := generateV12Schema(seed)
	ontology := append(schemaOntology(schema.base), V10OntologyTerm{
		Semantic: "signed post-approval adjustment", Wire: schema.Adjustment,
	})
	schemaDigest, err := digestV10Schema(schema.base)
	if err != nil {
		return nil, err
	}
	usedDrafts := map[int]bool{}
	r := &v11Source{state: uint64(v10Seed(seed, "v12:scenarios"))}
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		shape := v11ProgramShape(v10Seed(seed, fmt.Sprintf("v12:shape-%d", group)) % int64(v11ShapeCount))
		scenario := v11Scenario{base: v10Scenario{
			Alias: v12Label(seed, fmt.Sprintf("v12-scenario-%d", group)),
			Draft: v11UniqueDraft(r, usedDrafts),
			Paid:  40_000 + r.Intn(160_000),
			Unit:  []string{"USD cents", "CAD cents", "EUR cents"}[r.Intn(3)],
			Decoy: 90_000 + r.Intn(900_000),
		}}
		// Gap 2b: the approved figure now falls BELOW the draft on most cases
		// (four of five offsets are negative), so for the larger-minus-settled
		// shape max(draft, approved)=draft genuinely differs from the plain
		// subtract approved-paid.
		scenario.base.Approved = scenario.base.Draft + []int{-95_000, -70_000, -45_000, -20_000, 30_000}[r.Intn(5)]
		if shape == v11ShapeLargerMinusSettled && scenario.base.Approved >= scenario.base.Draft {
			// Force max=draft for this shape so it is never a free plain subtract.
			scenario.base.Approved = scenario.base.Draft - 55_000
		}
		scenario.Adjustment = []int{-45_000, -20_000, 15_000, 30_000, 55_000}[r.Intn(5)]
		scenario.Approved2 = scenario.base.Approved + []int{-30_000, 22_500, 47_500}[r.Intn(3)]
		if scenario.Approved2 <= scenario.base.Paid+10_000 {
			scenario.Approved2 = scenario.base.Paid + 110_000
		}

		// Every shape's answer must stay a positive money value. For shapes that
		// read the approved/corrected value (subtract, adjust, latest) raise it
		// until the balance clears; the larger-minus-settled shape reads the
		// draft, which always exceeds the payment, so its answer is already
		// positive and its approved<draft invariant is left intact.
		for attempt := 0; v11Answer(shape, scenario) <= 0 && attempt < 64; attempt++ {
			if shape == v11ShapeLargerMinusSettled {
				scenario.base.Paid = 40_000 + r.Intn(120_000)
				continue
			}
			scenario.base.Approved += 150_000
			scenario.Approved2 += 150_000
		}

		counter := scenario
		counter.base.Alias = scenario.base.Alias + "-revision"
		counter.base.Approved += 25_000 + 12_500*group
		counter.Approved2 += 25_000 + 12_500*group
		counter.Adjustment += 10_000
		if shape == v11ShapeLargerMinusSettled {
			// The larger shape ignores approved; mutate the draft (the governing
			// operand) so the counterfactual answer genuinely changes.
			counter.base.Draft += 60_000 + 12_500*group
		}
		if v11Answer(shape, counter) == v11Answer(shape, scenario) {
			counter.base.Paid += 5_000
		}
		if v11Answer(shape, counter) == v11Answer(shape, scenario) {
			return nil, fmt.Errorf("v12 causal mutation did not change group %d answer", group)
		}
		if v11Answer(shape, counter) <= 0 {
			return nil, fmt.Errorf("v12 counterfactual for group %d produced a non-positive answer", group)
		}

		groupID := protocol.OpaqueCaseID(seed, "v12-metamorphic-group", group)
		variants := []struct {
			relation string
			answer   string
			scenario v11Scenario
			distract bool
		}{
			{relation: "base", answer: "base", scenario: scenario},
			{relation: "renderer_invariant", answer: "same", scenario: scenario},
			{relation: "distractor_invariant", answer: "same", scenario: scenario, distract: true},
			{relation: "causal_counterfactual", answer: "changed", scenario: counter},
		}
		for variant, spec := range variants {
			renderer := v10Renderers[(group+variant)%len(v10Renderers)]
			generated, err := materializeV12Case(seed, group, variant, groupID, schemaDigest, ontology, schema, shape, renderer, spec.scenario, spec.relation, spec.answer, spec.distract)
			if err != nil {
				return nil, err
			}
			out = append(out, generated)
		}
	}
	return out, nil
}

func materializeV12Case(
	seed int64,
	group int,
	variant int,
	groupID string,
	schemaDigest string,
	ontology []V10OntologyTerm,
	schema v11Schema,
	shape v11ProgramShape,
	renderer V10Renderer,
	scenario v11Scenario,
	relation string,
	answerRelation string,
	includeDistractor bool,
) (V10GeneratedCase, error) {
	prefix := fmt.Sprintf("v12-%d-%d", group, variant)
	pairIDs := []string{
		protocol.OpaqueCaseID(seed, prefix+"-glossary-a", 0),
		protocol.OpaqueCaseID(seed, prefix+"-glossary-b", 0),
		protocol.OpaqueCaseID(seed, prefix+"-binding", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-a", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-b", 0),
		protocol.OpaqueCaseID(seed, prefix+"-state-c", 0),
	}
	pairs := renderV12Scenario(seed, group, renderer, schema, shape, scenario, pairIDs, includeDistractor)
	answer := v11Answer(shape, scenario)
	if answer <= 0 {
		return V10GeneratedCase{}, fmt.Errorf("v12 group %d shape %d produced non-positive answer %d", group, shape, answer)
	}

	// Gap 3: universal descriptive binding. Every group references the subject
	// by a relational role resolved through the glossary — never by its alias
	// and never by a value printed in the records.
	subject := v12Pick(seed, fmt.Sprintf("subj-%d", group), v12SubjectForms)

	opClause := v12OperationClause(seed, group, shape, schema, subject)
	question := v12Pick(seed, fmt.Sprintf("qopen-%d-%d", group, variant), v12QuestionOpeners) +
		" " + strings.ToUpper(opClause[:1]) + opClause[1:] + ". " +
		fmt.Sprintf(v12Pick(seed, fmt.Sprintf("qunit-%d-%d", group, variant), v12UnitFrames), scenario.base.Unit)

	// Distractors: plant the plain-subtract result (draft-paid), the v10/v11
	// formula (approved-paid), the raw approved figure, and the unrelated decoy.
	// Any value equal to the answer or non-positive is dropped so no distractor
	// ever collides with the graded answer.
	distractors := v12Distractors(answer, scenario)

	caseID := protocol.OpaqueCaseID(seed, "v12-program-case", group*4+variant)
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV12,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      "v12-open-program",
		Question:          question,
		ExpectedAnswer:    fmt.Sprintf("%d", answer),
		AnswerKind:        protocol.AnswerMoney,
		DistractorAnswers: distractors,
		WritingProtected: []string{
			scenario.base.Alias, schema.base.Entity, schema.base.Alias,
			schema.base.Approved, schema.base.Paid, schema.Adjustment, scenario.base.Unit,
		},
	}
	if relation != "causal_counterfactual" {
		caseValue.TwinGroup = groupID
	}
	plan := QuestionPlan{
		Case:            caseValue,
		RequiredPairIDs: append([]string(nil), pairIDs...),
		Facts: []string{
			"per-run schema glossary", "entity alias", "draft amount",
			"approved correction", "signed adjustment", "settled payment", "minor-unit contract",
		},
		Constraints: []string{scenario.base.Alias, schema.base.Approved, schema.base.Paid, scenario.base.Unit},
		Operations: []string{
			"induce schema", "resolve subject by role", "select governing program",
			"read operative value from prose", "evaluate program", "return minor units",
		},
	}
	provenance := V10CaseProvenance{
		Revision:         V12ProvenanceRevision,
		SchemaSHA256:     schemaDigest,
		Ontology:         append([]V10OntologyTerm(nil), ontology...),
		Program:          v11Program(shape, schema),
		Renderer:         renderer,
		MetamorphicGroup: groupID,
		Relation:         relation,
		AnswerRelation:   answerRelation,
		EvidencePairIDs:  append([]string(nil), pairIDs...),
	}
	return V10GeneratedCase{Plan: plan, Pairs: pairs, Provenance: provenance}, nil
}

// v12Distractors builds a collision-free, positive distractor set.
func v12Distractors(answer int, scenario v11Scenario) []string {
	candidates := []int{
		scenario.base.Draft - scenario.base.Paid,    // plain subtract over the draft
		scenario.base.Approved - scenario.base.Paid, // the v10/v11 approved-minus-paid formula
		scenario.base.Approved,                      // the raw approved figure
		scenario.base.Decoy,                         // an unrelated workstream's figure
	}
	seen := map[int]bool{answer: true}
	out := make([]string, 0, len(candidates))
	for _, c := range candidates {
		if c <= 0 || seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, fmt.Sprintf("%d", c))
	}
	return out
}

// renderV12Scenario materializes the six evidence records. Two glossary lines
// lead; the four remaining records — the alias binding plus three amount prose
// clauses — are emitted in a seeded permutation so no role is bindable by row
// position. No record carries a `label=amount` pair; the only `=` binds the
// entity to its alias.
func renderV12Scenario(seed int64, group int, renderer V10Renderer, schema v11Schema, shape v11ProgramShape, scenario v11Scenario, pairIDs []string, includeDistractor bool) []protocol.MemoryPair {
	b := schema.base
	glossary := v11Glossary(seed, group, schema)
	alias := scenario.base.Alias
	unit := scenario.base.Unit

	binding := fmt.Sprintf("%s=%s; %s=%s", b.Entity, alias, b.Alias, alias)
	if includeDistractor {
		// The unrelated workstream carries only an approved figure and NO settled
		// payment, so the relational subject descriptor still resolves uniquely.
		binding += fmt.Sprintf(". Separately, an unrelated workstream %s=%s shows an approved figure of %d but no settled payment.",
			b.Entity, v12Label(seed, fmt.Sprintf("v12-distractor-entity-%d", group)), scenario.base.Decoy)
	}
	draftClause := fmt.Sprintf(v12Pick(seed, fmt.Sprintf("draft-%d", group), v12DraftForms), alias, b.Draft, scenario.base.Draft, unit)
	paidClause := fmt.Sprintf(v12Pick(seed, fmt.Sprintf("paid-%d", group), v12PaidForms), alias, b.Paid, scenario.base.Paid, unit)
	correctionClause := v12CorrectionClause(seed, group, shape, schema, scenario)

	// Slots 2..5 hold these four records; their order is shuffled per seed.
	movable := []string{binding, draftClause, paidClause, correctionClause}
	perm := v12Perm(seed, fmt.Sprintf("rows-%d", group), len(movable))
	rows := []string{glossary[0], glossary[1]}
	for _, p := range perm {
		rows = append(rows, movable[p])
	}

	pairs := make([]protocol.MemoryPair, 0, len(rows))
	for i, row := range rows {
		prompt, response := renderV12Record(seed, group, renderer, i, row, alias)
		pairs = append(pairs, protocol.MemoryPair{
			PairID: pairIDs[i],
			// Opaque session identifiers: no renderer family or version leaks
			// into the wire (issues #492/#499/#537).
			SessionID: protocol.OpaqueCaseID(seed, fmt.Sprintf("v12-session-%d", group), i),
			Timestamp: fmt.Sprintf("2026-01-%02dT%02d:00:00Z", 2+i, 9+i),
			Prompt:    prompt, Response: response,
		})
	}
	return pairs
}

func renderV12Record(seed int64, group int, renderer V10Renderer, index int, row, alias string) (string, string) {
	ack := v12Pick(seed, fmt.Sprintf("acklead-%d-%d", group, index), v12AckLeads) + " " +
		v12Pick(seed, fmt.Sprintf("ackbody-%d-%d", group, index), v12AckBodies) +
		" (thread: " + alias + ")"
	switch renderer {
	case V10ConversationRenderer:
		leads := []string{
			"One more line from our custom workspace schema: ",
			"Adding a workspace record — read it with our local glossary: ",
			"Here is another entry for the batch: ",
		}
		return v12Pick(seed, fmt.Sprintf("convlead-%d-%d", group, index), leads) + row, ack
	case V10EmailRenderer:
		return fmt.Sprintf("Subject: reconciliation note %d\nFrom: operations@example.invalid\n\n%s", index+1, row), ack
	case V10TableRenderer:
		return fmt.Sprintf("Pasted table row %d\n| payload |\n|---|\n| %s |", index+1, row), ack
	case V10OpsRenderer:
		return fmt.Sprintf("operations_dump[%d] { %s }", index, strings.ReplaceAll(row, "; ", ", ")), ack
	default:
		panic("unhandled v12 renderer")
	}
}
