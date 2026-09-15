package mixaudit

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// Summary aggregates per-seed reports across a sweep so caps and floors can be
// read as per-seed extremes (the envelope is per seed, never an average).
type Summary struct {
	BenchVersion int     `json:"bench_version"`
	RunSize      string  `json:"run_size"`
	Seeds        []int64 `json:"seeds"`
	MemoryCases  Range   `json:"memory_cases"`

	DirectMoneyCases     Range `json:"direct_money_cases"`
	MoneyBearingCases    Range `json:"money_bearing_cases"`
	MoneyShare           Range `json:"money_share"`
	MoneyOnlyCases       Range `json:"money_only_cases"`
	MonetaryOpenPrograms Range `json:"monetary_open_programs"`
	ArithmeticShare      Range `json:"arithmetic_share"`
	ComputedShare        Range `json:"computed_claim_share"`
	ComputedMoneyShare   Range `json:"computed_money_share_of_computed"`
	AbstentionShare      Range `json:"abstention_share"`
	TwinCoverage         Range `json:"twin_coverage"`
	GateExposedShare     Range `json:"gate_exposed_share"`
	CascadeShare         Range `json:"cascade_dependent_share"`
	SingleErrorCascade   Range `json:"max_single_error_cascade"`
	GIHParseRate         Range `json:"gih_parse_rate"`

	DomainShare        map[string]Range `json:"domain_share"`
	SubDomainShare     map[string]Range `json:"sub_domain_share"`
	OperationShare     map[string]Range `json:"operation_share"`
	KindOperationShare map[string]Range `json:"kind_operation_share"`
	AnswerKindShare    map[string]Range `json:"answer_kind_share"`
	LanguageShare      map[string]Range `json:"language_share"`
	Families           map[string]Range `json:"families"`

	// Violations lists, per seed, the v13 envelope rules that seed breaks.
	Violations map[string][]Violation `json:"violations"`
	// SeedsViolating is how many seeds break at least one rule.
	SeedsViolating int `json:"seeds_violating"`
}

// Range is the per-seed distribution of one measure.
type Range struct {
	Min  float64 `json:"min"`
	Max  float64 `json:"max"`
	Mean float64 `json:"mean"`
	N    int     `json:"n"`
}

func (r *Range) add(x float64) {
	if r.N == 0 || x < r.Min {
		r.Min = x
	}
	if r.N == 0 || x > r.Max {
		r.Max = x
	}
	r.Mean = (r.Mean*float64(r.N) + x) / float64(r.N+1)
	r.N++
}

func addShare(m map[string]Range, key string, x float64) {
	r := m[key]
	r.add(x)
	m[key] = r
}

// Summarize folds seed reports into a Summary and evaluates env on each seed.
func Summarize(reports []SeedReport, env Envelope) Summary {
	s := Summary{
		DomainShare: map[string]Range{}, SubDomainShare: map[string]Range{}, OperationShare: map[string]Range{},
		KindOperationShare: map[string]Range{}, AnswerKindShare: map[string]Range{}, LanguageShare: map[string]Range{},
		Families: map[string]Range{}, Violations: map[string][]Violation{},
	}
	// Every key seen anywhere gets a zero on seeds where it is absent, so a
	// family that drops out of one seed shows Min=0 rather than vanishing.
	keys := map[string]map[string]bool{"domain": {}, "sub": {}, "op": {}, "kop": {}, "kind": {}, "lang": {}, "fam": {}}
	for _, r := range reports {
		for k := range r.Domains {
			keys["domain"][k] = true
		}
		for k := range r.SubDomains {
			keys["sub"][k] = true
		}
		for k := range r.Operations {
			keys["op"][k] = true
		}
		for k := range r.KindOperation {
			keys["kop"][k] = true
		}
		for k := range r.AnswerKinds {
			keys["kind"][k] = true
		}
		for k := range r.Languages {
			keys["lang"][k] = true
		}
		for k := range r.Families {
			keys["fam"][k] = true
		}
	}
	for _, r := range reports {
		s.BenchVersion, s.RunSize = r.BenchVersion, r.RunSize
		s.Seeds = append(s.Seeds, r.Seed)
		s.MemoryCases.add(float64(r.MemoryCases))
		s.DirectMoneyCases.add(float64(r.DirectMoneyCases))
		s.MoneyBearingCases.add(float64(r.MoneyBearingCases))
		s.MoneyShare.add(r.MoneyShare)
		s.MoneyOnlyCases.add(float64(r.MoneyOnlyCases))
		s.MonetaryOpenPrograms.add(float64(r.MonetaryOpenPrograms))
		s.ArithmeticShare.add(r.ArithmeticShare)
		if r.EvidenceBoundWeight > 0 {
			s.ComputedShare.add(r.ComputedClaims / r.EvidenceBoundWeight)
		}
		if r.ComputedClaims > 0 {
			s.ComputedMoneyShare.add(r.ComputedMoneyWeight / r.ComputedClaims)
		}
		s.AbstentionShare.add(r.AbstentionShare)
		s.TwinCoverage.add(r.TwinCoverage)
		s.GateExposedShare.add(r.GateExposedShare)
		s.CascadeShare.add(r.CascadeDependentShare)
		s.SingleErrorCascade.add(r.MaxSingleErrorCascade)
		if r.GIHParseRate != nil {
			s.GIHParseRate.add(*r.GIHParseRate)
		}
		w := r.Weight
		for k := range keys["domain"] {
			addShare(s.DomainShare, k, r.Domains[k]/w)
		}
		for k := range keys["sub"] {
			addShare(s.SubDomainShare, k, r.SubDomains[k]/w)
		}
		for k := range keys["op"] {
			addShare(s.OperationShare, k, r.Operations[k]/w)
		}
		for k := range keys["kop"] {
			addShare(s.KindOperationShare, k, r.KindOperation[k]/w)
		}
		for k := range keys["kind"] {
			addShare(s.AnswerKindShare, k, r.AnswerKinds[k]/w)
		}
		for k := range keys["lang"] {
			addShare(s.LanguageShare, k, r.Languages[k]/w)
		}
		for k := range keys["fam"] {
			addShare(s.Families, k, float64(r.Families[k]))
		}
		if v := env.Check(r); len(v) > 0 {
			s.Violations[fmt.Sprintf("%d", r.Seed)] = v
			s.SeedsViolating++
		}
	}
	return s
}

// Markdown renders the summary in the docs/v9-family-mix-study.md table style.
func (s Summary) Markdown(env Envelope) string {
	var b strings.Builder
	fmt.Fprintf(&b, "## Bench v%d `%s` memory mix over %d seed(s)\n\n", s.BenchVersion, s.RunSize, len(s.Seeds))
	fmt.Fprintf(&b, "| Measure | Min | Mean | Max | v13 limit |\n| --- | ---: | ---: | ---: | ---: |\n")
	row := func(name string, r Range, pct bool, limit string) {
		if r.N == 0 {
			fmt.Fprintf(&b, "| %s | – | – | – | %s |\n", name, limit)
			return
		}
		f := func(x float64) string {
			if pct {
				return fmt.Sprintf("%.1f%%", 100*x)
			}
			return fmt.Sprintf("%.4g", x)
		}
		fmt.Fprintf(&b, "| %s | %s | %s | %s | %s |\n", name, f(r.Min), f(r.Mean), f(r.Max), limit)
	}
	row("Memory cases", s.MemoryCases, false, fmt.Sprintf("= %d", env.MemoryCasesTarget))
	row("Direct `money` cases", s.DirectMoneyCases, false, "")
	row("Money-bearing cases", s.MoneyBearingCases, false, fmt.Sprintf("≤ %d", env.MoneyBearingCasesCap))
	row("Money share of memory weight", s.MoneyShare, true, fmt.Sprintf("≤ %.0f%% (target %.0f%%)", 100*env.MoneyWeightHardCap, 100*env.MoneyWeightTarget))
	row("Money-only cases", s.MoneyOnlyCases, false, "")
	row("Monetary open programs", s.MonetaryOpenPrograms, false, fmt.Sprintf("= %d", env.MonetaryOpenPrograms))
	row("Arithmetic-required share", s.ArithmeticShare, true, fmt.Sprintf("≤ %.0f%%", 100*env.ArithmeticShareCap))
	row("Computed share of evidence-bound claims", s.ComputedShare, true, "")
	row("Money share of computed claims", s.ComputedMoneyShare, true, "")
	row("Abstention share", s.AbstentionShare, true, fmt.Sprintf("%.0f%% ± %.0f%%", 100*env.AbstentionShareTarget, 100*env.AbstentionShareBand))
	row("Twin/metamorphic coverage", s.TwinCoverage, true, fmt.Sprintf("≥ %.0f%%", 100*env.TwinCoverageFloor))
	row("Gate-exposed share", s.GateExposedShare, true, fmt.Sprintf("≤ %.0f%%", 100*env.GateExposedShareCap))
	row("Cascade-dependent share", s.CascadeShare, true, "")
	row("Max single-error cascade", s.SingleErrorCascade, true, fmt.Sprintf("≤ %.0f%%", 100*env.SingleErrorCascadeCap))
	row("GIH parse rate (cmd/parserprobe)", s.GIHParseRate, true, "report-only")
	b.WriteString("\n")
	table := func(title string, m map[string]Range, limit string) {
		fmt.Fprintf(&b, "### %s\n\n| Key | Min | Mean | Max |\n| --- | ---: | ---: | ---: |\n", title)
		keys := make([]string, 0, len(m))
		for k := range m {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			if m[keys[i]].Mean != m[keys[j]].Mean {
				return m[keys[i]].Mean > m[keys[j]].Mean
			}
			return keys[i] < keys[j]
		})
		for _, k := range keys {
			r := m[k]
			fmt.Fprintf(&b, "| `%s` | %.1f%% | %.1f%% | %.1f%% |\n", k, 100*r.Min, 100*r.Mean, 100*r.Max)
		}
		if limit != "" {
			fmt.Fprintf(&b, "\nv13 limit: %s.\n", limit)
		}
		b.WriteString("\n")
	}
	table("Domain share of memory weight", s.DomainShare, fmt.Sprintf("personal ≥ %.0f%%, business ≥ %.0f%%", 100*env.PersonalWeightFloor, 100*env.BusinessWeightFloor))
	table("Sub-domain share of memory weight", s.SubDomainShare, fmt.Sprintf("each ≤ %.0f%%", 100*env.SubDomainWeightCap))
	table("Operation share of memory weight", s.OperationShare, "")
	table("Answer kind × operation share", s.KindOperationShare, fmt.Sprintf("each ≤ %.0f%%", 100*env.KindOperationWeightCap))
	table("Answer kind share", s.AnswerKindShare, "")
	table("Language share", s.LanguageShare, "")
	fmt.Fprintf(&b, "### Families (cases per seed)\n\n| Family | Min | Mean | Max |\n| --- | ---: | ---: | ---: |\n")
	fams := make([]string, 0, len(s.Families))
	for k := range s.Families {
		fams = append(fams, k)
	}
	sort.Slice(fams, func(i, j int) bool {
		if s.Families[fams[i]].Mean != s.Families[fams[j]].Mean {
			return s.Families[fams[i]].Mean > s.Families[fams[j]].Mean
		}
		return fams[i] < fams[j]
	})
	for _, k := range fams {
		r := s.Families[k]
		fmt.Fprintf(&b, "| `%s` | %.0f | %.2f | %.0f |\n", k, r.Min, r.Mean, r.Max)
	}
	b.WriteString("\n")
	fmt.Fprintf(&b, "### v13 envelope check\n\n%d of %d seed(s) violate at least one rule.\n\n", s.SeedsViolating, len(s.Seeds))
	if s.SeedsViolating > 0 {
		// Rules are reported once with the number of seeds breaking them, so a
		// 40-seed sweep stays readable.
		counts := map[string]int{}
		worst := map[string]float64{}
		for _, vs := range s.Violations {
			for _, v := range vs {
				if _, seen := worst[v.Rule]; !seen || math.Abs(v.Observed-v.Limit) > math.Abs(worst[v.Rule]-v.Limit) {
					worst[v.Rule] = v.Observed
				}
				counts[v.Rule]++
			}
		}
		rules := make([]string, 0, len(counts))
		for r := range counts {
			rules = append(rules, r)
		}
		sort.Strings(rules)
		fmt.Fprintf(&b, "| Rule | Seeds violating | Worst observed |\n| --- | ---: | ---: |\n")
		for _, r := range rules {
			fmt.Fprintf(&b, "| %s | %d | %.4f |\n", r, counts[r], worst[r])
		}
		b.WriteString("\n")
	}
	return b.String()
}
