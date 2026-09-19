package universe

import (
	"context"
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type v13PersonalFactPlan struct {
	World                      v13FactWorld
	Decoy                      v13Fact
	Distractors                []string
	ProgramField, ResolveField string
}

func buildV13PersonalFactPlan(seed int64, group int, g v13PersonalGroup, counter bool) (v13PersonalFactPlan, error) {
	entity, decoy := g.Subject, g.Decoy
	field, programField, resolveField := "", "", ""
	var first, final, other v13FactValue
	current := 1
	if counter {
		current = 2
	}
	person := func(i int) v13FactValue { return factValue(g.People[i], protocol.ClaimKindPerson) }
	date := func(i int) v13FactValue {
		at := v13CalendarDate(seed, "personal", group, g.Dates[i], false)
		v := factValue(at.Format("2006-01-02"), protocol.ClaimKindDate, V13DateAccept(at.Year(), int(at.Month()), at.Day())...)
		v.Surface = at.Format("January 2, 2006")
		v.Unit = "day"
		return v
	}
	var extras []v13Fact
	switch g.Domain {
	case V13PersonalHousehold:
		field, programField, resolveField = "person responsible", "chore_owner", "chore"
		first, final, other = person(0), person(current), person(3)
	case V13PersonalAppointments:
		entity, decoy = g.Relation+"'s "+g.Subject, g.People[3]+"'s "+g.Decoy
		field, programField, resolveField = "appointment date", "appointment_date", "appointment"
		first, final, other = date(0), date(current), date(3)
	case V13PersonalSchool:
		entity, decoy = g.Relation+"'s "+g.Subject, g.People[3]+"'s "+g.Decoy
		field, programField, resolveField = "packing list", "packing_list", "school_event"
		other = factValue(g.DecoyItm, protocol.ClaimKindSetMember)
	case V13PersonalMilestones:
		entity, decoy = g.Relation+"'s "+g.Subject, g.People[3]+"'s "+g.Decoy
		field, programField, resolveField = "plan status", "plan_status", "milestone"
		value := func(i int) v13FactValue {
			c := V13PersonalStatusClasses[g.Classes[i]]
			return factValue(c.Canonical, protocol.ClaimKindStatus, c.Accept...)
		}
		first, final, other = value(0), value(current), value(3)
		extras = append(extras, v13Fact{Entity: entity, Field: "planned date", Value: date(0), Mode: "static", Record: 0})
	case V13PersonalSubscriptions:
		field, programField, resolveField = "next action", "next_action", "subscription"
		value := func(i int) v13FactValue {
			c := V13PersonalActionClasses[g.Classes[i]]
			return factValue(c.Canonical, protocol.ClaimKindAction, c.Accept...)
		}
		first, final, other = value(0), value(current), value(3)
		extras = append(extras, v13Fact{Entity: entity, Field: "renewal date", Value: date(0), Mode: "static", Record: 0})
	case V13PersonalTravel:
		leg := v13Pick(seed, fmt.Sprintf("p-leg-%d", group), v13Legs)
		entity = leg + " for " + g.Subject
		field, programField, resolveField = "departure time", "departure_time", "trip"
		value := func(i int) v13FactValue {
			at := g.Times[i]
			v := factValue(fmt.Sprintf("%02d:%02d", at.Hour, at.Minute), protocol.ClaimKindTime, V13TimeAccept(at.Hour, at.Minute)...)
			v.Unit = "minute"
			return v
		}
		first, final, other = value(0), value(current), value(3)
	case V13PersonalHobbies:
		field, programField, resolveField = "organiser", "organiser", "club"
		first, final, other = person(0), person(current), person(3)
	default:
		return v13PersonalFactPlan{}, fmt.Errorf("unsupported personal fact domain")
	}
	w := v13FactWorld{Entity: entity, Purpose: entity, Query: []v13FactQuery{{"latest", field}}}
	if g.Domain == V13PersonalSchool {
		if len(g.Items) != 4 || g.DropBase < 0 || g.DropBase >= 4 || g.DropCF < 0 || g.DropCF >= 4 {
			return v13PersonalFactPlan{}, fmt.Errorf("invalid school set draw")
		}
		w.Query[0].Op = "set_after_update"
		for i, item := range g.Items {
			w.Facts = append(w.Facts, v13Fact{Entity: entity, Field: field, Value: factValue(item, protocol.ClaimKindSetMember), Mode: "set_member", Record: 0, Order: i})
		}
		drop := g.DropBase
		if counter {
			drop = g.DropCF
		}
		w.Facts = append(w.Facts,
			v13Fact{Entity: entity, Field: field, Value: factValue(g.Items[drop], protocol.ClaimKindSetMember), Mode: "set_remove", Record: 1, Order: 0},
			v13Fact{Entity: entity, Field: field, Value: factValue(g.Added, protocol.ClaimKindSetMember), Mode: "set_add", Record: 1, Order: 1})
	} else {
		w.Facts = []v13Fact{{Entity: entity, Field: field, Value: first, Mode: "history", Order: 0, Record: 0}, {Entity: entity, Field: field, Value: final, Mode: "history", Order: 1, Record: 1}}
	}
	w.Facts = append(w.Facts, extras...)
	w.Facts = append(w.Facts, v13Fact{Entity: "the household", Field: "shared-list arrangement", Value: factValue("keep the shopping list on the fridge tablet", protocol.ClaimKindAction), Mode: "static", Record: 2})
	p := v13PersonalFactPlan{World: w, ProgramField: programField, ResolveField: resolveField, Decoy: v13Fact{Entity: decoy, Field: field, Value: other, Mode: "static", Record: 0}, Distractors: []string{other.Canonical}}
	if g.Domain == V13PersonalAppointments || g.Domain == V13PersonalTravel {
		p.Distractors = append([]string(nil), other.Accept[1:]...)
	}
	if g.Domain == V13PersonalSchool {
		drop := g.DropBase
		if counter {
			drop = g.DropCF
		}
		p.Distractors = []string{g.Items[drop], g.DecoyItm}
	}
	_, err := w.evaluate()
	return p, err
}

func renderV13PersonalFactPlan(p v13PersonalFactPlan, seed int64) (v13Member, error) {
	m, err := renderV13FactMember(p.World, v13Schema{Correction: "update", Alias: p.ResolveField}, seed, "")
	if err != nil {
		return m, err
	}
	q := p.World.Query[0]
	forms := []string{"After all recorded changes, what is the %[2]s for %[1]s?", "For %[1]s, give the final recorded %[2]s."}
	if q.Op == "set_after_update" {
		forms = []string{"After the list update, what is on the %[2]s for %[1]s? Give every required item.", "For %[1]s, list all items remaining on the %[2]s after the removal and addition."}
		m.Records[0] += " These are all the original required items."
	}
	m.Question = fmt.Sprintf(v13Pick(seed, "personal-fact-query", forms), p.World.Entity, q.Field)
	m.DecoyClause, err = renderV13Fact(p.Decoy, "update", seed, "personal-decoy")
	if err != nil {
		return m, err
	}
	m.Distractors = append([]string(nil), p.Distractors...)
	m.Protected = append(m.Protected, p.Decoy.Entity, p.Decoy.Field, p.Decoy.Value.Surface)
	m.Program.Field = p.ProgramField
	return m, nil
}

// GenerateV13PersonalFactPrograms uses the shared fact interpreter, preserving
// the seeded personal domain mix, calendar anchors and grading units. It is an
// opt-in migration path, not a completed private dataset or qualification.
func GenerateV13PersonalFactPrograms(worldSeed, presentationSeed int64, count int) ([]V10GeneratedCase, error) {
	return generateV13PersonalFactPrograms(context.Background(), worldSeed, presentationSeed, count, nil)
}

func GenerateV13RenderedPersonalFactPrograms(ctx context.Context, worldSeed, presentationSeed int64, count int, renderer V13FactRenderer) ([]V10GeneratedCase, error) {
	if renderer == nil {
		return nil, fmt.Errorf("explicit fact renderer required")
	}
	return generateV13PersonalFactPrograms(ctx, worldSeed, presentationSeed, count, renderer)
}

func generateV13PersonalFactPrograms(ctx context.Context, worldSeed, presentationSeed int64, count int, renderer V13FactRenderer) ([]V10GeneratedCase, error) {
	if count <= 0 || count%4 != 0 {
		return nil, fmt.Errorf("personal fact count must be a positive multiple of four")
	}
	d := newV13Draws(worldSeed, "personal")
	domains := v13Perm(worldSeed, "personal-domains", len(V13PersonalDomains))
	var out []V10GeneratedCase
	for group := 0; group < count/4; group++ {
		g := d.drawPersonalGroup(group, V13PersonalDomains[domains[group%len(domains)]])
		var baseAnswer string
		var basePlan *V13FactRenderPlan
		for variant, relation := range []string{protocol.RelationBase, protocol.RelationRendererInvariant, protocol.RelationDistractorInvariant, protocol.RelationCausalCounterfactual} {
			p, err := buildV13PersonalFactPlan(worldSeed, group, g, variant == 3)
			if err != nil {
				return nil, err
			}
			renderSeed := v10Seed(presentationSeed, fmt.Sprintf("personal-facts-%d", group))
			if variant == 1 {
				renderSeed = v10Seed(renderSeed, "equivalent-presentation")
			}
			m, err := renderV13PersonalFactPlan(p, renderSeed)
			if err != nil {
				return nil, err
			}
			if renderer != nil {
				plan := basePlan
				if variant == 1 {
					plan = nil
				}
				var chosen *V13FactRenderPlan
				m, chosen, err = applyV13FactRender(ctx, p.World, v13Schema{}, false, m, renderer, plan)
				if err != nil {
					return nil, err
				}
				if variant == 0 {
					basePlan = chosen
				}
			}
			if variant == 0 {
				baseAnswer = m.Expected
			}
			if (variant == 3 && baseAnswer == m.Expected) || (variant > 0 && variant < 3 && baseAnswer != m.Expected) {
				return nil, fmt.Errorf("personal fact metamorphic violation")
			}
			answerRelation := "same"
			if variant == 0 {
				answerRelation = "base"
			}
			if variant == 3 {
				answerRelation = "changed"
			}
			generated := materializeV13PersonalCase(worldSeed, group, variant, protocol.OpaqueCaseID(worldSeed, "v13-personal-group", group), v13PersonalRenderers[(group+variant)%len(v13PersonalRenderers)], g, m, relation, answerRelation, variant == 2)
			generated.Provenance.Revision = "dittobench-v13-personal-fact-world-v1"
			if renderer != nil {
				generated.Provenance.Revision = "dittobench-v13-authored-personal-fact-world-v1"
			}
			out = append(out, generated)
		}
	}
	return out, nil
}
