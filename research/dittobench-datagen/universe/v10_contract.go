package universe

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V10Renderer is a public renderer class. The renderer name is retained only
// in the validator-side artifact provenance; the submitted harness receives the
// rendered records without a family label.
type V10Renderer string

const (
	V10ConversationRenderer V10Renderer = "conversation"
	V10EmailRenderer        V10Renderer = "email"
	V10TableRenderer        V10Renderer = "table"
	V10OpsRenderer          V10Renderer = "operations_dump"
)

// V10OntologyTerm maps a stable semantic role to its seed-scoped wire label.
// Semantics are explained inside the generated records so a general agent can
// induce the schema at run time. The mapping never crosses /seed as metadata.
type V10OntologyTerm struct {
	Semantic string `json:"semantic"`
	Wire     string `json:"wire"`
}

// V10QueryNode is a recursive, deterministic query program. Leaves read a
// seed-scoped field; internal nodes compose state selection, arithmetic, and
// output conversion. It is audit provenance, not a prescribed tool trace.
type V10QueryNode struct {
	Op       string         `json:"op"`
	Field    string         `json:"field,omitempty"`
	Children []V10QueryNode `json:"children,omitempty"`
}

// V10CaseProvenance binds a scored case to the latent contract that generated
// it. DatasetArtifact hashes this value, while BuildHarnessProjection omits it.
type V10CaseProvenance struct {
	Revision         string            `json:"revision"`
	SchemaSHA256     string            `json:"schema_sha256"`
	Ontology         []V10OntologyTerm `json:"ontology"`
	Program          V10QueryNode      `json:"program"`
	Renderer         V10Renderer       `json:"renderer"`
	MetamorphicGroup string            `json:"metamorphic_group"`
	Relation         string            `json:"relation"`
	AnswerRelation   string            `json:"answer_relation"`
	EvidencePairIDs  []string          `json:"evidence_pair_ids"`
}

// V10GeneratedCase is one fully materialized member of a metamorphic group.
// Plan carries the ordinary scored case; Provenance stays validator-side.
type V10GeneratedCase struct {
	Plan       QuestionPlan
	Pairs      []protocol.MemoryPair
	Provenance V10CaseProvenance
}

type v10Schema struct {
	Entity     string `json:"entity"`
	Alias      string `json:"alias"`
	Draft      string `json:"draft"`
	Approved   string `json:"approved"`
	Paid       string `json:"paid"`
	Unit       string `json:"unit"`
	Correction string `json:"correction_relation"`
}

type v10Scenario struct {
	Alias    string
	Draft    int
	Approved int
	Paid     int
	Unit     string
	Decoy    int
}

var v10LabelStarts = []string{
	"aro", "bel", "cyr", "dova", "eri", "fenn", "gali", "haro",
	"ivo", "jora", "kest", "luma", "mori", "nexa", "orin", "pera",
	"quill", "rava", "selo", "tavi", "ulma", "vora", "wera", "zori",
}

var v10LabelEnds = []string{
	"dan", "elle", "fin", "gate", "helm", "ion", "jor", "key",
	"line", "mark", "node", "ora", "path", "quin", "row", "set",
	"tier", "unit", "vale", "wire",
}

var v10Renderers = []V10Renderer{
	V10ConversationRenderer,
	V10EmailRenderer,
	V10TableRenderer,
	V10OpsRenderer,
}

// GenerateV10Programs returns count scored cases in complete four-member
// groups: base, renderer invariant, distractor invariant, and causal
// counterfactual. Count must therefore be a positive multiple of four.
func GenerateV10Programs(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("v10 program count must be a positive multiple of four, got %d", count)
	}
	schema := generateV10Schema(seed)
	ontology := schemaOntology(schema)
	schemaDigest, err := digestV10Schema(schema)
	if err != nil {
		return nil, err
	}
	r := rand.New(rand.NewSource(v10Seed(seed, "scenarios")))
	out := make([]V10GeneratedCase, 0, count)
	for group := 0; group < count/4; group++ {
		base := v10Scenario{
			Alias: v10Label(seed, fmt.Sprintf("scenario-%d", group)),
			Draft: 240_000 + r.Intn(2_400_000),
			Paid:  40_000 + r.Intn(160_000),
			Unit:  []string{"USD cents", "CAD cents", "EUR cents"}[r.Intn(3)],
			Decoy: 90_000 + r.Intn(900_000),
		}
		base.Approved = base.Draft + []int{-37_500, -12_500, 18_750, 42_500, 67_500}[r.Intn(5)]
		if base.Approved <= base.Paid+25_000 {
			base.Approved = base.Paid + 125_000
		}
		counter := base
		counter.Alias = base.Alias + "-revision"
		counter.Approved += 25_000 + 12_500*group
		if counter.Approved-counter.Paid == base.Approved-base.Paid {
			return nil, fmt.Errorf("v10 causal mutation did not change group %d answer", group)
		}
		groupID := protocol.OpaqueCaseID(seed, "v10-metamorphic-group", group)
		variants := []struct {
			relation string
			answer   string
			scenario v10Scenario
			distract bool
		}{
			{relation: "base", answer: "base", scenario: base},
			{relation: "renderer_invariant", answer: "same", scenario: base},
			{relation: "distractor_invariant", answer: "same", scenario: base, distract: true},
			{relation: "causal_counterfactual", answer: "changed", scenario: counter},
		}
		for variant, spec := range variants {
			renderer := v10Renderers[(group+variant)%len(v10Renderers)]
			generated, err := materializeV10Case(seed, group, variant, groupID, schemaDigest, ontology, schema, renderer, spec.scenario, spec.relation, spec.answer, spec.distract)
			if err != nil {
				return nil, err
			}
			out = append(out, generated)
		}
	}
	return out, nil
}

func materializeV10Case(
	seed int64,
	group int,
	variant int,
	groupID string,
	schemaDigest string,
	ontology []V10OntologyTerm,
	schema v10Schema,
	renderer V10Renderer,
	scenario v10Scenario,
	relation string,
	answerRelation string,
	includeDistractor bool,
) (V10GeneratedCase, error) {
	prefix := fmt.Sprintf("v10-%d-%d", group, variant)
	pairIDs := []string{
		protocol.OpaqueCaseID(seed, prefix+"-glossary", 0),
		protocol.OpaqueCaseID(seed, prefix+"-context", 0),
		protocol.OpaqueCaseID(seed, prefix+"-ledger", 0),
		protocol.OpaqueCaseID(seed, prefix+"-correction", 0),
		protocol.OpaqueCaseID(seed, prefix+"-payment", 0),
	}
	pairs := renderV10Scenario(seed, renderer, schema, scenario, pairIDs, includeDistractor)
	program := v10BalanceProgram(schema)
	state := map[string]int{schema.Draft: scenario.Draft, schema.Approved: scenario.Approved, schema.Paid: scenario.Paid}
	answer, err := EvaluateV10Program(program, state)
	if err != nil {
		return V10GeneratedCase{}, err
	}
	want := scenario.Approved - scenario.Paid
	if answer != want {
		return V10GeneratedCase{}, fmt.Errorf("v10 program evaluated %d, want %d", answer, want)
	}
	caseID := protocol.OpaqueCaseID(seed, "v10-program-case", group*4+variant)
	question := renderV10Question(seed, group, variant, schema, scenario)
	distractors := []string{
		fmt.Sprintf("%d", scenario.Draft-scenario.Paid),
		fmt.Sprintf("%d", scenario.Approved),
		fmt.Sprintf("%d", scenario.Decoy),
	}
	caseValue := protocol.MemoryCase{
		BenchVersion:      protocol.BenchVersionV10,
		ID:                caseID,
		QuestionID:        caseID,
		QuestionType:      "v10-open-program",
		Question:          question,
		ExpectedAnswer:    fmt.Sprintf("%d", answer),
		AnswerKind:        protocol.AnswerMoney,
		DistractorAnswers: distractors,
		WritingProtected:  []string{scenario.Alias, schema.Entity, schema.Alias, schema.Approved, schema.Paid, scenario.Unit},
	}
	if relation != "causal_counterfactual" {
		caseValue.TwinGroup = groupID
	}
	plan := QuestionPlan{
		Case:            caseValue,
		RequiredPairIDs: append([]string(nil), pairIDs...),
		Facts:           []string{"per-run schema glossary", "entity alias", "draft amount", "approved correction", "settled payment", "minor-unit contract"},
		Constraints:     []string{scenario.Alias, schema.Approved, schema.Paid, scenario.Unit},
		Operations:      []string{"induce schema", "resolve entity alias", "replace draft with approved correction", "subtract settled payment", "return minor units"},
	}
	provenance := V10CaseProvenance{
		Revision:         "dittobench-v10-generator-spec-v1",
		SchemaSHA256:     schemaDigest,
		Ontology:         append([]V10OntologyTerm(nil), ontology...),
		Program:          program,
		Renderer:         renderer,
		MetamorphicGroup: groupID,
		Relation:         relation,
		AnswerRelation:   answerRelation,
		EvidencePairIDs:  append([]string(nil), pairIDs...),
	}
	return V10GeneratedCase{Plan: plan, Pairs: pairs, Provenance: provenance}, nil
}

func generateV10Schema(seed int64) v10Schema {
	used := map[string]bool{}
	next := func(salt string) string {
		for attempt := 0; ; attempt++ {
			candidate := v10Label(seed, fmt.Sprintf("%s-%d", salt, attempt))
			if !used[candidate] {
				used[candidate] = true
				return candidate
			}
		}
	}
	return v10Schema{
		Entity: next("entity"), Alias: next("alias"), Draft: next("draft"),
		Approved: next("approved"), Paid: next("paid"), Unit: next("unit"),
		Correction: next("correction"),
	}
}

func schemaOntology(schema v10Schema) []V10OntologyTerm {
	return []V10OntologyTerm{
		{Semantic: "workstream entity", Wire: schema.Entity},
		{Semantic: "user-facing alias", Wire: schema.Alias},
		{Semantic: "draft amount", Wire: schema.Draft},
		{Semantic: "approved replacement amount", Wire: schema.Approved},
		{Semantic: "settled payment", Wire: schema.Paid},
		{Semantic: "minor-unit convention", Wire: schema.Unit},
		{Semantic: "replaces prior state", Wire: schema.Correction},
	}
}

func digestV10Schema(schema v10Schema) (string, error) {
	raw, err := json.Marshal(schema)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func v10BalanceProgram(schema v10Schema) V10QueryNode {
	return V10QueryNode{Op: "convert_minor_units", Children: []V10QueryNode{{
		Op: "subtract", Children: []V10QueryNode{
			{Op: "latest", Field: schema.Approved, Children: []V10QueryNode{{Op: "resolve_entity", Field: schema.Alias}}},
			{Op: "read", Field: schema.Paid},
		},
	}}}
}

// EvaluateV10Program executes the public recursive program against a latent
// integer state. It deliberately knows only the seed-scoped field labels.
func EvaluateV10Program(node V10QueryNode, state map[string]int) (int, error) {
	switch node.Op {
	case "resolve_entity":
		return 0, nil
	case "read", "latest":
		value, ok := state[node.Field]
		if !ok {
			return 0, fmt.Errorf("v10 program field %q is absent", node.Field)
		}
		for _, child := range node.Children {
			if _, err := EvaluateV10Program(child, state); err != nil {
				return 0, err
			}
		}
		return value, nil
	case "subtract":
		if len(node.Children) != 2 {
			return 0, fmt.Errorf("v10 subtract expects two children")
		}
		left, err := EvaluateV10Program(node.Children[0], state)
		if err != nil {
			return 0, err
		}
		right, err := EvaluateV10Program(node.Children[1], state)
		if err != nil {
			return 0, err
		}
		return left - right, nil
	case "convert_minor_units":
		if len(node.Children) != 1 {
			return 0, fmt.Errorf("v10 conversion expects one child")
		}
		return EvaluateV10Program(node.Children[0], state)
	default:
		return 0, fmt.Errorf("unsupported v10 program op %q", node.Op)
	}
}

func renderV10Scenario(seed int64, renderer V10Renderer, schema v10Schema, scenario v10Scenario, pairIDs []string, includeDistractor bool) []protocol.MemoryPair {
	glossary := fmt.Sprintf("In this batch, %s names a workstream, %s is its everyday alias, %s is a draft amount, %s is the later approved replacement, %s is a settled payment, %s states the minor-unit convention, and %s means the later value replaces the earlier one.", schema.Entity, schema.Alias, schema.Draft, schema.Approved, schema.Paid, schema.Unit, schema.Correction)
	rows := []string{
		glossary,
		fmt.Sprintf("%s=%s; %s=%s", schema.Entity, scenario.Alias, schema.Alias, scenario.Alias),
		fmt.Sprintf("%s=%s; %s=%d; %s=%s", schema.Alias, scenario.Alias, schema.Draft, scenario.Draft, schema.Unit, scenario.Unit),
		fmt.Sprintf("%s=%s; %s=%s->%s; %s=%d", schema.Alias, scenario.Alias, schema.Correction, schema.Draft, schema.Approved, schema.Approved, scenario.Approved),
		fmt.Sprintf("%s=%s; %s=%d", schema.Alias, scenario.Alias, schema.Paid, scenario.Paid),
	}
	if includeDistractor {
		rows[1] += fmt.Sprintf("; unrelated %s=%s, %s=%d", schema.Entity, v10Label(seed, "distractor-entity"), schema.Approved, scenario.Decoy)
	}
	pairs := make([]protocol.MemoryPair, 0, len(rows))
	for i, row := range rows {
		prompt, response := renderV10Record(renderer, i, row)
		pairs = append(pairs, protocol.MemoryPair{
			PairID: pairIDs[i], SessionID: fmt.Sprintf("v10-%s-%d", renderer, i),
			Timestamp: fmt.Sprintf("2026-01-%02dT%02d:00:00Z", 2+i, 9+i),
			Prompt:    prompt, Response: response,
		})
	}
	return pairs
}

func renderV10Record(renderer V10Renderer, index int, row string) (string, string) {
	switch renderer {
	case V10ConversationRenderer:
		return "I am going to describe one line from a custom workspace schema: " + row, "Understood — I will interpret those labels using this workspace's glossary."
	case V10EmailRenderer:
		return fmt.Sprintf("Subject: workspace record %d\nFrom: operations@example.invalid\n\n%s", index+1, row), "I have filed this email as part of the same reconciliation thread."
	case V10TableRenderer:
		return fmt.Sprintf("Pasted table row %d\n| payload |\n|---|\n| %s |", index+1, row), "I will preserve the row and its per-run field meanings."
	case V10OpsRenderer:
		return fmt.Sprintf("operations_dump[%d] { %s }", index, strings.ReplaceAll(row, "; ", ", ")), "Recorded as an operational event; later replacement markers take precedence."
	default:
		panic("unhandled v10 renderer")
	}
}

func renderV10Question(seed int64, group, variant int, schema v10Schema, scenario v10Scenario) string {
	templates := []string{
		"Using this run's glossary, reconcile %s: take the latest %s value, subtract %s, and return the result under %s as minor units.",
		"For %s, interpret the local schema rather than guessing field names. After %s replaces the draft, what remains once %s is removed? Answer in %s minor units.",
		"Resolve the %s record from the custom labels, apply its %s correction, then deduct %s. What balance follows using %s?",
		"Read the per-run field meanings for %s. Which minor-unit balance remains after the current %s amount and the settled %s amount are reconciled under %s?",
	}
	index := int(v10Seed(seed, fmt.Sprintf("question-%d-%d", group, variant)) % int64(len(templates)))
	return fmt.Sprintf(templates[index], scenario.Alias, schema.Approved, schema.Paid, schema.Unit)
}

func v10Label(seed int64, salt string) string {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v10-label:%d:%s", seed, salt)
	value := h.Sum64()
	return v10LabelStarts[value%uint64(len(v10LabelStarts))] + v10LabelEnds[(value/uint64(len(v10LabelStarts)))%uint64(len(v10LabelEnds))]
}

func v10Seed(seed int64, salt string) int64 {
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "dittobench-v10:%d:%s", seed, salt)
	return int64(h.Sum64() & ((1 << 63) - 1))
}
