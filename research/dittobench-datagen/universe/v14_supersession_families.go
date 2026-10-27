package universe

import (
	"fmt"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// V14 supersession-chain family adapters (SN-353 / SN-354).
//
// SN-356 (next-action), SN-357 (school list), SN-358 (latest event) and SN-360
// (client vs vendor) own the other families, so only these chain.

// v14ChainBusinessFamilies are the business families that render chains.
var v14ChainBusinessFamilies = map[V13Family]bool{
	V13FamilyOwnerAfterCorrection: true,
	V13FamilyStandingStatus:       true,
	V13FamilyCurrentChannel:       true,
}

// v14ChainPersonalDomains are the personal domains that render chains.
var v14ChainPersonalDomains = map[V13PersonalDomain]bool{
	V13PersonalHousehold:     true,
	V13PersonalAppointments:  true,
	V13PersonalMilestones:    true,
	V13PersonalSubscriptions: true,
	V13PersonalTravel:        true,
	V13PersonalHobbies:       true,
}

// drawV14BusinessChainValues draws the fourth chain value of a v14 group. It
// runs for every family so the name stream advances identically whichever
// family a group draws.
func (d *v13Draws) drawV14BusinessChainValues(group int, g *v13Group) {
	g.ExtraPerson = d.name()
	switch g.Family {
	case V13FamilyStandingStatus:
		g.ExtraClass = v13Perm(d.seed, fmt.Sprintf("status-classes-%d", group), len(V13StatusClasses))[4]
	case V13FamilyCurrentChannel:
		g.ExtraClass = v13Perm(d.seed, fmt.Sprintf("channel-classes-%d", group), len(V13ChannelClasses))[4]
	}
}

func v14ChainSteps(plan v14ChainPlan, counter bool) []v14ChainStep {
	if counter {
		return plan.Template.Counter
	}
	return plan.Template.Steps
}

// applyV14BusinessChain replaces a business member's single correction with a
// supersession chain. The decoy, question and neutral record are unchanged.
func applyV14BusinessChain(d *v13Draws, m *v13Member, schema v13Schema, group int, g v13Group, counter bool) {
	if !v14ChainBusinessFamilies[g.Family] {
		return
	}
	seed := d.seed
	plan := drawV14ChainPlan(seed, "business", group, 4)
	steps := v14ChainSteps(plan, counter)
	answer, _ := v14ChainFold(steps)
	resolve := V10QueryNode{Op: "resolve_entity", Field: schema.Alias}
	salt := fmt.Sprintf("v14-%s-%d", g.Family, group)
	var voice v14ChainVoice
	var field string
	switch g.Family {
	case V13FamilyOwnerAfterCorrection:
		people := []string{g.People[0], g.People[1], g.People[2], g.ExtraPerson}
		field = schema.Owner
		voice = v14ChainVoice{Subject: fmt.Sprintf("the %s on %s", schema.Owner, g.Alias), Copula: "is",
			Value: func(i int, _ string) string { return people[i] }}
		m.Expected, m.AcceptAny = people[answer], []string{people[answer]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, people[answer], []string{people[answer]}, 1)}
		m.Protected = append(m.Protected, g.People[2], g.ExtraPerson)
	case V13FamilyStandingStatus:
		classes := []int{g.Classes[0], g.Classes[1], g.Classes[2], g.ExtraClass}
		field = schema.Status
		voice = v14ChainVoice{Subject: fmt.Sprintf("the %s of %s", schema.Status, g.Alias), Copula: "is",
			Value: func(i int, _ string) string { return d.statusAlias(classes[i]) }}
		cls := V13StatusClasses[classes[answer]]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string{d.statusAlias(classes[answer])}, cls.Accept...)
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindStatus, cls.Canonical, m.AcceptAny, 1)}
		m.Protected = append(m.Protected, d.statusAlias(g.Classes[2]), d.statusAlias(g.ExtraClass))
	case V13FamilyCurrentChannel:
		classes := []int{g.Classes[0], g.Classes[1], g.Classes[2], g.ExtraClass}
		field = schema.Channel
		voice = v14ChainVoice{Subject: fmt.Sprintf("the %s for %s", schema.Channel, g.Alias), Copula: "is",
			Value: func(i int, _ string) string { return V13ChannelClasses[classes[i]].Canonical }}
		cls := V13ChannelClasses[classes[answer]]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string(nil), cls.Accept...)
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindChannel, cls.Canonical, cls.Accept, 1)}
		m.Protected = append(m.Protected, V13ChannelClasses[g.Classes[2]].Canonical, V13ChannelClasses[g.ExtraClass].Canonical)
	}
	m.v14ChainApply(renderV14Chain(seed, salt, plan, steps, voice, m.Records[0]))
	m.Program = v14ChainProgram(field, resolve)
	m.Facts = append([]string{"per-run schema glossary", "entity alias"}, v14ChainFacts(plan, field)...)
	m.Operations = append([]string{"induce schema"}, v14ChainOperations(plan)...)
}

// applyV14PersonalChain is the personal-domain counterpart.
func applyV14PersonalChain(d *v13Draws, m *v13Member, group int, g v13PersonalGroup, counter bool) {
	if !v14ChainPersonalDomains[g.Domain] {
		return
	}
	seed := d.seed
	maxValues := 4
	if g.Domain == V13PersonalMilestones {
		// Four status classes: three chain values plus the decoy.
		maxValues = 3
	}
	plan := drawV14ChainPlan(seed, "personal", group, maxValues)
	steps := v14ChainSteps(plan, counter)
	answer, _ := v14ChainFold(steps)
	salt := fmt.Sprintf("v14-p-%s-%d", g.Domain, group)
	var voice v14ChainVoice
	var field string
	person := func(subject string) {
		people := []string{g.People[0], g.People[1], g.People[2], g.ExtraPerson}
		voice = v14ChainVoice{Subject: subject, Copula: "is", Value: func(i int, _ string) string { return people[i] }}
		m.Expected, m.AcceptAny = people[answer], []string{people[answer]}
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindPerson, people[answer], []string{people[answer]}, 1)}
		m.Protected = append(m.Protected, g.People[2], g.ExtraPerson)
	}
	switch g.Domain {
	case V13PersonalHousehold:
		field = "chore_owner"
		person("the person doing " + g.Subject)
	case V13PersonalHobbies:
		field = "organiser"
		person("the organiser of " + g.Subject)
	case V13PersonalAppointments:
		field = "appointment_date"
		dates := []v13Date{g.Dates[0], g.Dates[1], g.Dates[2], g.ExtraDate}
		voice = v14ChainVoice{Subject: "the date of " + g.Relation + "'s " + g.Subject, Copula: "is",
			Value: func(i int, key string) string {
				return v13CalendarProse(seed, "personal", group, key, dates[i], false)
			}}
		at := v13CalendarDate(seed, "personal", group, dates[answer], false)
		m.Expected = at.Format("2006-01-02")
		m.AcceptAny = V13DateAccept(at.Year(), int(at.Month()), at.Day())
		m.Claims = []protocol.Claim{{Kind: protocol.ClaimKindDate, Expected: m.Expected, Accept: m.AcceptAny, Unit: "day", Critical: true, Weight: 1}}
	case V13PersonalMilestones:
		field = "plan_status"
		classes := []int{g.Classes[0], g.Classes[1], g.Classes[2]}
		voice = v14ChainVoice{Subject: "the status of " + g.Relation + "'s " + g.Subject, Copula: "is",
			Value: func(i int, _ string) string { return V13PersonalStatusClasses[classes[i]].Canonical }}
		cls := V13PersonalStatusClasses[classes[answer]]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string(nil), cls.Accept...)
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindStatus, cls.Canonical, m.AcceptAny, 1)}
		m.Protected = append(m.Protected, V13PersonalStatusClasses[g.Classes[1]].Canonical, V13PersonalStatusClasses[g.Classes[2]].Canonical)
	case V13PersonalSubscriptions:
		field = "next_action"
		classes := []int{g.Classes[0], g.Classes[1], g.Classes[2], g.ExtraClass}
		voice = v14ChainVoice{Subject: "the plan for " + g.Subject, Copula: "is to",
			Value: func(i int, _ string) string { return V13PersonalActionClasses[classes[i]].Canonical }}
		cls := V13PersonalActionClasses[classes[answer]]
		m.Expected = cls.Canonical
		m.AcceptAny = append([]string(nil), cls.Accept...)
		m.Claims = []protocol.Claim{v13Claim(protocol.ClaimKindAction, cls.Canonical, m.AcceptAny, 1)}
		for _, c := range classes {
			m.Protected = append(m.Protected, V13PersonalActionClasses[c].Canonical)
		}
	case V13PersonalTravel:
		field = "departure_time"
		leg := v13Pick(seed, fmt.Sprintf("p-leg-%d", group), v13Legs)
		times := []v13Time{g.Times[0], g.Times[1], g.Times[2], g.ExtraTime}
		voice = v14ChainVoice{Subject: "the departure time of the " + leg + " for " + g.Subject, Copula: "is",
			Value: func(i int, key string) string { return v13TimeProse(seed, key, times[i]) }}
		t := times[answer]
		m.Expected = fmt.Sprintf("%02d:%02d", t.Hour, t.Minute)
		m.AcceptAny = V13TimeAccept(t.Hour, t.Minute)
		m.Claims = []protocol.Claim{{Kind: protocol.ClaimKindTime, Expected: m.Expected, Accept: m.AcceptAny, Unit: "minute", Critical: true, Weight: 1}}
	}
	m.v14ChainApply(renderV14Chain(seed, salt, plan, steps, voice, m.Records[0]))
	m.Program = v14ChainProgram(field, V10QueryNode{Op: "resolve_entity", Field: field})
	m.Facts = v14ChainFacts(plan, field)
	m.Operations = v14ChainOperations(plan)
}
