package universe

import (
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// An assignment is an event, not a sentence. Sequence is world chronology;
// record order and wording must never determine the answer.
type v13OwnerEvent struct {
	Entity   string
	Person   string
	Sequence int
}

type v13OwnerWorld struct {
	Entity, Purpose, Role, Correction string
	Events                            []v13OwnerEvent
}

func (w v13OwnerWorld) answer() (string, error) {
	if w.Entity == "" || w.Purpose == "" || w.Role == "" || w.Correction == "" {
		return "", fmt.Errorf("incomplete owner world")
	}
	seen := map[string]map[int]bool{}
	latest, answer := -1, ""
	for _, event := range w.Events {
		if event.Entity == "" || event.Person == "" || event.Sequence < 0 {
			return "", fmt.Errorf("invalid owner event")
		}
		if seen[event.Entity] == nil {
			seen[event.Entity] = map[int]bool{}
		}
		if seen[event.Entity][event.Sequence] {
			return "", fmt.Errorf("ambiguous owner chronology")
		}
		seen[event.Entity][event.Sequence] = true
		if event.Entity == w.Entity && event.Sequence > latest {
			latest, answer = event.Sequence, event.Person
		}
	}
	if answer == "" {
		return "", fmt.Errorf("owner query has no evidence")
	}
	return answer, nil
}

// Render only a validated two-event history. Additional event shapes must add
// render support explicitly; never silently omit a load-bearing fact.
func renderV13OwnerWorld(w v13OwnerWorld, presentationSeed int64) ([2]string, string, error) {
	var rows [2]string
	if _, err := w.answer(); err != nil {
		return rows, "", err
	}
	if len(w.Events) != 2 || w.Events[0].Entity != w.Entity || w.Events[1].Entity != w.Entity ||
		w.Events[0].Sequence != 0 || w.Events[1].Sequence != 1 {
		return rows, "", fmt.Errorf("unsupported owner history shape")
	}
	first, current := w.Events[0].Person, w.Events[1].Person
	rows[0] = fmt.Sprintf(v13Pick(presentationSeed, "owner-initial", []string{
		"When %[1]s opened, %[2]s held its %[3]s role.",
		"The original %[3]s assignment for %[1]s named %[2]s.",
		"At the start, responsibility for %[1]s in the %[3]s role belonged to %[2]s.",
		"%[2]s was appointed %[3]s for %[1]s at its opening.",
	}), w.Entity, first, w.Role)
	rows[1] = fmt.Sprintf(v13Pick(presentationSeed, "owner-update", []string{
		"Later, a %[4]s assigned the %[3]s role for %[1]s to %[2]s.",
		"The subsequent %[4]s for %[1]s names %[2]s as %[3]s.",
		"%[2]s took over the %[3]s role for %[1]s in a later %[4]s.",
		"For %[1]s, the latest %[4]s places %[2]s in the %[3]s role.",
	}), w.Entity, current, w.Role, w.Correction)
	rows[1] += " " + v13Pick(presentationSeed, "owner-supersession", []string{
		"This replaces the opening assignment; no further reassignment is recorded.",
		"The original assignment no longer applies, and this is the final recorded change.",
		"This is the last recorded assignment and supersedes the initial one.",
	})
	question := fmt.Sprintf(v13Pick(presentationSeed, "owner-query", []string{
		"Who now holds the %[2]s role for the workstream whose remit is %[1]s?",
		"For the workstream handling %[1]s, name the %[2]s after all recorded changes.",
		"Which person is the current %[2]s of the workstream responsible for %[1]s?",
		"After the final reassignment, who is %[2]s for the workstream tasked with %[1]s?",
	}), w.Purpose, w.Role)
	question += " " + v13Pick(presentationSeed, "owner-format", []string{
		"Return only the person's name.", "Answer with the name only.",
	})
	return rows, question, nil
}

// GenerateV13OwnerSlice is a research-only complete metamorphic slice of the
// existing V13 owner family. It does not enable private rewriting or change
// GenerateV13Programs. Facts use worldSeed; presentation uses independent
// entropy. Callers needing private presentation must not publish that entropy.
// No free-form model is permitted to modify facts, questions, or answers here.
func GenerateV13OwnerSlice(worldSeed, presentationSeed int64) ([]V10GeneratedCase, error) {
	d := newV13Draws(worldSeed, "business")
	g := d.drawGroup(0)
	schema := generateV13Schema(worldSeed)
	digest, err := digestV13Schema(schema)
	if err != nil {
		return nil, err
	}
	out := make([]V10GeneratedCase, 0, 4)
	relations := []string{protocol.RelationBase, protocol.RelationRendererInvariant, protocol.RelationDistractorInvariant, protocol.RelationCausalCounterfactual}
	for variant, relation := range relations {
		current := g.People[1]
		if variant == 3 {
			current = g.People[2]
		}
		world := v13OwnerWorld{Entity: g.Alias, Purpose: g.Purpose, Role: schema.Owner, Correction: schema.Correction,
			Events: []v13OwnerEvent{{g.Alias, g.People[0], 0}, {g.Alias, current, 1}}}
		answer, err := world.answer()
		if err != nil {
			return nil, err
		}
		// Only the presentation-invariant member changes sentence choices;
		// the causal member changes one fact with the exact same render plan.
		renderSeed := presentationSeed
		if variant == 1 {
			renderSeed ^= 0x517cc1b727220a95
		}
		rows, question, err := renderV13OwnerWorld(world, renderSeed)
		if err != nil {
			return nil, err
		}
		m := v13Member{
			Records:     [3]string{rows[0], rows[1], fmt.Sprintf("The milestone review for %s is scheduled for %s.", g.Alias, v13CalendarProse(worldSeed, "business", 0, "milestone-0", g.Milestone, false))},
			DecoyClause: fmt.Sprintf(" Its %s is %s.", schema.Owner, g.People[3]),
			Question:    question, Kind: protocol.AnswerValue, Expected: answer, AcceptAny: []string{answer},
			Distractors: []string{g.People[3]}, Claims: []protocol.Claim{v13Claim(protocol.ClaimKindPerson, answer, []string{answer}, 1)},
			Program:     V10QueryNode{Op: "latest", Field: schema.Owner, Children: []V10QueryNode{{Op: "resolve_entity", Field: schema.Alias}}},
			Protected:   []string{g.Alias, schema.Entity, schema.Alias, schema.Correction, schema.Owner, g.People[0], current, g.People[3]},
			Facts:       []string{"entity remit", "initial assignment", "final superseding assignment", "unrelated milestone"},
			Constraints: []string{g.Alias, g.Purpose, schema.Owner, schema.Correction},
			Operations:  []string{"resolve entity by remit", "evaluate assignment chronology", "return current person"},
		}
		// Template inputs are generated atoms, not unrestricted instructions.
		if strings.Contains(question, answer) {
			return nil, fmt.Errorf("answer leaked into owner question")
		}
		answerRelation := "same"
		if variant == 0 {
			answerRelation = "base"
		}
		if variant == 3 {
			answerRelation = "changed"
		}
		generated := materializeV13Case(d, schema, digest, v13Ontology(schema), 0, variant,
			protocol.OpaqueCaseID(worldSeed, "v13-metamorphic-group", 0), v10Renderers[variant], g, m, relation, answerRelation, variant == 2)
		generated.Provenance.Revision = "dittobench-v13-owner-world-slice-v1"
		out = append(out, generated)
	}
	return out, nil
}
