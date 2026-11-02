package mixaudit

import "github.com/ditto-assistant/dittobench-datagen/gen"

// auditCurrent adapts the canonical envelope audit. The historical classifier
// below remains frozen for v12 reproduction; it must not classify v13 using
// obsolete monetary program names or flattened composite answers.
func auditCurrent(a gen.DatasetArtifact, runSize string, keepCases bool) (SeedReport, error) {
	m, err := gen.AuditMix(a)
	if err != nil {
		return SeedReport{}, err
	}
	r := SeedReport{
		Seed: a.Seed, BenchVersion: a.BenchVersion, RunSize: runSize,
		MemoryCases: m.Cases, ToolCases: len(a.ToolCases), Weight: m.Weight,
		MoneyBearingCases: m.MoneyCases, MoneyWeight: m.MoneyWeight, MoneyShare: m.MoneyShare(),
		MoneyOnlyCases: m.MoneyOnlyCases, MonetaryOpenPrograms: m.MonetaryOpenPrograms,
		OpenPrograms: m.OpenPrograms, ArithmeticCases: m.ArithmeticCases, ArithmeticShare: m.ArithmeticShare(),
		ComputedClaims: float64(m.ComputedCases), VerbatimClaims: float64(m.EvidenceBoundCases - m.ComputedCases),
		EvidenceBoundWeight: float64(m.EvidenceBoundCases), ComputedMoneyWeight: float64(m.ComputedMoneyCases),
		AbstentionCases: m.AbstentionCases, AbstentionShare: m.AbstentionShare(),
		TwinCoveredWeight: m.TwinWeight, TwinCoverage: m.TwinShare(),
		GateExposedWeight: m.GateExposedWeight, GateExposedShare: m.GateExposedShare(),
		CascadeDependentWeight: m.DependentWeight, MaxSingleErrorCascade: m.CascadeShare(),
		Families: m.Families, Domains: m.Domains, SubDomains: m.SubDomains, KindOperation: m.KindOperations,
		AnswerKinds: map[string]float64{}, Operations: map[string]float64{}, Languages: map[string]float64{},
		Gates: map[string]float64{}, Relations: map[string]int{},
	}
	if m.Weight > 0 {
		r.CascadeDependentShare = m.DependentWeight / m.Weight
	}
	for k, v := range m.AnswerKinds {
		r.AnswerKinds[k] = float64(v)
	}
	for i, c := range m.Classes {
		ac := a.MemoryCases[i]
		if c.AnswerKind == "money" {
			r.DirectMoneyCases++
		}
		r.Operations[c.Operation]++
		lang := ac.Language
		if lang == "" {
			lang = "en"
		}
		r.Languages[lang]++
		cc := CaseClass{CaseID: c.CaseID, Family: c.QuestionType, AnswerKind: c.AnswerKind,
			Domain: c.Domain, SubDomain: c.SubDomain, Operation: c.Operation, MoneyWeight: c.MoneyWeight,
			MoneyBearing: c.MoneyBearing, Arithmetic: c.Arithmetic, EvidenceBound: c.EvidenceBound,
			Language: lang, CascadeDependent: c.Dependency != "", OpenProgram: c.OpenProgram,
			Abstention: c.AnswerKind == "absence" || c.AnswerKind == "clarify", UserID: ac.UserID}
		if c.Twin {
			cc.TwinRelation = "paired"
			r.Relations["paired"]++
		}
		if c.GateExposed {
			cc.Gates = []string{GatePair}
			r.Gates[GatePair]++
		}
		if keepCases {
			r.Cases = append(r.Cases, cc)
		}
	}
	return r, nil
}
