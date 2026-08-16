package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v11 program cases. The metamorphic-group machinery, per-seed schema
// labels, renderer classes, and validator-side provenance are inherited from
// the v10 contract. What changes is everything the measured v10 rule engines
// gripped:
//
//   - the query program is sampled per group from several shapes instead of
//     always approved-minus-paid, and its prose description is composed from a
//     seeded grammar, so a harness cannot hardcode one formula;
//   - the glossary is split across records and phrased by seeded
//     recombination, so no stable literal prefix identifies a field's role;
//   - half the groups bind the subject descriptively (by a unique
//     evidence-derived draft amount) instead of echoing the alias;
//   - assistant acknowledgements are composed per record instead of drawn
//     from a fixed pair of strings.
//
// Everything remains deterministic in (seed, group, variant): the oracle test
// proves a correct reader scores 1.0 and the determinism test proves two
// generations agree byte-for-byte.

const V11ProvenanceRevision = "dittobench-v11-generator-spec-v1"

// v11ProgramShape enumerates the sampled query programs. Each shape defines
// the extra evidence it plants and the integer answer it expects.
type v11ProgramShape int

const (
	// v11ShapeSubtract is the v10 balance: latest approved minus settled.
	v11ShapeSubtract v11ProgramShape = iota
	// v11ShapeAdjustSubtract adds a signed adjustment before settling.
	v11ShapeAdjustSubtract
	// v11ShapeLatestCorrection plants two corrections; only the later one
	// governs. A harness replaying "first correction wins" fails.
	v11ShapeLatestCorrection
	// v11ShapeLargerMinusSettled takes the larger of draft and approved
	// before settling, so blind replacement fails when approval lowered it.
	v11ShapeLargerMinusSettled
	v11ShapeCount
)

// v11Scenario extends the v10 scenario with the extra state the new program
// shapes read.
type v11Scenario struct {
	base       v10Scenario
	Adjustment int // signed, used by v11ShapeAdjustSubtract
	Approved2  int // second correction, used by v11ShapeLatestCorrection
}

func v11Answer(shape v11ProgramShape, s v11Scenario) int {
	switch shape {
	case v11ShapeSubtract:
		return s.base.Approved - s.base.Paid
	case v11ShapeAdjustSubtract:
		return s.base.Approved + s.Adjustment - s.base.Paid
	case v11ShapeLatestCorrection:
		return s.Approved2 - s.base.Paid
	case v11ShapeLargerMinusSettled:
		larger := s.base.Approved
		if s.base.Draft > larger {
			larger = s.base.Draft
		}
		return larger - s.base.Paid
	default:
		panic("unhandled v11 program shape")
	}
}

func v11Program(shape v11ProgramShape, schema v11Schema) V10QueryNode {
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.base.Alias}
	read := func(field string) V10QueryNode {
		return V10QueryNode{Op: "read", Field: field}
	}
	latest := func(field string) V10QueryNode {
		return V10QueryNode{Op: "latest", Field: field, Children: []V10QueryNode{resolve}}
	}
	wrap := func(inner V10QueryNode) V10QueryNode {
		return V10QueryNode{Op: "convert_minor_units", Children: []V10QueryNode{inner}}
	}
	switch shape {
	case v11ShapeSubtract:
		return wrap(V10QueryNode{Op: "subtract", Children: []V10QueryNode{latest(schema.base.Approved), read(schema.base.Paid)}})
	case v11ShapeAdjustSubtract:
		return wrap(V10QueryNode{Op: "subtract", Children: []V10QueryNode{
			{Op: "add", Children: []V10QueryNode{latest(schema.base.Approved), read(schema.Adjustment)}},
			read(schema.base.Paid),
		}})
	case v11ShapeLatestCorrection:
		return wrap(V10QueryNode{Op: "subtract", Children: []V10QueryNode{latest(schema.base.Approved), read(schema.base.Paid)}})
	case v11ShapeLargerMinusSettled:
		return wrap(V10QueryNode{Op: "subtract", Children: []V10QueryNode{
			{Op: "max", Children: []V10QueryNode{read(schema.base.Draft), latest(schema.base.Approved)}},
			read(schema.base.Paid),
		}})
	default:
		panic("unhandled v11 program shape")
	}
}

// v11Schema extends the per-seed v10 schema with the labels the new shapes
// need. Labels stay seed-scoped and collision-free.
type v11Schema struct {
	base       v10Schema
	Adjustment string
}

func generateV11Schema(seed int64) v11Schema {
	base := generateV10Schema(seed)
	used := map[string]bool{
		base.Entity: true, base.Alias: true, base.Draft: true,
		base.Approved: true, base.Paid: true, base.Unit: true, base.Correction: true,
	}
	adjustment := ""
	for attempt := 0; ; attempt++ {
		candidate := v10Label(seed, fmt.Sprintf("v11-adjustment-%d", attempt))
		if !used[candidate] {
			adjustment = candidate
			break
		}
	}
	return v11Schema{base: base, Adjustment: adjustment}
}

// ── Compositional surface grammar ────────────────────────────────────────────
//
// Every list below is a component bank; a surface form is a seeded choice from
// each bank. There is deliberately no complete sentence stored anywhere, so no
// literal prefix survives two seeds. Component banks are frozen with the
// contract: enlarging them later would change bytes and therefore requires a
// new bench version.

var v11GlossaryOpeners = []string{
	"For this batch of records,",
	"Within this workspace,",
	"Reading guide for the entries that follow:",
	"Local field conventions here:",
	"Before the data lands, note that",
	"Schema note for this reconciliation set:",
}

var v11NamesVerbs = []string{"names", "designates", "labels", "identifies", "marks"}
var v11AliasNouns = []string{"everyday alias", "working shorthand", "informal handle", "day-to-day name"}
var v11DraftNouns = []string{"an initial drafted amount", "a preliminary figure", "an early working amount"}
var v11ApprovedNouns = []string{
	"the amount that later replaces the draft once approved",
	"the sanctioned replacement figure",
	"the value that supersedes the draft on approval",
}
var v11PaidNouns = []string{"a settled payment", "an amount already paid out", "a disbursement that has cleared"}
var v11UnitNouns = []string{
	"the minor-unit convention in force",
	"which minor currency unit applies",
	"the unit every figure is quoted in",
}
var v11CorrectionNouns = []string{
	"that the later value supersedes the earlier one",
	"a replacement of prior state, newest wins",
	"that whatever comes later overrides what came before",
}
var v11AdjustmentNouns = []string{
	"a signed adjustment applied on top of the approved figure",
	"a post-approval correction added to the approved amount",
	"an increment or decrement layered onto the approval",
}

var v11QuestionOpeners = []string{
	"Work from this batch's own field meanings.",
	"Interpret the local labels; do not guess standard names.",
	"Use the conventions established for these records.",
	"Ground every field in this workspace's glossary.",
	"Apply the per-run schema before any arithmetic.",
}

var v11UnitFrames = []string{
	"Give the result in %s as minor units.",
	"Answer as a minor-unit figure under %s.",
	"Report minor units, per the %s convention.",
	"State the balance in minor units (%s).",
}

var v11AckLeads = []string{
	"Logged.", "Filed.", "Captured.", "On record.", "Saved.",
}

var v11AckBodies = []string{
	"I'll read those labels through this workspace's own glossary.",
	"Field meanings for this batch noted and pinned.",
	"I'll treat later replacement markers as authoritative.",
	"These per-run conventions will govern my reading.",
	"I'll resolve the labels locally rather than by habit.",
}

func v11Pick(seed int64, salt string, bank []string) string {
	return bank[int(v10Seed(seed, "v11:"+salt)%int64(len(bank)))]
}

// v11Glossary renders the schema's semantics as two seeded sentences with no
// stable prefix, ordered by a seeded rotation.
func v11Glossary(seed int64, group int, schema v11Schema) []string {
	b := schema.base
	clauses := []string{
		fmt.Sprintf("%s %s a workstream", b.Entity, v11Pick(seed, fmt.Sprintf("gentity-%d", group), v11NamesVerbs)),
		fmt.Sprintf("%s is its %s", b.Alias, v11Pick(seed, fmt.Sprintf("galias-%d", group), v11AliasNouns)),
		fmt.Sprintf("%s carries %s", b.Draft, v11Pick(seed, fmt.Sprintf("gdraft-%d", group), v11DraftNouns)),
		fmt.Sprintf("%s is %s", b.Approved, v11Pick(seed, fmt.Sprintf("gappr-%d", group), v11ApprovedNouns)),
		fmt.Sprintf("%s records %s", b.Paid, v11Pick(seed, fmt.Sprintf("gpaid-%d", group), v11PaidNouns)),
		fmt.Sprintf("%s states %s", b.Unit, v11Pick(seed, fmt.Sprintf("gunit-%d", group), v11UnitNouns)),
		fmt.Sprintf("%s means %s", b.Correction, v11Pick(seed, fmt.Sprintf("gcorr-%d", group), v11CorrectionNouns)),
		fmt.Sprintf("%s denotes %s", schema.Adjustment, v11Pick(seed, fmt.Sprintf("gadj-%d", group), v11AdjustmentNouns)),
	}
	rotation := int(v10Seed(seed, fmt.Sprintf("v11:grot-%d", group)) % int64(len(clauses)))
	rotated := append(append([]string(nil), clauses[rotation:]...), clauses[:rotation]...)
	split := len(rotated) / 2
	opener1 := v11Pick(seed, fmt.Sprintf("gopen1-%d", group), v11GlossaryOpeners)
	opener2 := v11Pick(seed, fmt.Sprintf("gopen2-%d", group), v11GlossaryOpeners)
	return []string{
		opener1 + " " + strings.Join(rotated[:split], "; ") + ".",
		opener2 + " " + strings.Join(rotated[split:], "; ") + ".",
	}
}

// v11OperationClause describes the sampled program in prose composed for this
// (seed, group). The wording never repeats a fixed template across seeds.
func v11OperationClause(seed int64, group int, shape v11ProgramShape, schema v11Schema, subject string) string {
	b := schema.base
	switch shape {
	case v11ShapeSubtract:
		forms := []string{
			"take the governing %[2]s value for %[1]s and remove the %[3]s amount",
			"start from the current %[2]s figure on %[1]s, then deduct %[3]s",
			"reconcile %[1]s: the standing %[2]s value less the %[3]s amount",
		}
		return fmt.Sprintf(v11Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid)
	case v11ShapeAdjustSubtract:
		forms := []string{
			"combine the standing %[2]s value for %[1]s with its %[4]s, then deduct %[3]s",
			"apply the recorded %[4]s to the %[2]s figure on %[1]s before removing %[3]s",
			"for %[1]s, fold the %[4]s into the %[2]s amount and settle %[3]s against it",
		}
		return fmt.Sprintf(v11Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, schema.Adjustment)
	case v11ShapeLatestCorrection:
		forms := []string{
			"more than one %[4]s entry touches %[1]s's %[2]s; only the most recent governs — deduct %[3]s from it",
			"%[1]s carries a revised %[2]s after a second %[4]s; use the latest and remove %[3]s",
			"resolve which %[2]s value currently stands for %[1]s (its %[4]s history has two entries), then take away %[3]s",
		}
		return fmt.Sprintf(v11Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, b.Correction)
	case v11ShapeLargerMinusSettled:
		forms := []string{
			"for %[1]s, keep whichever of %[4]s and %[2]s is larger, then deduct %[3]s",
			"compare %[1]s's %[4]s against its %[2]s, retain the greater, and settle %[3]s",
			"the governing figure for %[1]s is the larger of %[4]s and %[2]s; remove %[3]s from it",
		}
		return fmt.Sprintf(v11Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, b.Approved, b.Paid, b.Draft)
	default:
		panic("unhandled v11 program shape")
	}
}

// GenerateV11Programs returns count scored cases in complete four-member
// metamorphic groups (base, renderer invariant, distractor invariant, causal
// counterfactual), like the v10 contract. Count must be a positive multiple of
// four.
func GenerateV11Programs(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v11 program count must be a positive multiple of four, got %d", count)
	}
	schema := generateV11Schema(seed)
	ontology := append(schemaOntology(schema.base), V10OntologyTerm{
		Semantic: "signed post-approval adjustment", Wire: schema.Adjustment,
	})
	schemaDigest, err := digestV10Schema(schema.base)
	if err != nil {
		return nil, err
	}
	usedDrafts := map[int]bool{}
	r := v11Rand(seed)
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		shape := v11ProgramShape(v10Seed(seed, fmt.Sprintf("v11:shape-%d", group)) % int64(v11ShapeCount))
		scenario := v11Scenario{base: v10Scenario{
			Alias: v10Label(seed, fmt.Sprintf("v11-scenario-%d", group)),
			Draft: v11UniqueDraft(r, usedDrafts),
			Paid:  40_000 + r.Intn(160_000),
			Unit:  []string{"USD cents", "CAD cents", "EUR cents"}[r.Intn(3)],
			Decoy: 90_000 + r.Intn(900_000),
		}}
		scenario.base.Approved = scenario.base.Draft + []int{-37_500, -12_500, 18_750, 42_500, 67_500}[r.Intn(5)]
		if scenario.base.Approved <= scenario.base.Paid+25_000 {
			scenario.base.Approved = scenario.base.Paid + 125_000
		}
		scenario.Adjustment = []int{-45_000, -20_000, 15_000, 30_000, 55_000}[r.Intn(5)]
		scenario.Approved2 = scenario.base.Approved + []int{-30_000, 22_500, 47_500}[r.Intn(3)]
		if scenario.Approved2 <= scenario.base.Paid+10_000 {
			scenario.Approved2 = scenario.base.Paid + 110_000
		}

		// Program answers must stay positive money values under every shape;
		// raise the corrected figures until they clear the settled amount.
		for attempt := 0; v11Answer(shape, scenario) <= 0 && attempt < 64; attempt++ {
			scenario.base.Approved += 150_000
			scenario.Approved2 += 150_000
		}

		counter := scenario
		counter.base.Alias = scenario.base.Alias + "-revision"
		counter.base.Approved += 25_000 + 12_500*group
		counter.Approved2 += 25_000 + 12_500*group
		counter.Adjustment += 10_000
		if v11Answer(shape, counter) == v11Answer(shape, scenario) {
			counter.base.Paid += 5_000
		}
		if v11Answer(shape, counter) == v11Answer(shape, scenario) {
			return nil, fmt.Errorf("v11 causal mutation did not change group %d answer", group)
		}
		if v11Answer(shape, counter) <= 0 {
			return nil, fmt.Errorf("v11 counterfactual for group %d produced a non-positive answer", group)
		}

		groupID := protocol.OpaqueCaseID(seed, "v11-metamorphic-group", group)
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
			generated, err := materializeV11Case(seed, group, variant, groupID, schemaDigest, ontology, schema, shape, renderer, spec.scenario, spec.relation, spec.answer, spec.distract)
			if err != nil {
				return nil, err
			}
			out = append(out, generated)
		}
	}
	return out, nil
}

func v11UniqueDraft(r *v11Source, used map[int]bool) int {
	for {
		draft := 240_000 + r.Intn(2_400_000)
		if !used[draft] {
			used[draft] = true
			return draft
		}
	}
}

func materializeV11Case(
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
	prefix := fmt.Sprintf("v11-%d-%d", group, variant)
	pairIDs := []string{
		protocol.OpaqueCaseID(seed, prefix+"-glossary-a", 0),
		protocol.OpaqueCaseID(seed, prefix+"-glossary-b", 0),
		protocol.OpaqueCaseID(seed, prefix+"-context", 0),
		protocol.OpaqueCaseID(seed, prefix+"-ledger", 0),
		protocol.OpaqueCaseID(seed, prefix+"-correction", 0),
		protocol.OpaqueCaseID(seed, prefix+"-payment", 0),
	}
	pairs := renderV11Scenario(seed, group, renderer, schema, shape, scenario, pairIDs, includeDistractor)
	answer := v11Answer(shape, scenario)
	if answer <= 0 {
		return V10GeneratedCase{}, fmt.Errorf("v11 group %d shape %d produced non-positive answer %d", group, shape, answer)
	}

	// Descriptive entity binding: even groups reference the subject by its
	// unique draft amount instead of echoing the alias, so alias string-match
	// alone cannot locate the records.
	subject := scenario.base.Alias
	if group%2 == 0 {
		subject = fmt.Sprintf("the workstream whose %s was recorded as %d", schema.base.Draft, scenario.base.Draft)
	}

	question := v11Pick(seed, fmt.Sprintf("qopen-%d-%d", group, variant), v11QuestionOpeners) +
		" " + strings.ToUpper(string([]rune(v11OperationClause(seed, group, shape, schema, subject))[0:1])) +
		v11OperationClause(seed, group, shape, schema, subject)[1:] + ". " +
		fmt.Sprintf(v11Pick(seed, fmt.Sprintf("qunit-%d-%d", group, variant), v11UnitFrames), scenario.base.Unit)

	distractors := []string{
		fmt.Sprintf("%d", scenario.base.Draft-scenario.base.Paid),
		fmt.Sprintf("%d", scenario.base.Approved),
		fmt.Sprintf("%d", scenario.base.Decoy),
	}
	if alt := v11Answer(v11ShapeSubtract, scenario); alt != answer {
		// The v10 formula's result is always a distractor for non-subtract
		// shapes: a harness replaying the v10 program picks it.
		distractors[0] = fmt.Sprintf("%d", alt)
	}

	caseID := protocol.OpaqueCaseID(seed, "v11-program-case", group*4+variant)
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV11,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      "v11-open-program",
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
			"induce schema", "resolve entity binding", "select governing program",
			"evaluate program over latest state", "return minor units",
		},
	}
	provenance := V10CaseProvenance{
		Revision:         V11ProvenanceRevision,
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

func renderV11Scenario(seed int64, group int, renderer V10Renderer, schema v11Schema, shape v11ProgramShape, scenario v11Scenario, pairIDs []string, includeDistractor bool) []protocol.MemoryPair {
	b := schema.base
	glossary := v11Glossary(seed, group, schema)
	rows := []string{
		glossary[0],
		glossary[1],
		fmt.Sprintf("%s=%s; %s=%s", b.Entity, scenario.base.Alias, b.Alias, scenario.base.Alias),
		fmt.Sprintf("%s=%s; %s=%d; %s=%s", b.Alias, scenario.base.Alias, b.Draft, scenario.base.Draft, b.Unit, scenario.base.Unit),
		fmt.Sprintf("%s=%s; %s=%s->%s; %s=%d", b.Alias, scenario.base.Alias, b.Correction, b.Draft, b.Approved, b.Approved, scenario.base.Approved),
		fmt.Sprintf("%s=%s; %s=%d", b.Alias, scenario.base.Alias, b.Paid, scenario.base.Paid),
	}
	switch shape {
	case v11ShapeAdjustSubtract:
		rows[4] += fmt.Sprintf("; %s=%+d", schema.Adjustment, scenario.Adjustment)
	case v11ShapeLatestCorrection:
		// The second correction lands in the payment record's row so the
		// governing value is established strictly later than the first.
		rows[5] = fmt.Sprintf("%s=%s; %s=%s->%s; %s=%d; %s=%d",
			b.Alias, scenario.base.Alias, b.Correction, b.Approved, b.Approved,
			b.Approved, scenario.Approved2, b.Paid, scenario.base.Paid)
	}
	if includeDistractor {
		rows[2] += fmt.Sprintf("; unrelated %s=%s, %s=%d", b.Entity, v10Label(seed, "v11-distractor-entity"), b.Approved, scenario.base.Decoy)
	}
	pairs := make([]protocol.MemoryPair, 0, len(rows))
	for i, row := range rows {
		prompt, response := renderV11Record(seed, group, renderer, i, row, scenario.base.Alias)
		pairs = append(pairs, protocol.MemoryPair{
			PairID: pairIDs[i], SessionID: fmt.Sprintf("v11-%s-%d", renderer, i),
			Timestamp: fmt.Sprintf("2026-01-%02dT%02d:00:00Z", 2+i, 9+i),
			Prompt:    prompt, Response: response,
		})
	}
	return pairs
}

func renderV11Record(seed int64, group int, renderer V10Renderer, index int, row, alias string) (string, string) {
	ack := v11Pick(seed, fmt.Sprintf("acklead-%d-%d", group, index), v11AckLeads) + " " +
		v11Pick(seed, fmt.Sprintf("ackbody-%d-%d", group, index), v11AckBodies) +
		" (thread: " + alias + ")"
	switch renderer {
	case V10ConversationRenderer:
		leads := []string{
			"One more line from our custom workspace schema: ",
			"Adding a workspace record — read it with our local glossary: ",
			"Here is another entry for the batch: ",
		}
		return v11Pick(seed, fmt.Sprintf("convlead-%d-%d", group, index), leads) + row, ack
	case V10EmailRenderer:
		return fmt.Sprintf("Subject: reconciliation note %d\nFrom: operations@example.invalid\n\n%s", index+1, row), ack
	case V10TableRenderer:
		return fmt.Sprintf("Pasted table row %d\n| payload |\n|---|\n| %s |", index+1, row), ack
	case V10OpsRenderer:
		return fmt.Sprintf("operations_dump[%d] { %s }", index, strings.ReplaceAll(row, "; ", ", ")), ack
	default:
		panic("unhandled v11 renderer")
	}
}

// ── Seeded randomness ────────────────────────────────────────────────────────

// v11Source is a tiny deterministic generator (splitmix-style) so v11 does not
// share v10's rand stream and the two contracts stay independent.
type v11Source struct{ state uint64 }

func v11Rand(seed int64) *v11Source {
	return &v11Source{state: uint64(v10Seed(seed, "v11:scenarios"))}
}

func (s *v11Source) next() uint64 {
	s.state += 0x9E3779B97F4A7C15
	z := s.state
	z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
	z = (z ^ (z >> 27)) * 0x94D049BB133111EB
	return z ^ (z >> 31)
}

func (s *v11Source) Intn(n int) int {
	return int(s.next() % uint64(n))
}
