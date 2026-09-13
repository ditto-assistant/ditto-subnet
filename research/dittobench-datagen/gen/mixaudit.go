package gen

import (
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Deterministic memory-mix audit (issues #1529 / #1830 / #1848).
//
// #1529 found that monetary claims owned 50.9% of the v12 memory score weight
// and that a plain AnswerMoney count (46.6%) under-reports it: the story
// net-change and outcome-summary oracles carry a money item inside a typed
// list. The audit therefore weights every case by its share of case credit and
// classifies every case along the axes the v13 caps are written in:
//
//   - slot: which published V13Envelope slot the case fills;
//   - domain / sub-domain: personal vs business, and the finer topic;
//   - answer kind x operation: the anti-monoculture axis (whatever replaces
//     money must not become the next single parse target);
//   - money-bearing weight, arithmetic-required, computed-vs-verbatim (the
//     AnswerVerbatimInEvidence rule over the case's declared evidence);
//   - twin / gate-exposed / dependency cluster: how much composite a single
//     honest error, or a gate rather than a wrong answer, can zero.
//
// Classification is a closed table keyed by QuestionType. An unknown question
// type or answer kind is an ERROR, never a silent "other": a new family cannot
// bypass the histogram by being unclassified. Weights are per case (1.0), so
// the denominators equal the memory-case count and the v12 figures in #1529
// (117 / 143 / 127.83 of 251) reproduce exactly (TestMixAuditReproducesV12).

// MixCaseClass is the classification of one memory case.
type MixCaseClass struct {
	CaseID       string  `json:"case_id"`
	QuestionType string  `json:"question_type"`
	Slot         string  `json:"slot"`
	Domain       string  `json:"domain"`
	SubDomain    string  `json:"sub_domain"`
	AnswerKind   string  `json:"answer_kind"`
	Operation    string  `json:"operation"`
	MoneyWeight  float64 `json:"money_weight"`
	MoneyBearing bool    `json:"money_bearing"`
	MoneyOnly    bool    `json:"money_only"`
	Arithmetic   bool    `json:"arithmetic"`
	// EvidenceBound is true when the case declares evidence pairs (v10+) and has
	// a graded expected answer; Computed then reports whether that answer is
	// absent from the declared evidence.
	EvidenceBound bool   `json:"evidence_bound"`
	Computed      bool   `json:"computed"`
	OpenProgram   bool   `json:"open_program"`
	Twin          bool   `json:"twin"`
	GateExposed   bool   `json:"gate_exposed"`
	Dependency    string `json:"dependency,omitempty"`
}

// MixAudit is the per-run histogram.
type MixAudit struct {
	BenchVersion int            `json:"bench_version"`
	Seed         int64          `json:"seed"`
	Cases        int            `json:"cases"`
	Weight       float64        `json:"weight"`
	Slots        map[string]int `json:"slots"`
	// InterimSlots names the v13 slots currently filled by the interim world
	// fill (their cases are counted under ordinary-world). Empty before v13 and
	// once every v13 generator has landed.
	InterimSlots []string `json:"interim_slots,omitempty"`
	// InterimGenerators names the v13 slots whose count is final but whose
	// cases still come from a monetary v12-era generator (V13InterimGenerators).
	InterimGenerators []string `json:"interim_generators,omitempty"`

	MoneyWeight          float64 `json:"money_weight"`
	MoneyCases           int     `json:"money_cases"`
	MoneyOnlyCases       int     `json:"money_only_cases"`
	MonetaryOpenPrograms int     `json:"monetary_open_programs"`
	OpenPrograms         int     `json:"open_programs"`
	ArithmeticCases      int     `json:"arithmetic_cases"`
	EvidenceBoundCases   int     `json:"evidence_bound_cases"`
	ComputedCases        int     `json:"computed_cases"`
	ComputedMoneyCases   int     `json:"computed_money_cases"`
	AbstentionCases      int     `json:"abstention_cases"`
	ProjectOutstanding   int     `json:"project_outstanding_cases"`

	Domains        map[string]float64 `json:"domains"`
	SubDomains     map[string]float64 `json:"sub_domains"`
	KindOperations map[string]float64 `json:"kind_operations"`
	AnswerKinds    map[string]int     `json:"answer_kinds"`
	Families       map[string]int     `json:"families"`

	TwinWeight        float64 `json:"twin_weight"`
	GateExposedWeight float64 `json:"gate_exposed_weight"`
	DependentWeight   float64 `json:"dependent_weight"`
	// MaxDependencyCluster is the weight of the largest set of cases whose
	// scores depend on one another (a metamorphic group or a counterfactual
	// pair): the most memory weight one honest error can cascade into.
	MaxDependencyCluster float64 `json:"max_dependency_cluster"`

	Classes []MixCaseClass `json:"classes,omitempty"`
}

// Share helpers (fractions of memory weight).
func (a MixAudit) MoneyShare() float64       { return safeDiv(a.MoneyWeight, a.Weight) }
func (a MixAudit) ArithmeticShare() float64  { return safeDiv(float64(a.ArithmeticCases), a.Weight) }
func (a MixAudit) AbstentionShare() float64  { return safeDiv(float64(a.AbstentionCases), a.Weight) }
func (a MixAudit) TwinShare() float64        { return safeDiv(a.TwinWeight, a.Weight) }
func (a MixAudit) GateExposedShare() float64 { return safeDiv(a.GateExposedWeight, a.Weight) }
func (a MixAudit) CascadeShare() float64     { return safeDiv(a.MaxDependencyCluster, a.Weight) }
func (a MixAudit) ComputedMoneyShare() float64 {
	return safeDiv(float64(a.ComputedMoneyCases), float64(a.ComputedCases))
}
func (a MixAudit) DomainShare(domain string) float64 { return safeDiv(a.Domains[domain], a.Weight) }

func safeDiv(n, d float64) float64 {
	if d == 0 {
		return 0
	}
	return n / d
}

// Domain labels.
const (
	MixDomainPersonal = "personal"
	MixDomainBusiness = "business"
)

// mixFamily is one row of the closed classification table.
type mixFamily struct {
	slot       string
	domain     string
	subDomain  string
	operation  string
	arithmetic bool
	program    bool
}

// mixFamilies keys exact QuestionType strings. Program catalogs are matched by
// suffix in mixFamilyFor because every version names its own
// ("v10-open-program", "v11-open-program", ...).
var mixFamilies = map[string]mixFamily{
	// Shared-world story oracles (universe/questions.go storyQuestionCandidates).
	"world-story-balance-current":       {V13SlotStory, MixDomainBusiness, "finance", "balance", true, false},
	"world-story-post-approval-balance": {V13SlotStory, MixDomainBusiness, "finance", "balance", true, false},
	"world-story-budget-delta":          {V13SlotStory, MixDomainBusiness, "finance", "extract-amount", false, false},
	"world-story-later-net-change":      {V13SlotStory, MixDomainBusiness, "finance", "net-change", true, false},
	"world-story-outcome-summary":       {V13SlotStory, MixDomainBusiness, "finance", "summary", true, false},
	"world-story-contact-current":       {V13SlotStory, MixDomainBusiness, "vendor-contact", "current-channel", false, false},
	"world-story-lesson":                {V13SlotStory, MixDomainPersonal, "reflection", "select-lesson", false, false},
	// Ordinary shared-world oracles.
	"world-contact-current":           {V13SlotOrdinaryWorld, MixDomainPersonal, "contacts", "current-channel", false, false},
	"world-contact-previous":          {V13SlotOrdinaryWorld, MixDomainPersonal, "contacts", "prior-state", false, false},
	"world-project-outstanding":       {V13SlotOrdinaryWorld, MixDomainBusiness, "projects", "balance", true, false},
	"world-project-lead-current":      {V13SlotOrdinaryWorld, MixDomainBusiness, "projects", "current-channel", false, false},
	"world-project-lead-previous":     {V13SlotOrdinaryWorld, MixDomainBusiness, "projects", "prior-state", false, false},
	"world-trip-current":              {V13SlotOrdinaryWorld, MixDomainPersonal, "travel", "sum", true, false},
	"world-trip-longest-current":      {V13SlotOrdinaryWorld, MixDomainPersonal, "travel", "max", true, false},
	"world-trip-changed-leg-previous": {V13SlotOrdinaryWorld, MixDomainPersonal, "travel", "prior-state", false, false},
	"world-trip-changed-leg-current":  {V13SlotOrdinaryWorld, MixDomainPersonal, "travel", "corrected-value", false, false},
	// v12 family compiler (gen/familycompiler.go): the interim record-quantity slot.
	QTRecordBalancePlain:      {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceAdjusted:   {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceSuperseded: {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceCapped:     {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceForgiven:   {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", false, false},
	QTRecordBalanceReferred:   {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceCFBase:     {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	QTRecordBalanceCFVariant:  {V13SlotRecordQuantity, MixDomainBusiness, "accounts", "balance", true, false},
	// Parser divergence (gen/divergence.go).
	QTParserDivergenceNegation:     {V13SlotDivergence, MixDomainPersonal, "household", "divergent-reading", false, false},
	QTParserDivergenceRetraction:   {V13SlotDivergence, MixDomainPersonal, "household", "divergent-reading", false, false},
	QTParserDivergenceHypothetical: {V13SlotDivergence, MixDomainBusiness, "finance", "divergent-reading", false, false},
	QTParserDivergenceReported:     {V13SlotDivergence, MixDomainPersonal, "household", "divergent-reading", false, false},
	// World-native integrity tail (gen/memory_v2.go v8WorldIntegrityCases).
	QTChitchat:                   {V13SlotIntegrity, MixDomainPersonal, "preferences", "chitchat", false, false},
	QTDeclarativeAck:             {V13SlotIntegrity, MixDomainPersonal, "preferences", "acknowledge", false, false},
	QTDeclarativeBehavior:        {V13SlotIntegrity, MixDomainPersonal, "preferences", "apply-preference", false, false},
	"world-canary":               {V13SlotIntegrity, MixDomainPersonal, "events", "attributed-select", false, false},
	"world-injection-resistance": {V13SlotIntegrity, MixDomainBusiness, "finance", "balance", true, false},
	// Cross-user isolation (gen/isolation.go).
	"world-isolation-contact-current": {V13SlotIsolation, MixDomainPersonal, "contacts", "current-channel", false, false},
}

var openProgramFamily = mixFamily{V13SlotBusinessPrograms, MixDomainBusiness, "finance", "balance", true, true}

func mixFamilyFor(questionType string) (mixFamily, bool) {
	if family, ok := mixFamilies[questionType]; ok {
		return family, true
	}
	if strings.HasSuffix(questionType, "-open-program") {
		return openProgramFamily, true
	}
	return mixFamily{}, false
}

var knownAnswerKinds = map[string]bool{
	"": true, protocol.AnswerValue: true, protocol.AnswerNumber: true, protocol.AnswerMoney: true,
	protocol.AnswerDirection: true, protocol.AnswerList: true, protocol.AnswerOrderedList: true,
	protocol.AnswerDuration: true, protocol.AnswerReversal: true, protocol.AnswerPersistence: true,
	protocol.AnswerDecline: true, protocol.AnswerAcknowledge: true, protocol.AnswerChitchat: true,
}

// AuditMix classifies every memory case of a generated artifact. It fails on an
// unclassified question type or answer kind so a new family cannot bypass the
// #1529 caps. Evidence-bound computed/verbatim classification uses the
// artifact's own seeded pairs (tool prerequisites plus memory waves).
func AuditMix(artifact DatasetArtifact) (MixAudit, error) {
	pairs := make(map[string]string)
	for _, tc := range artifact.ToolCases {
		for _, pair := range tc.PrerequisitePairs {
			pairs[pair.PairID] = pair.Prompt + " " + pair.Response
		}
	}
	for _, wave := range artifact.MemoryWaves {
		for _, pair := range wave.Pairs {
			pairs[pair.PairID] = pair.Prompt + " " + pair.Response
		}
	}
	audit := MixAudit{
		BenchVersion:   artifact.BenchVersion,
		Seed:           artifact.Seed,
		Slots:          map[string]int{},
		Domains:        map[string]float64{},
		SubDomains:     map[string]float64{},
		KindOperations: map[string]float64{},
		AnswerKinds:    map[string]int{},
		Families:       map[string]int{},
	}
	if artifact.BenchVersion >= protocol.BenchVersionV13 {
		audit.InterimSlots = V13InterimSlots()
		audit.InterimGenerators = V13InterimGenerators()
	}
	clusters := map[string]float64{}
	pendingCFBase := ""
	cfPairs := 0
	for _, c := range artifact.MemoryCases {
		family, ok := mixFamilyFor(c.QuestionType)
		if !ok {
			return MixAudit{}, fmt.Errorf("mix audit: unclassified question type %q (case %s)", c.QuestionType, c.ID)
		}
		if !knownAnswerKinds[c.AnswerKind] {
			return MixAudit{}, fmt.Errorf("mix audit: unclassified answer kind %q (case %s)", c.AnswerKind, c.ID)
		}
		for _, kind := range c.AnswerItemKinds {
			if !knownAnswerKinds[kind] {
				return MixAudit{}, fmt.Errorf("mix audit: unclassified list item kind %q (case %s)", kind, c.ID)
			}
		}
		kind := c.AnswerKind
		if kind == "" {
			kind = protocol.AnswerValue
		}
		class := MixCaseClass{
			CaseID: c.ID, QuestionType: c.QuestionType, Slot: family.slot,
			Domain: family.domain, SubDomain: family.subDomain, AnswerKind: kind,
			Operation: family.operation, Arithmetic: family.arithmetic, OpenProgram: family.program,
		}
		switch kind {
		case protocol.AnswerMoney:
			class.MoneyWeight, class.MoneyBearing, class.MoneyOnly = 1, true, true
		case protocol.AnswerList, protocol.AnswerOrderedList:
			if len(c.AnswerItems) > 0 {
				money := 0
				for _, itemKind := range c.AnswerItemKinds {
					if itemKind == protocol.AnswerMoney {
						money++
					}
				}
				class.MoneyWeight = float64(money) / float64(len(c.AnswerItems))
				class.MoneyBearing = money > 0
				// A list whose every item is money is money-only at weight 1.0.
				class.MoneyOnly = money > 0 && money == len(c.AnswerItems)
			}
		}
		if c.ExpectedAnswer != "" && kind != protocol.AnswerChitchat && len(c.V10EvidencePairIDs) > 0 {
			class.EvidenceBound = true
			var evidence strings.Builder
			for _, pairID := range c.V10EvidencePairIDs {
				text, ok := pairs[pairID]
				if !ok {
					return MixAudit{}, fmt.Errorf("mix audit: case %s declares unseeded evidence pair %s", c.ID, pairID)
				}
				evidence.WriteByte(' ')
				evidence.WriteString(text)
			}
			class.Computed = !AnswerVerbatimInEvidence(evidence.String(), c.ExpectedAnswer)
		}
		// Dependency clusters: metamorphic groups (TwinGroup / provenance) and the
		// family-compiler counterfactual pairs, which the generator emits as
		// adjacent base -> variant cases.
		switch {
		case c.TwinGroup != "":
			class.Dependency = "twin:" + c.TwinGroup
		case c.V10Provenance != nil && c.V10Provenance.MetamorphicGroup != "":
			class.Dependency = "group:" + c.V10Provenance.MetamorphicGroup
		case c.QuestionType == QTRecordBalanceCFBase:
			cfPairs++
			pendingCFBase = fmt.Sprintf("cf:%d", cfPairs)
			class.Dependency = pendingCFBase
		case c.QuestionType == QTRecordBalanceCFVariant:
			if pendingCFBase == "" {
				return MixAudit{}, fmt.Errorf("mix audit: counterfactual variant %s has no preceding base", c.ID)
			}
			class.Dependency = pendingCFBase
			pendingCFBase = ""
		}
		class.Twin = class.Dependency != ""
		class.GateExposed = class.Twin || c.V10Provenance != nil
		if class.Dependency != "" {
			clusters[class.Dependency]++
			audit.DependentWeight++
		}

		audit.Cases++
		audit.Weight++
		audit.Slots[class.Slot]++
		audit.Families[c.QuestionType]++
		audit.AnswerKinds[kind]++
		audit.Domains[class.Domain]++
		audit.SubDomains[class.Domain+"/"+class.SubDomain]++
		audit.KindOperations[kind+"/"+class.Operation]++
		audit.MoneyWeight += class.MoneyWeight
		if class.MoneyBearing {
			audit.MoneyCases++
		}
		if class.MoneyOnly {
			audit.MoneyOnlyCases++
		}
		if class.OpenProgram {
			audit.OpenPrograms++
			if class.MoneyBearing {
				audit.MonetaryOpenPrograms++
			}
		}
		if class.Arithmetic {
			audit.ArithmeticCases++
		}
		if class.EvidenceBound {
			audit.EvidenceBoundCases++
			if class.Computed {
				audit.ComputedCases++
				if class.MoneyBearing {
					audit.ComputedMoneyCases++
				}
			}
		}
		if kind == protocol.AnswerDecline {
			audit.AbstentionCases++
		}
		if c.QuestionType == "world-project-outstanding" {
			audit.ProjectOutstanding++
		}
		if class.Twin {
			audit.TwinWeight++
		}
		if class.GateExposed {
			audit.GateExposedWeight++
		}
		audit.Classes = append(audit.Classes, class)
	}
	if pendingCFBase != "" {
		return MixAudit{}, fmt.Errorf("mix audit: counterfactual base %s has no variant", pendingCFBase)
	}
	for _, weight := range clusters {
		if weight > audit.MaxDependencyCluster {
			audit.MaxDependencyCluster = weight
		}
	}
	return audit, nil
}

// MixGate is the set of caps and floors a per-seed audit must satisfy. Every
// share is a fraction of memory weight unless stated otherwise.
type MixGate struct {
	MoneyWeightMax          float64 // hard cap on monetary claim weight
	MoneyWeightTarget       float64 // reported, not enforced
	MoneyCasesMax           int     // cases carrying any monetary claim
	MonetaryOpenProgramsMax int
	ArithmeticMax           float64
	MoneyOfComputedMax      float64 // money share of evidence-bound computed answers
	PersonalMin             float64
	BusinessMin             float64
	SubDomainMax            float64
	KindOperationMax        float64
	AbstentionTarget        float64
	AbstentionTolerance     float64
	TwinCoverageMin         float64
	GateExposedMax          float64
	CascadeMax              float64 // largest dependency cluster
	ProjectOutstandingMax   int
}

// MixGateV13 is the #1529 / #1848 gate (Owner decision - default taken:
// <= 12% target / 15% hard).
var MixGateV13 = MixGate{
	MoneyWeightMax: 0.15, MoneyWeightTarget: 0.12, MoneyCasesMax: 22, MonetaryOpenProgramsMax: 0,
	ArithmeticMax: 0.20, MoneyOfComputedMax: 0.25,
	PersonalMin: 0.30, BusinessMin: 0.40, SubDomainMax: 0.20, KindOperationMax: 0.15,
	AbstentionTarget: 0.10, AbstentionTolerance: 0.01,
	TwinCoverageMin: 0.40, GateExposedMax: 0.40, CascadeMax: 0.04,
	ProjectOutstandingMax: V13OrdinaryProjectOutstandingCap,
}

// StructuralViolations are the bounds the interim v13 envelope must already
// satisfy: they are upper bounds on exposure that no generator swap may widen.
func (g MixGate) StructuralViolations(a MixAudit) []string {
	var out []string
	if share := a.GateExposedShare(); share > g.GateExposedMax {
		out = append(out, fmt.Sprintf("gate-exposed share %.4f > %.2f", share, g.GateExposedMax))
	}
	if share := a.CascadeShare(); share > g.CascadeMax {
		out = append(out, fmt.Sprintf("largest dependency cluster %.4f of memory weight > %.2f", share, g.CascadeMax))
	}
	if a.ProjectOutstanding > g.ProjectOutstandingMax {
		out = append(out, fmt.Sprintf("project-outstanding cases %d > %d", a.ProjectOutstanding, g.ProjectOutstandingMax))
	}
	return out
}

// Violations evaluates the whole gate. The result is empty when every cap and
// floor holds.
func (g MixGate) Violations(a MixAudit) []string {
	out := g.StructuralViolations(a)
	if share := a.MoneyShare(); share > g.MoneyWeightMax {
		out = append(out, fmt.Sprintf("money weight %.4f > %.2f hard cap", share, g.MoneyWeightMax))
	}
	if a.MoneyCases > g.MoneyCasesMax {
		out = append(out, fmt.Sprintf("money-bearing cases %d > %d", a.MoneyCases, g.MoneyCasesMax))
	}
	if a.MonetaryOpenPrograms > g.MonetaryOpenProgramsMax {
		out = append(out, fmt.Sprintf("monetary open programs %d > %d", a.MonetaryOpenPrograms, g.MonetaryOpenProgramsMax))
	}
	if share := a.ArithmeticShare(); share > g.ArithmeticMax {
		out = append(out, fmt.Sprintf("arithmetic-required share %.4f > %.2f", share, g.ArithmeticMax))
	}
	if share := a.ComputedMoneyShare(); share > g.MoneyOfComputedMax {
		out = append(out, fmt.Sprintf("money share of computed answers %.4f > %.2f", share, g.MoneyOfComputedMax))
	}
	if share := a.DomainShare(MixDomainPersonal); share < g.PersonalMin {
		out = append(out, fmt.Sprintf("personal share %.4f < %.2f floor", share, g.PersonalMin))
	}
	if share := a.DomainShare(MixDomainBusiness); share < g.BusinessMin {
		out = append(out, fmt.Sprintf("business share %.4f < %.2f floor", share, g.BusinessMin))
	}
	for _, key := range sortedKeys(a.SubDomains) {
		if share := safeDiv(a.SubDomains[key], a.Weight); share > g.SubDomainMax {
			out = append(out, fmt.Sprintf("sub-domain %s share %.4f > %.2f", key, share, g.SubDomainMax))
		}
	}
	for _, key := range sortedKeys(a.KindOperations) {
		if share := safeDiv(a.KindOperations[key], a.Weight); share > g.KindOperationMax {
			out = append(out, fmt.Sprintf("answer-kind x operation %s share %.4f > %.2f", key, share, g.KindOperationMax))
		}
	}
	if share := a.AbstentionShare(); share < g.AbstentionTarget-g.AbstentionTolerance || share > g.AbstentionTarget+g.AbstentionTolerance {
		out = append(out, fmt.Sprintf("abstention share %.4f outside %.2f +/- %.2f", share, g.AbstentionTarget, g.AbstentionTolerance))
	}
	if share := a.TwinShare(); share < g.TwinCoverageMin {
		out = append(out, fmt.Sprintf("twin coverage %.4f < %.2f floor", share, g.TwinCoverageMin))
	}
	return out
}

func sortedKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// MixAuditSummary is the compact per-seed row cmd/mixaudit prints.
type MixAuditSummary struct {
	Seed                  int64          `json:"seed"`
	Cases                 int            `json:"cases"`
	MoneyShare            float64        `json:"money_share"`
	MoneyCases            int            `json:"money_cases"`
	MonetaryOpenPrograms  int            `json:"monetary_open_programs"`
	ArithmeticShare       float64        `json:"arithmetic_share"`
	ComputedMoneyShare    float64        `json:"computed_money_share"`
	PersonalShare         float64        `json:"personal_share"`
	BusinessShare         float64        `json:"business_share"`
	MaxSubDomain          string         `json:"max_sub_domain"`
	MaxSubDomainShare     float64        `json:"max_sub_domain_share"`
	MaxKindOperation      string         `json:"max_kind_operation"`
	MaxKindOperationShare float64        `json:"max_kind_operation_share"`
	AbstentionShare       float64        `json:"abstention_share"`
	TwinShare             float64        `json:"twin_share"`
	GateExposedShare      float64        `json:"gate_exposed_share"`
	CascadeShare          float64        `json:"cascade_share"`
	Slots                 map[string]int `json:"slots"`
	Violations            []string       `json:"violations,omitempty"`
}

// Summarize reduces an audit to its gate row.
func (a MixAudit) Summarize(gate MixGate) MixAuditSummary {
	maxKey := func(m map[string]float64) (string, float64) {
		best, bestWeight := "", 0.0
		for _, key := range sortedKeys(m) {
			if m[key] > bestWeight {
				best, bestWeight = key, m[key]
			}
		}
		return best, safeDiv(bestWeight, a.Weight)
	}
	subDomain, subDomainShare := maxKey(a.SubDomains)
	kindOperation, kindOperationShare := maxKey(a.KindOperations)
	return MixAuditSummary{
		Seed: a.Seed, Cases: a.Cases,
		MoneyShare: a.MoneyShare(), MoneyCases: a.MoneyCases, MonetaryOpenPrograms: a.MonetaryOpenPrograms,
		ArithmeticShare: a.ArithmeticShare(), ComputedMoneyShare: a.ComputedMoneyShare(),
		PersonalShare: a.DomainShare(MixDomainPersonal), BusinessShare: a.DomainShare(MixDomainBusiness),
		MaxSubDomain: subDomain, MaxSubDomainShare: subDomainShare,
		MaxKindOperation: kindOperation, MaxKindOperationShare: kindOperationShare,
		AbstentionShare: a.AbstentionShare(), TwinShare: a.TwinShare(),
		GateExposedShare: a.GateExposedShare(), CascadeShare: a.CascadeShare(),
		Slots: a.Slots, Violations: gate.Violations(a),
	}
}
