package universe

import (
	"fmt"
	"hash/fnv"
	"strconv"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Bench v12 program cases. v12 keeps the v10/v11 machinery that is still the
// right anti-gaming substrate — metamorphic groups, per-seed schema labels,
// independent renderers, prose-only amounts, shuffled record order, relational
// subject binding, no %+d / -> tells — and changes the QUERY PROGRAMS.
//
// v10–v11 (and the first v12 scaffold) scored convert_minor_units of four
// closed money shapes. That paid a request-agnostic cents accumulator. v12
// instead asks about real-world business events: who currently owns the work,
// what status stands, what happened last, who is the client vs vendor, and
// what is next. Remaining-cents arithmetic is a coverage floor, not the
// catalog.
//
// Everything remains deterministic in (seed, group, variant).

const V12ProvenanceRevision = "dittobench-v12-generator-spec-v2"

// ── Expanded, per-seed label superset (issues #492/#499/#537) ─────────────────
//
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

// ── Business-event program catalog ───────────────────────────────────────────

type v12BizShape int

const (
	v12ShapeCurrentOwner v12BizShape = iota
	v12ShapeCurrentStatus
	v12ShapeLastEvent
	v12ShapeCounterparty
	v12ShapeNextAction
	v12ShapeOutstanding // coverage floor: remaining cents after approval minus payment
	v12BizShapeCount
)

type v12BizSchema struct {
	Entity     string
	Alias      string
	Owner      string
	Status     string
	LastEvent  string
	Client     string
	Vendor     string
	NextAction string
	Approved   string
	Paid       string
	Unit       string
	Correction string
}

type v12BizScenario struct {
	Alias         string
	OriginalOwner string
	CurrentOwner  string
	Status        string
	LastEvent     string
	Client        string
	Vendor        string
	NextAction    string
	Draft         int
	Approved      int
	Paid          int
	Unit          string
	DecoyOwner    string
	DecoyClient   string
	DecoyStatus   string
	DecoyEvent    string
}

var v12People = []string{
	"Priya Shah", "Marcus Cole", "Elena Ruiz", "Jonah Hale",
	"Amelia Cho", "Diego Vargas", "Hannah Briggs", "Omar Farouk",
	"Keiko Mori", "Samuel Adeyemi", "Nina Petrov", "Chris Lang",
}

var v12Companies = []string{
	"Northwind Labs", "Harbor Freight Co", "Juniper Media", "Foundry Goods",
	"Cedar & Pine", "Helio Transit", "Kestrel Audit", "Saltline Partners",
	"Orchard Supply", "Nimbus Legal", "Redwood Clinics", "Bramble Design",
}

var v12Statuses = []string{"drafted", "in review", "approved", "paid", "superseded"}

var v12Events = []string{
	"kickoff meeting", "scope approval", "partial payment",
	"internal handoff", "vendor swap",
}

var v12NextActions = []string{
	"send the revised SOW", "book the follow-up", "ping the current owner",
	"close the invoice", "confirm the new vendor",
}

func generateV12BizSchema(seed int64) v12BizSchema {
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
	return v12BizSchema{
		Entity: next("entity"), Alias: next("alias"), Owner: next("owner"),
		Status: next("status"), LastEvent: next("lastevent"), Client: next("client"),
		Vendor: next("vendor"), NextAction: next("next"), Approved: next("approved"),
		Paid: next("paid"), Unit: next("unit"), Correction: next("correction"),
	}
}

func v12BizOntology(schema v12BizSchema) []V10OntologyTerm {
	return []V10OntologyTerm{
		{Semantic: "workstream", Wire: schema.Entity},
		{Semantic: "everyday alias", Wire: schema.Alias},
		{Semantic: "current internal owner", Wire: schema.Owner},
		{Semantic: "current status", Wire: schema.Status},
		{Semantic: "most recent event", Wire: schema.LastEvent},
		{Semantic: "client", Wire: schema.Client},
		{Semantic: "vendor", Wire: schema.Vendor},
		{Semantic: "next action", Wire: schema.NextAction},
		{Semantic: "approved amount", Wire: schema.Approved},
		{Semantic: "settled payment", Wire: schema.Paid},
		{Semantic: "minor-unit convention", Wire: schema.Unit},
		{Semantic: "replaces prior state", Wire: schema.Correction},
	}
}

func v12PickDistinct(seed int64, salt string, bank []string, used map[string]bool) string {
	for i := 0; i < len(bank)*3; i++ {
		candidate := bank[int(v10Seed(seed, fmt.Sprintf("v12:%s:%d", salt, i))%int64(len(bank)))]
		if !used[candidate] {
			used[candidate] = true
			return candidate
		}
	}
	panic("v12 value bank exhausted for " + salt)
}

func v12Answer(shape v12BizShape, s v12BizScenario) string {
	switch shape {
	case v12ShapeCurrentOwner:
		return s.CurrentOwner
	case v12ShapeCurrentStatus:
		return s.Status
	case v12ShapeLastEvent:
		return s.LastEvent
	case v12ShapeCounterparty:
		return s.Client
	case v12ShapeNextAction:
		return s.NextAction
	case v12ShapeOutstanding:
		return fmt.Sprintf("%d", s.Approved-s.Paid)
	default:
		panic("unhandled v12 business shape")
	}
}

func v12BizProgram(shape v12BizShape, schema v12BizSchema) V10QueryNode {
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	latest := func(field string) V10QueryNode {
		return V10QueryNode{Op: "latest", Field: field, Children: []V10QueryNode{resolve}}
	}
	switch shape {
	case v12ShapeCurrentOwner:
		return latest(schema.Owner)
	case v12ShapeCurrentStatus:
		return latest(schema.Status)
	case v12ShapeLastEvent:
		return latest(schema.LastEvent)
	case v12ShapeCounterparty:
		return latest(schema.Client)
	case v12ShapeNextAction:
		return latest(schema.NextAction)
	case v12ShapeOutstanding:
		return V10QueryNode{Op: "convert_minor_units", Children: []V10QueryNode{
			{Op: "subtract", Children: []V10QueryNode{latest(schema.Approved), {Op: "read", Field: schema.Paid}}},
		}}
	default:
		panic("unhandled v12 business shape")
	}
}

func digestV12BizSchema(schema v12BizSchema) (string, error) {
	return digestV10Schema(v10Schema{
		Entity: schema.Entity, Alias: schema.Alias, Draft: schema.Owner,
		Approved: schema.Status, Paid: schema.Paid, Unit: schema.Unit,
		Correction: schema.Correction,
	})
}

// Subject descriptors bind the subject by a relational role — the workstream
// that carries a settled payment — never by its alias and never by a printed
// number. The unrelated decoy workstream carries no settled payment, so this
// description resolves uniquely.
var v12SubjectForms = []string{
	"the workstream in this batch that has a settled payment on record",
	"the entry whose history logs an amount already paid out",
	"the workstream carrying a cleared disbursement alongside its draft",
	"the account in these records that shows a payment already settled",
}

var v12QuestionOpeners = []string{
	"Work only from this batch's own field meanings.",
	"Read the local glossary before naming any field.",
	"Ground every label in this workspace's conventions.",
	"Induce the per-run schema, then answer from the events.",
	"Do not assume standard field names; use the ones defined here.",
}

var v12UnitFrames = []string{
	"Give the result in %s as minor units.",
	"Answer as a minor-unit figure under %s.",
	"Report minor units, per the %s convention.",
	"State the remaining balance in minor units (%s).",
}

var v12AckLeads = []string{
	"Noted.", "Recorded.", "Logged.", "Kept.", "Tracked.",
}

var v12AckBodies = []string{
	"I'll bind each event to its role through this batch's glossary.",
	"Reading parties and status from the prose, not from row position.",
	"Later revisions will govern; I'll take the current owner and status.",
	"These local conventions decide which label means what.",
	"I'll resolve the subject by role rather than by name.",
}

var v12OwnerNouns = []string{"the current internal owner", "who currently owns it on our side", "the standing internal owner after any correction"}
var v12StatusNouns = []string{"the status that currently stands", "where this work currently sits", "the governing status after later revisions"}
var v12EventNouns = []string{"the most recent recorded event", "what happened last on this work", "the latest event in its history"}
var v12ClientNouns = []string{"the client, not the vendor", "which company we are delivering for", "the customer counterparty"}
var v12NextNouns = []string{"what still needs to happen next", "the standing next action", "the open follow-up"}

func v12OperationClause(seed int64, group int, shape v12BizShape, schema v12BizSchema, subject string) string {
	switch shape {
	case v12ShapeCurrentOwner:
		return fmt.Sprintf("who is %s for %s", v12Pick(seed, fmt.Sprintf("opnoun-%d", group), v12OwnerNouns), subject)
	case v12ShapeCurrentStatus:
		return fmt.Sprintf("what is %s on %s", v12Pick(seed, fmt.Sprintf("opnoun-%d", group), v12StatusNouns), subject)
	case v12ShapeLastEvent:
		return fmt.Sprintf("what is %s for %s", v12Pick(seed, fmt.Sprintf("opnoun-%d", group), v12EventNouns), subject)
	case v12ShapeCounterparty:
		return fmt.Sprintf("who is %s on %s", v12Pick(seed, fmt.Sprintf("opnoun-%d", group), v12ClientNouns), subject)
	case v12ShapeNextAction:
		return fmt.Sprintf("what is %s for %s", v12Pick(seed, fmt.Sprintf("opnoun-%d", group), v12NextNouns), subject)
	case v12ShapeOutstanding:
		forms := []string{
			"take the governing %[2]s value for %[1]s and remove the %[3]s amount",
			"start from the standing %[2]s figure on %[1]s, then deduct %[3]s",
			"for %[1]s, reconcile the current %[2]s value against the %[3]s amount",
		}
		return fmt.Sprintf(v12Pick(seed, fmt.Sprintf("op-%d", group), forms), subject, schema.Approved, schema.Paid)
	default:
		panic("unhandled v12 business shape")
	}
}

func v12ScenarioForGroup(seed int64, group int, r *v11Source) v12BizScenario {
	usedPeople := map[string]bool{}
	usedCompanies := map[string]bool{}
	usedStatus := map[string]bool{}
	usedEvents := map[string]bool{}
	usedNext := map[string]bool{}
	s := v12BizScenario{
		Alias:         v12Label(seed, fmt.Sprintf("v12-scenario-%d", group)),
		OriginalOwner: v12PickDistinct(seed, fmt.Sprintf("orig-owner-%d", group), v12People, usedPeople),
		CurrentOwner:  v12PickDistinct(seed, fmt.Sprintf("cur-owner-%d", group), v12People, usedPeople),
		DecoyOwner:    v12PickDistinct(seed, fmt.Sprintf("decoy-owner-%d", group), v12People, usedPeople),
		Status:        v12PickDistinct(seed, fmt.Sprintf("status-%d", group), v12Statuses, usedStatus),
		DecoyStatus:   v12PickDistinct(seed, fmt.Sprintf("decoy-status-%d", group), v12Statuses, usedStatus),
		LastEvent:     v12PickDistinct(seed, fmt.Sprintf("event-%d", group), v12Events, usedEvents),
		DecoyEvent:    v12PickDistinct(seed, fmt.Sprintf("decoy-event-%d", group), v12Events, usedEvents),
		Client:        v12PickDistinct(seed, fmt.Sprintf("client-%d", group), v12Companies, usedCompanies),
		Vendor:        v12PickDistinct(seed, fmt.Sprintf("vendor-%d", group), v12Companies, usedCompanies),
		DecoyClient:   v12PickDistinct(seed, fmt.Sprintf("decoy-client-%d", group), v12Companies, usedCompanies),
		NextAction:    v12PickDistinct(seed, fmt.Sprintf("next-%d", group), v12NextActions, usedNext),
		Draft:         240_000 + r.Intn(2_400_000),
		Paid:          40_000 + r.Intn(160_000),
		Unit:          []string{"USD cents", "CAD cents", "EUR cents"}[r.Intn(3)],
	}
	s.Approved = s.Draft + []int{-95_000, -70_000, -45_000, 30_000, 80_000}[r.Intn(5)]
	if s.Approved <= s.Paid+10_000 {
		s.Approved = s.Paid + 125_000
	}
	return s
}

func v12Mutate(seed int64, group int, shape v12BizShape, base v12BizScenario) (v12BizScenario, error) {
	counter := base
	counter.Alias = base.Alias + "-revision"
	usedPeople := map[string]bool{base.OriginalOwner: true, base.CurrentOwner: true, base.DecoyOwner: true}
	usedCompanies := map[string]bool{base.Client: true, base.Vendor: true, base.DecoyClient: true}
	usedStatus := map[string]bool{base.Status: true, base.DecoyStatus: true}
	usedEvents := map[string]bool{base.LastEvent: true, base.DecoyEvent: true}
	usedNext := map[string]bool{base.NextAction: true}
	switch shape {
	case v12ShapeCurrentOwner:
		counter.CurrentOwner = v12PickDistinct(seed, fmt.Sprintf("cf-owner-%d", group), v12People, usedPeople)
	case v12ShapeCurrentStatus:
		counter.Status = v12PickDistinct(seed, fmt.Sprintf("cf-status-%d", group), v12Statuses, usedStatus)
	case v12ShapeLastEvent:
		counter.LastEvent = v12PickDistinct(seed, fmt.Sprintf("cf-event-%d", group), v12Events, usedEvents)
	case v12ShapeCounterparty:
		counter.Client = v12PickDistinct(seed, fmt.Sprintf("cf-client-%d", group), v12Companies, usedCompanies)
	case v12ShapeNextAction:
		counter.NextAction = v12PickDistinct(seed, fmt.Sprintf("cf-next-%d", group), v12NextActions, usedNext)
	case v12ShapeOutstanding:
		counter.Paid += 25_000 + 12_500*group
		if v12Answer(shape, counter) == v12Answer(shape, base) {
			counter.Approved += 40_000
		}
	}
	if v12Answer(shape, counter) == v12Answer(shape, base) {
		return v12BizScenario{}, fmt.Errorf("v12 causal mutation did not change group %d answer", group)
	}
	if shape == v12ShapeOutstanding {
		left, err := strconv.Atoi(v12Answer(shape, counter))
		if err != nil || left <= 0 {
			return v12BizScenario{}, fmt.Errorf("v12 counterfactual for group %d produced a non-positive remainder", group)
		}
	}
	return counter, nil
}

// GenerateV12Programs returns count scored cases in complete four-member
// metamorphic groups (base, renderer invariant, distractor invariant, causal
// counterfactual). Count must be a positive multiple of four.
func GenerateV12Programs(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v12 program count must be a positive multiple of four, got %d", count)
	}
	schema := generateV12BizSchema(seed)
	ontology := v12BizOntology(schema)
	schemaDigest, err := digestV12BizSchema(schema)
	if err != nil {
		return nil, err
	}
	r := &v11Source{state: uint64(v10Seed(seed, "v12:scenarios"))}
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		shape := v12BizShape(v10Seed(seed, fmt.Sprintf("v12:shape-%d", group)) % int64(v12BizShapeCount))
		scenario := v12ScenarioForGroup(seed, group, r)
		counter, err := v12Mutate(seed, group, shape, scenario)
		if err != nil {
			return nil, err
		}
		groupID := protocol.OpaqueCaseID(seed, "v12-metamorphic-group", group)
		variants := []struct {
			relation string
			answer   string
			scenario v12BizScenario
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
	schema v12BizSchema,
	shape v12BizShape,
	renderer V10Renderer,
	scenario v12BizScenario,
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
	pairs := renderV12Scenario(seed, group, renderer, schema, scenario, pairIDs, includeDistractor)
	answer := v12Answer(shape, scenario)
	if answer == "" {
		return V10GeneratedCase{}, fmt.Errorf("v12 group %d shape %d produced an empty answer", group, shape)
	}

	subject := v12Pick(seed, fmt.Sprintf("subj-%d", group), v12SubjectForms)
	opClause := v12OperationClause(seed, group, shape, schema, subject)
	question := v12Pick(seed, fmt.Sprintf("qopen-%d-%d", group, variant), v12QuestionOpeners) +
		" " + strings.ToUpper(opClause[:1]) + opClause[1:] + "?"
	if shape == v12ShapeOutstanding {
		question = strings.TrimSuffix(question, "?") + ". " +
			fmt.Sprintf(v12Pick(seed, fmt.Sprintf("qunit-%d-%d", group, variant), v12UnitFrames), scenario.Unit)
	}

	kind := protocol.AnswerValue
	if shape == v12ShapeOutstanding {
		kind = protocol.AnswerMoney
	}
	distractors := v12EventDistractors(shape, scenario)

	caseID := protocol.OpaqueCaseID(seed, "v12-program-case", group*4+variant)
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV12,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      "v12-open-program",
		Question:          question,
		ExpectedAnswer:    answer,
		AnswerKind:        kind,
		DistractorAnswers: distractors,
		WritingProtected: []string{
			scenario.Alias, schema.Entity, schema.Alias,
			schema.Owner, schema.Status, schema.Client, scenario.Unit,
		},
	}
	if relation != "causal_counterfactual" {
		caseValue.TwinGroup = groupID
	}
	plan := QuestionPlan{
		Case:            caseValue,
		RequiredPairIDs: append([]string(nil), pairIDs...),
		Facts: []string{
			"per-run schema glossary", "entity alias", "current owner",
			"status", "last event", "client and vendor", "next action", "minor-unit contract",
		},
		Constraints: []string{scenario.Alias, schema.Owner, schema.Status, schema.Client},
		Operations: []string{
			"induce schema", "resolve subject by role", "select governing event field",
			"read operative value from prose", "return the current business fact",
		},
	}
	provenance := V10CaseProvenance{
		Revision:         V12ProvenanceRevision,
		SchemaSHA256:     schemaDigest,
		Ontology:         append([]V10OntologyTerm(nil), ontology...),
		Program:          v12BizProgram(shape, schema),
		Renderer:         renderer,
		MetamorphicGroup: groupID,
		Relation:         relation,
		AnswerRelation:   answerRelation,
		EvidencePairIDs:  append([]string(nil), pairIDs...),
	}
	return V10GeneratedCase{Plan: plan, Pairs: pairs, Provenance: provenance}, nil
}

func v12EventDistractors(shape v12BizShape, scenario v12BizScenario) []string {
	var candidates []string
	switch shape {
	case v12ShapeCurrentOwner:
		candidates = []string{scenario.OriginalOwner, scenario.DecoyOwner, scenario.Client}
	case v12ShapeCurrentStatus:
		candidates = []string{scenario.DecoyStatus, scenario.LastEvent, "drafted"}
	case v12ShapeLastEvent:
		candidates = []string{scenario.DecoyEvent, scenario.Status, scenario.NextAction}
	case v12ShapeCounterparty:
		candidates = []string{scenario.Vendor, scenario.DecoyClient, scenario.CurrentOwner}
	case v12ShapeNextAction:
		candidates = []string{scenario.LastEvent, scenario.Status, "close the invoice"}
	case v12ShapeOutstanding:
		candidates = []string{
			fmt.Sprintf("%d", scenario.Draft-scenario.Paid),
			fmt.Sprintf("%d", scenario.Approved),
			fmt.Sprintf("%d", scenario.Paid),
		}
	}
	answer := v12Answer(shape, scenario)
	seen := map[string]bool{answer: true, "": true}
	out := make([]string, 0, 3)
	for _, c := range candidates {
		if seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, c)
		if len(out) == 3 {
			break
		}
	}
	return out
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

var v12ApprovedForms = []string{
	"On review, the %[2]s for %[1]s was approved at %[3]d %[4]s.",
	"%[1]s's %[2]s was sanctioned at %[3]d %[4]s.",
	"The approved %[2]s standing for %[1]s is %[3]d %[4]s.",
}

func v12EventGlossary(seed int64, group int, schema v12BizSchema) []string {
	clauses := []string{
		fmt.Sprintf("%s names a workstream", schema.Entity),
		fmt.Sprintf("%s is its everyday alias", schema.Alias),
		fmt.Sprintf("%s is the current internal owner after any %s", schema.Owner, schema.Correction),
		fmt.Sprintf("%s is the status that currently stands", schema.Status),
		fmt.Sprintf("%s is the most recent recorded event", schema.LastEvent),
		fmt.Sprintf("%s is the client, not the vendor", schema.Client),
		fmt.Sprintf("%s is the vendor counterparty", schema.Vendor),
		fmt.Sprintf("%s is the standing next action", schema.NextAction),
		fmt.Sprintf("%s is the approved amount", schema.Approved),
		fmt.Sprintf("%s is a settled payment", schema.Paid),
		fmt.Sprintf("%s is the minor-unit convention", schema.Unit),
	}
	rotation := int(v10Seed(seed, fmt.Sprintf("v12:grot-%d", group)) % int64(len(clauses)))
	rotated := append(append([]string(nil), clauses[rotation:]...), clauses[:rotation]...)
	split := len(rotated) / 2
	openers := []string{
		"For this batch of records,",
		"Within this workspace,",
		"Reading guide for the entries that follow:",
		"Local field conventions here:",
		"Before the data lands, note that",
	}
	opener1 := v12Pick(seed, fmt.Sprintf("gopen1-%d", group), openers)
	opener2 := v12Pick(seed, fmt.Sprintf("gopen2-%d", group), openers)
	return []string{
		opener1 + " " + strings.Join(rotated[:split], "; ") + ".",
		opener2 + " " + strings.Join(rotated[split:], "; ") + ".",
	}
}

// renderV12Scenario materializes the six evidence records. Two glossary lines
// lead; the four remaining records — alias binding, parties, events, and money
// context — are emitted in a seeded permutation so no role is bindable by row
// position. No record carries a `label=amount` pair; the only `=` binds the
// entity to its alias.
func renderV12Scenario(seed int64, group int, renderer V10Renderer, schema v12BizSchema, scenario v12BizScenario, pairIDs []string, includeDistractor bool) []protocol.MemoryPair {
	glossary := v12EventGlossary(seed, group, schema)
	alias := scenario.Alias

	binding := fmt.Sprintf("%s=%s; %s=%s", schema.Entity, alias, schema.Alias, alias)
	if includeDistractor {
		binding += fmt.Sprintf(". Separately, an unrelated workstream %s=%s lists %s as owner and %s as client, with an approved figure of %d but no settled payment.",
			schema.Entity, v12Label(seed, fmt.Sprintf("v12-distractor-entity-%d", group)),
			scenario.DecoyOwner, scenario.DecoyClient, scenario.Draft)
	}
	people := fmt.Sprintf(
		"%s opened with %s as %s. A later %s put %s in that role. %s is %s; %s is %s.",
		alias, scenario.OriginalOwner, schema.Owner, schema.Correction, scenario.CurrentOwner,
		schema.Client, scenario.Client, schema.Vendor, scenario.Vendor,
	)
	events := fmt.Sprintf(
		"On %s the most recent %s is %s, the standing %s is %s, and %s is %s.",
		alias, schema.LastEvent, scenario.LastEvent, schema.Status, scenario.Status,
		schema.NextAction, scenario.NextAction,
	)
	money := fmt.Sprintf(v12Pick(seed, fmt.Sprintf("draft-%d", group), v12DraftForms), alias, "draft", scenario.Draft, scenario.Unit) +
		" " + fmt.Sprintf(v12Pick(seed, fmt.Sprintf("appr-%d", group), v12ApprovedForms), alias, schema.Approved, scenario.Approved, scenario.Unit) +
		" " + fmt.Sprintf(v12Pick(seed, fmt.Sprintf("paid-%d", group), v12PaidForms), alias, schema.Paid, scenario.Paid, scenario.Unit)

	movable := []string{binding, people, events, money}
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
		return fmt.Sprintf("Subject: working note %d\nFrom: operations@example.invalid\n\n%s", index+1, row), ack
	case V10TableRenderer:
		return fmt.Sprintf("Pasted table row %d\n| payload |\n|---|\n| %s |", index+1, row), ack
	case V10OpsRenderer:
		return fmt.Sprintf("operations_dump[%d] { %s }", index, strings.ReplaceAll(row, "; ", ", ")), ack
	default:
		panic("unhandled v12 renderer")
	}
}
