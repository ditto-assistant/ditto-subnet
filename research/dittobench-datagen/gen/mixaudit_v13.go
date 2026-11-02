package gen

import (
	"fmt"
	"math"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// Typed claims, not a stale flattened legacy answer, determine score exposure.
// Invalid or unknown contracts must fail the audit just as they fail grading.
func v13ClaimExposure(claims []protocol.Claim) (float64, map[string]float64, error) {
	var total, money float64
	kinds := map[string]float64{}
	for _, c := range claims {
		w := c.Weight
		if w == 0 {
			w = 1
		}
		if w < 0 || math.IsNaN(w) || math.IsInf(w, 0) || strings.TrimSpace(c.Expected) == "" {
			return 0, nil, fmt.Errorf("invalid semantic claim")
		}
		switch c.Kind {
		case protocol.ClaimKindEntity, protocol.ClaimKindConcept, protocol.ClaimKindOrder,
			protocol.ClaimKindValue, protocol.ClaimKindPerson, protocol.ClaimKindStatus,
			protocol.ClaimKindEvent, protocol.ClaimKindOrganisation, protocol.ClaimKindAction,
			protocol.ClaimKindChannel, protocol.ClaimKindDate, protocol.ClaimKindTime,
			protocol.ClaimKindSetMember, protocol.ClaimKindConflict, protocol.ClaimKindDirection:
		case protocol.ClaimKindQuantity:
			switch strings.ToLower(strings.TrimSpace(c.Unit)) {
			case "money", "minor", "cents", "usd", "eur", "gbp", "cad":
				money += w
			case "", "count", "units", "nights", "days", "seats", "hours", "licences", "percent", "percentage points":
			default:
				return 0, nil, fmt.Errorf("unclassified semantic quantity unit %q", c.Unit)
			}
		default:
			return 0, nil, fmt.Errorf("unclassified semantic claim %q", c.Kind)
		}
		total += w
		kinds[c.Kind] += w
	}
	if total == 0 || math.IsInf(total, 0) {
		return 0, nil, fmt.Errorf("invalid semantic claim weights")
	}
	for k, w := range kinds {
		kinds[k] = w / total
	}
	return money / total, kinds, nil
}

// Regenerate the latent story metadata rather than infer a domain from noisy
// question prose. IDs and question types must both match; unknown families
// remain fail-closed. This metadata never enters the harness projection.
func v13StoryMixFamilies(a DatasetArtifact) (map[string]mixFamily, error) {
	scale := 0
	for name, prof := range profilesV13 {
		env, _ := V13EnvelopeForRunSize(name)
		if env.Total() == len(a.MemoryCases) {
			scale, _ = v8WorldProfile(prof.Mem)
			break
		}
	}
	if scale == 0 {
		return nil, fmt.Errorf("mix audit: unknown v13 envelope %d", len(a.MemoryCases))
	}
	w := universe.GenerateForVersion(a.Seed, scale, a.BenchVersion)
	plans, _, err := w.SelectQuestionPlans(universe.WorldPlanSelection{StoryPerArc: 6, Salt: "v13"})
	if err != nil {
		return nil, err
	}
	out := map[string]mixFamily{}
	for _, p := range plans {
		arc := w.StoryArcs[p.StoryArcIndex()].V2
		domain := MixDomainBusiness
		if arc.Kind != universe.StoryBusiness {
			domain = MixDomainPersonal
		}
		op := strings.TrimPrefix(p.OracleKind(), "story-")
		arithmetic := false
		if op == "quantity" && arc.Quantity != nil {
			op = arc.Quantity.Op
			arithmetic = op == "add" || op == "subtract"
		}
		out[p.Case.ID+"/"+p.Case.QuestionType] = mixFamily{V13SlotStory, domain, string(arc.Theme), op, arithmetic, false}
	}
	return out, nil
}

func v13MixFamily(c ArtifactCase, stories map[string]mixFamily) (mixFamily, bool) {
	if f, ok := stories[c.ID+"/"+c.QuestionType]; ok {
		return f, true
	}
	switch c.QuestionType {
	case universe.V13ProgramQuestionType, universe.V13PersonalQuestionType, universe.V13EnterpriseQuestionType:
		if c.V10Provenance == nil || c.V10Provenance.Program.Op == "" {
			return mixFamily{}, false
		}
		f := mixFamily{V13SlotBusinessPrograms, MixDomainBusiness, "workflow", c.V10Provenance.Program.Op, false, true}
		if c.QuestionType == universe.V13PersonalQuestionType {
			f.slot, f.domain, f.subDomain = V13SlotPersonalPrograms, MixDomainPersonal, ""
			for _, term := range c.V10Provenance.Ontology {
				if term.Semantic == "personal-life domain" {
					for _, domain := range universe.V13PersonalDomains {
						if term.Wire == string(domain) {
							f.subDomain = term.Wire
						}
					}
				}
			}
			if f.subDomain == "" {
				return mixFamily{}, false
			}
		}
		return f, true
	case QTRecordQuantityMoney, QTRecordQuantityUnits, QTRecordQuantityCFBase, QTRecordQuantityCFVariant, QTRecordQuantityDirection:
		return mixFamily{V13SlotRecordQuantity, MixDomainBusiness, "accounts", "record-convention", true, false}, true
	case QTV13InjectionDataInside, QTV13InjectionEnvelopeFree, QTV13InjectionClassic:
		return mixFamily{V13SlotIntegrity, MixDomainBusiness, "untrusted-records", "attributed-select", false, false}, true
	}
	for _, family := range universe.V13AbsenceFamilies {
		if c.QuestionType != QTAbsence+family && c.QuestionType != QTAbsenceTwin+family {
			continue
		}
		domain, sub := MixDomainPersonal, "contacts"
		if family == universe.V13FamilyInsufficient {
			domain, sub = MixDomainBusiness, "projects"
		}
		if family == universe.V13FamilyFalsePremise {
			sub = "travel"
		}
		slot, op := V13SlotAbstention, "grounded-absence"
		if strings.HasPrefix(c.QuestionType, QTAbsenceTwin) {
			slot, op = V13SlotOrdinaryWorld, "evidence-select"
		}
		return mixFamily{slot, domain, sub, op, false, false}, true
	}
	for _, family := range []string{"contact", "invoice", "trip-leg"} {
		if c.QuestionType != QTPointInTime+family {
			continue
		}
		domain, sub := MixDomainPersonal, "contacts"
		if family == "invoice" {
			domain, sub = MixDomainBusiness, "projects"
		}
		if family == "trip-leg" {
			sub = "travel"
		}
		return mixFamily{V13SlotPointInTime, domain, sub, "as-of", c.AnswerKind == protocol.AnswerMoney, false}, true
	}
	return mixFamilyFor(c.QuestionType)
}
