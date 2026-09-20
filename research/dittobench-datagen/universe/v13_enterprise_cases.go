package universe

import (
	"crypto/sha256"
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const V13EnterpriseRevision = "v13-deterministic-enterprise-v1"
const V13EnterpriseQuestionType = "enterprise-composed-program"

// GenerateV13EnterprisePrograms fills complete four-case groups. Renderers never
// receive the query. Each member has its own explicit evidence scope, so a
// counterfactual cannot overwrite another member's state in the shared graph.
func GenerateV13EnterprisePrograms(seed int64, count int) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 || count > 12 {
		return nil, fmt.Errorf("enterprise cases: need 4, 8 or 12 cases")
	}
	formats := []string{"csv", "json", "markdown", "slack", "email", "transcript"}
	relations := []string{protocol.RelationBase, protocol.RelationRendererInvariant, protocol.RelationDistractorInvariant, protocol.RelationCausalCounterfactual}
	var out []V10GeneratedCase
	for group := 0; group < count/4; group++ {
		worldSeed := v10Seed(seed, fmt.Sprintf("enterprise-case-world/%d", group))
		kind := int(uint64(seed)%3+uint64(group)) % 3
		target := enterpriseEntityID(worldSeed, fmt.Sprintf("work-%04d", uint64(seed)%6))
		steps := []V13EnterpriseStep{{Op: "follow", Field: "team"}, {Op: "follow", Field: "members"}, {Op: "filter", Field: "availability", Value: "available"}}
		question := fmt.Sprintf("At effective step 12, follow the team of %s and consider only its current members whose availability is available. ", target)
		op, answerKind := "sum", protocol.AnswerNumber
		switch kind {
		case 0:
			steps = append(steps, V13EnterpriseStep{Op: "sum", Field: "allocation_hours"})
			question += "What is their total allocation_hours, in hours? An empty selection totals zero."
		case 1:
			op = "count"
			steps = append(steps, V13EnterpriseStep{Op: "count"})
			question += "How many distinct members qualify? An empty selection counts as zero."
		case 2:
			op, answerKind = "project", protocol.AnswerValue
			steps = []V13EnterpriseStep{{Op: "follow", Field: "team"}, {Op: "follow", Field: "vendor"}, {Op: "follow", Field: "owner"}, {Op: "project", Field: "channel"}}
			question = fmt.Sprintf("At effective step 12, which contact channel belongs to the owner of the vendor of the team of %s?", target)
		}
		var baseAnswer string
		for variant, relation := range relations {
			teams := 6
			if variant == 2 {
				teams = 12 // Same grammar and fields; additional equally queryable worlds.
			}
			w, err := GenerateV13Enterprise(worldSeed, teams, 9)
			if err != nil {
				return nil, err
			}
			if variant == 3 {
				if err := enterpriseCaseCounterfactual(&w, target, kind, worldSeed); err != nil {
					return nil, err
				}
			}
			answer, err := EvaluateV13EnterpriseProgram(w, target, 12, steps)
			if err != nil || len(answer) != 1 {
				return nil, fmt.Errorf("enterprise case result: %v (%v)", answer, err)
			}
			if variant == 0 {
				baseAnswer = answer[0]
			} else if (answer[0] == baseAnswer) != (variant != 3) {
				return nil, fmt.Errorf("enterprise case: violated %s relation", relation)
			}
			formatIndex := int(uint64(seed)%6) + group
			if variant == 1 {
				formatIndex++
			}
			format := formats[formatIndex%len(formats)]
			presentationSeed := v10Seed(seed, fmt.Sprintf("enterprise-presentation/%d", group))
			docs, err := RenderV13EnterpriseDocuments(w, presentationSeed, format, 96)
			if err != nil {
				return nil, err
			}
			ordinal := group*4 + variant
			scope := protocol.OpaqueCaseID(seed, "enterprise-scope", ordinal)
			var pairs []protocol.MemoryPair
			var ids []string
			for i, doc := range docs {
				id := protocol.OpaqueCaseID(seed, fmt.Sprintf("enterprise-pair/%d", ordinal), i)
				ids = append(ids, id)
				pairs = append(pairs, protocol.MemoryPair{PairID: id,
					SessionID: protocol.OpaqueCaseID(seed, "enterprise-session", ordinal),
					Timestamp: v13CalendarTimestamp(seed, "enterprise", ordinal, i),
					Prompt:    "Case file " + scope + ". Complete event history, continued across this file's records. Apply events by effective_step, not document order. assign replaces a scalar; add/remove changes only the named set member. State persists until changed.\n" + doc.Body,
					Response:  fmt.Sprintf("Saved section %d of case file %s.", i+1, scope)})
			}
			caseID := protocol.OpaqueCaseID(seed, "enterprise-case", ordinal)
			groupID := protocol.OpaqueCaseID(seed, "enterprise-group", group)
			mc := protocol.MemoryCase{BenchVersion: protocol.BenchVersionV13, ID: caseID, QuestionID: caseID,
				QuestionType: V13EnterpriseQuestionType, Question: "Use only case file " + scope + ". " + question,
				ExpectedAnswer: answer[0], AnswerKind: answerKind}
			if variant != 3 {
				mc.TwinGroup = groupID
			} else {
				mc.DistractorAnswers = []string{baseAnswer}
			}
			program := V10QueryNode{Op: op}
			for _, step := range steps {
				program.Children = append(program.Children, V10QueryNode{Op: step.Op, Field: step.Field})
			}
			schema := sha256.Sum256([]byte(V13EnterpriseRevision + "/assign-add-remove/follow-filter-project-count-sum/at12"))
			answerRelation := "same"
			if variant == 0 {
				answerRelation = "base"
			}
			if variant == 3 {
				answerRelation = "changed"
			}
			out = append(out, V10GeneratedCase{Plan: QuestionPlan{Case: mc, RequiredPairIDs: ids}, Pairs: pairs,
				Provenance: V10CaseProvenance{Revision: V13EnterpriseRevision, SchemaSHA256: fmt.Sprintf("%x", schema),
					Ontology: []V10OntologyTerm{{Semantic: "serialization", Wire: format}}, Program: program,
					Renderer: V10Renderer(format), MetamorphicGroup: groupID, Relation: relation,
					AnswerRelation: answerRelation, EvidencePairIDs: append([]string(nil), ids...)}})
		}
	}
	return out, nil
}

func enterpriseCaseCounterfactual(w *V13EnterpriseWorld, target string, kind int, seed int64) error {
	s, err := w.state(12)
	if err != nil {
		return err
	}
	team := s[target]["team"][0]
	if kind == 2 {
		vendor := s[team]["vendor"][0]
		owner := s[vendor]["owner"][0]
		old := s[owner]["channel"][0]
		h := sha256.Sum256([]byte(old + "/alternate"))
		w.Events = append(w.Events, V13EnterpriseEvent{owner, "channel", fmt.Sprintf("contact-%x@fictional.example", h[:8]), 12, "assign"})
		return nil
	}
	// A new available member changes both count and sum, including an empty
	// selection. Its ordinary records carry no counterfactual/answer marker.
	person := enterpriseEntityID(seed, "additional-member")
	w.Entities = append(w.Entities, person)
	w.Events = append(w.Events,
		V13EnterpriseEvent{person, "availability", "available", 12, "assign"},
		V13EnterpriseEvent{person, "allocation_hours", "7", 12, "assign"},
		V13EnterpriseEvent{person, "channel", strings.ReplaceAll(person, "entity-", "contact-") + "@fictional.example", 12, "assign"},
		V13EnterpriseEvent{team, "members", person, 12, "add"})
	return nil
}
