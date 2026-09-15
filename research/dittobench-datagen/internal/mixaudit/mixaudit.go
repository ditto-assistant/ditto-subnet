// Package mixaudit classifies every memory case of a generated DittoBench
// artifact along the axes the Bench v13 mix envelope is defined on — family,
// semantic domain, answer kind, operation, monetary exposure, arithmetic,
// computed-vs-verbatim, language, twin relation, and gate exposure — and
// weights composite (list) answers by the fraction of case credit each claim
// carries. It is the deterministic histogram behind cmd/mixaudit and the
// gen/mixaudit_test.go envelope gate.
//
// Weighting contract (issue #1529): a case contributes weight 1 to the memory
// denominator. A list answer splits that weight evenly across its items (the
// grader credits the fraction of items present), so a [direction, money]
// answer is half monetary and an [email, money, lesson] summary is one third
// monetary. Embedding money inside a composite answer therefore cannot evade
// the budget. The classifier fails closed: an answer kind or list-item kind it
// cannot classify is an error, never a silent "other" bucket.
package mixaudit

import (
	"fmt"
	"sort"
	"strings"
	"unicode"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Domain is the top-level semantic domain a claim belongs to.
const (
	DomainPersonal       = "personal"
	DomainBusiness       = "business"
	DomainConversational = "conversational"
	DomainIntegrity      = "integrity"
)

// Operation values name the head reasoning operation a claim requires.
const (
	OpLatestByTime      = "latest-by-time"
	OpPriorState        = "prior-state"
	OpOwnerOf           = "owner-of"
	OpBalanceArithmetic = "balance-arithmetic"
	OpQuantityExtract   = "quantity-extract"
	OpQuantitySum       = "quantity-sum"
	OpQuantityMax       = "quantity-max"
	OpQuantitySelect    = "quantity-select"
	OpDirectionOf       = "direction-of"
	OpVerbatimSelect    = "verbatim-select"
	OpNegationSelect    = "negation-select"
	OpCounterfactual    = "counterfactual-select"
	OpReportedSpeech    = "reported-speech-select"
	OpAttributedSelect  = "attributed-select"
	OpPreferenceApply   = "preference-apply"
	OpAcknowledge       = "acknowledge"
	OpChitchat          = "chitchat"
	OpAbstain           = "abstain"
	OpClarify           = "clarify"
)

// Gate exposure kinds: a case whose score can be moved by something other than
// its own answer correctness.
const (
	GateTwin       = "twin"       // metamorphic twin group (consistency factor)
	GatePair       = "pair"       // counterfactual base/variant pair
	GateProvenance = "provenance" // open program with validator-side provenance (model-dependence gates)
	GateCatalog    = "catalog"    // v13 catalog-present gate (reserved; no v12 family)
)

// Claim is one score-bearing unit of a case: the whole answer for scalar kinds,
// one item for list kinds.
type Claim struct {
	Kind      string  `json:"kind"`
	Weight    float64 `json:"weight"`
	Money     bool    `json:"money"`
	Domain    string  `json:"domain"`
	SubDomain string  `json:"sub_domain"`
	Operation string  `json:"operation"`
	// Computed is true when the claim's expected value cannot be copied as a
	// contiguous token sequence out of the case's declared evidence
	// (gen.AnswerVerbatimInEvidence). Verbatim is its complement when evidence is
	// bound; both are false when the case declares no evidence.
	Computed bool `json:"computed"`
	Verbatim bool `json:"verbatim"`
}

// CaseClass is the classification of one memory case.
type CaseClass struct {
	CaseID       string  `json:"case_id"`
	Family       string  `json:"family"`
	AnswerKind   string  `json:"answer_kind"`
	Domain       string  `json:"domain"`
	SubDomain    string  `json:"sub_domain"`
	Operation    string  `json:"operation"`
	Claims       []Claim `json:"claims"`
	MoneyWeight  float64 `json:"money_weight"`
	MoneyBearing bool    `json:"money_bearing"`
	Arithmetic   bool    `json:"arithmetic"`
	// EvidenceBound is true when the artifact carries evidence pair ids for the
	// case (v10+), so computed/verbatim is decidable.
	EvidenceBound bool   `json:"evidence_bound"`
	Language      string `json:"language"`
	// TwinRelation is "" / "twin_group" / the V10 metamorphic relation /
	// "counterfactual_pair".
	TwinRelation string   `json:"twin_relation,omitempty"`
	Gates        []string `json:"gates,omitempty"`
	// CascadeDependent is true when the case's score can be moved by a sibling
	// case (twin agreement, base/variant pair).
	CascadeDependent bool   `json:"cascade_dependent"`
	OpenProgram      bool   `json:"open_program"`
	Abstention       bool   `json:"abstention"`
	UserID           string `json:"user_id,omitempty"`
}

// SeedReport is the histogram for one (seed, run size, version).
type SeedReport struct {
	Seed         int64  `json:"seed"`
	BenchVersion int    `json:"bench_version"`
	RunSize      string `json:"run_size"`
	MemoryCases  int    `json:"memory_cases"`
	ToolCases    int    `json:"tool_cases"`
	// Weight is the memory denominator (one per case).
	Weight float64 `json:"weight"`

	DirectMoneyCases       int     `json:"direct_money_cases"`
	MoneyBearingCases      int     `json:"money_bearing_cases"`
	MoneyWeight            float64 `json:"money_weight"`
	MoneyShare             float64 `json:"money_share"`
	MoneyOnlyCases         int     `json:"money_only_cases"`
	MonetaryOpenPrograms   int     `json:"monetary_open_programs"`
	OpenPrograms           int     `json:"open_programs"`
	ArithmeticCases        int     `json:"arithmetic_cases"`
	ArithmeticShare        float64 `json:"arithmetic_share"`
	ComputedClaims         float64 `json:"computed_claim_weight"`
	VerbatimClaims         float64 `json:"verbatim_claim_weight"`
	EvidenceBoundWeight    float64 `json:"evidence_bound_weight"`
	ComputedMoneyWeight    float64 `json:"computed_money_weight"`
	AbstentionCases        int     `json:"abstention_cases"`
	AbstentionShare        float64 `json:"abstention_share"`
	TwinCoveredWeight      float64 `json:"twin_covered_weight"`
	TwinCoverage           float64 `json:"twin_coverage"`
	GateExposedWeight      float64 `json:"gate_exposed_weight"`
	GateExposedShare       float64 `json:"gate_exposed_share"`
	CascadeDependentWeight float64 `json:"cascade_dependent_weight"`
	CascadeDependentShare  float64 `json:"cascade_dependent_share"`
	// MaxSingleErrorCascade is the largest memory-weight share one honest
	// error can move through sibling comparisons (the largest twin group or
	// pair, as a share of Weight).
	MaxSingleErrorCascade float64 `json:"max_single_error_cascade"`

	Families      map[string]int     `json:"families"`
	AnswerKinds   map[string]float64 `json:"answer_kind_weight"`
	Domains       map[string]float64 `json:"domain_weight"`
	SubDomains    map[string]float64 `json:"sub_domain_weight"`
	Operations    map[string]float64 `json:"operation_weight"`
	KindOperation map[string]float64 `json:"kind_operation_weight"`
	Languages     map[string]float64 `json:"language_weight"`
	Gates         map[string]float64 `json:"gate_weight"`
	Relations     map[string]int     `json:"twin_relations"`

	// GIHParseRate is the generator-inverse harness family-identification rate
	// for this seed when a cmd/parserprobe report was supplied; nil otherwise.
	GIHParseRate *float64 `json:"gih_parse_rate,omitempty"`

	Cases []CaseClass `json:"cases,omitempty"`
}

// Audit classifies every memory case of artifact. It returns an error on any
// unclassified answer kind, list-item kind, or family so a new family cannot
// silently land in an unaudited bucket.
func Audit(artifact gen.DatasetArtifact, runSize string, keepCases bool) (SeedReport, error) {
	pairs := map[string]string{}
	for _, tc := range artifact.ToolCases {
		for _, p := range tc.PrerequisitePairs {
			pairs[p.PairID] = p.Prompt + " " + p.Response
		}
	}
	for _, w := range artifact.MemoryWaves {
		for _, p := range w.Pairs {
			pairs[p.PairID] = p.Prompt + " " + p.Response
		}
	}
	report := SeedReport{
		Seed: artifact.Seed, BenchVersion: artifact.BenchVersion, RunSize: runSize,
		MemoryCases: len(artifact.MemoryCases), ToolCases: len(artifact.ToolCases),
		Families: map[string]int{}, AnswerKinds: map[string]float64{}, Domains: map[string]float64{},
		SubDomains: map[string]float64{}, Operations: map[string]float64{}, KindOperation: map[string]float64{},
		Languages: map[string]float64{}, Gates: map[string]float64{}, Relations: map[string]int{},
	}
	twinWeight := map[string]float64{}
	pairWeight := map[string]float64{}
	for _, ac := range artifact.MemoryCases {
		cc, err := Classify(ac, pairs)
		if err != nil {
			return SeedReport{}, fmt.Errorf("case %s (%s): %w", ac.ID, ac.QuestionType, err)
		}
		report.Weight++
		report.Families[cc.Family]++
		report.AnswerKinds[cc.AnswerKind]++
		report.Languages[cc.Language]++
		if cc.AnswerKind == protocol.AnswerMoney {
			report.DirectMoneyCases++
		}
		if cc.MoneyBearing {
			report.MoneyBearingCases++
		}
		if cc.MoneyWeight == 1 {
			report.MoneyOnlyCases++
		}
		report.MoneyWeight += cc.MoneyWeight
		if cc.OpenProgram {
			report.OpenPrograms++
			if cc.MoneyBearing {
				report.MonetaryOpenPrograms++
			}
		}
		if cc.Arithmetic {
			report.ArithmeticCases++
		}
		if cc.Abstention {
			report.AbstentionCases++
		}
		if cc.TwinRelation != "" {
			report.Relations[cc.TwinRelation]++
			report.TwinCoveredWeight++
		}
		if len(cc.Gates) > 0 {
			report.GateExposedWeight++
			for _, g := range cc.Gates {
				report.Gates[g]++
			}
		}
		if cc.CascadeDependent {
			report.CascadeDependentWeight++
			if ac.TwinGroup != "" {
				twinWeight[ac.TwinGroup]++
			} else {
				pairWeight[cc.Family]++
			}
		}
		if cc.EvidenceBound {
			report.EvidenceBoundWeight++
		}
		for _, cl := range cc.Claims {
			report.Domains[cl.Domain] += cl.Weight
			report.SubDomains[cl.Domain+"/"+cl.SubDomain] += cl.Weight
			report.Operations[cl.Operation] += cl.Weight
			report.KindOperation[cl.Kind+"×"+cl.Operation] += cl.Weight
			if cl.Computed {
				report.ComputedClaims += cl.Weight
				if cl.Money {
					report.ComputedMoneyWeight += cl.Weight
				}
			}
			if cl.Verbatim {
				report.VerbatimClaims += cl.Weight
			}
		}
		if keepCases {
			report.Cases = append(report.Cases, cc)
		}
	}
	if report.Weight > 0 {
		report.MoneyShare = report.MoneyWeight / report.Weight
		report.ArithmeticShare = float64(report.ArithmeticCases) / report.Weight
		report.AbstentionShare = float64(report.AbstentionCases) / report.Weight
		report.TwinCoverage = report.TwinCoveredWeight / report.Weight
		report.GateExposedShare = report.GateExposedWeight / report.Weight
		report.CascadeDependentShare = report.CascadeDependentWeight / report.Weight
		largest := 0.0
		for _, w := range twinWeight {
			if w > largest {
				largest = w
			}
		}
		// A counterfactual pair is two cases; one wrong member disagrees with one
		// sibling, so its cascade is the pair (2 cases).
		if len(pairWeight) > 0 && largest < 2 {
			largest = 2
		}
		report.MaxSingleErrorCascade = largest / report.Weight
	}
	return report, nil
}

// Classify assigns every axis of one artifact case. pairs maps pair id to the
// concatenated prompt+response text used for the computed/verbatim decision.
func Classify(ac gen.ArtifactCase, pairs map[string]string) (CaseClass, error) {
	kind := ac.AnswerKind
	if kind == "" {
		kind = protocol.AnswerValue
	}
	fam, ok := familyProfile(ac.QuestionType)
	if !ok {
		return CaseClass{}, fmt.Errorf("unclassified memory family %q", ac.QuestionType)
	}
	cc := CaseClass{
		CaseID: ac.ID, Family: ac.QuestionType, AnswerKind: kind,
		Domain: fam.domain, SubDomain: fam.subDomain, Operation: fam.operation,
		Language:    detectLanguage(ac.Question),
		OpenProgram: fam.openProgram, Abstention: fam.abstention || kind == protocol.AnswerDecline,
		UserID: ac.UserID,
	}
	var evidence strings.Builder
	if len(ac.V10EvidencePairIDs) > 0 {
		cc.EvidenceBound = true
		for _, id := range ac.V10EvidencePairIDs {
			text, ok := pairs[id]
			if !ok {
				return CaseClass{}, fmt.Errorf("evidence pair %s missing from artifact", id)
			}
			evidence.WriteByte(' ')
			evidence.WriteString(text)
		}
	}
	claimKinds := []string{kind}
	claimValues := []string{ac.ExpectedAnswer}
	switch kind {
	case protocol.AnswerList, protocol.AnswerOrderedList:
		if len(ac.AnswerItems) == 0 {
			return CaseClass{}, fmt.Errorf("list answer without items")
		}
		claimKinds = make([]string, len(ac.AnswerItems))
		claimValues = append([]string(nil), ac.AnswerItems...)
		for i := range ac.AnswerItems {
			k := protocol.AnswerValue
			if i < len(ac.AnswerItemKinds) && ac.AnswerItemKinds[i] != "" {
				k = ac.AnswerItemKinds[i]
			}
			claimKinds[i] = k
		}
	}
	weight := 1.0 / float64(len(claimKinds))
	for i, ck := range claimKinds {
		if !knownClaimKind(ck) {
			return CaseClass{}, fmt.Errorf("unclassified claim kind %q", ck)
		}
		cl := Claim{
			Kind: ck, Weight: weight, Money: ck == protocol.AnswerMoney,
			Domain: fam.domain, SubDomain: fam.subDomain, Operation: fam.operation,
		}
		// Composite (list) items carry their own domain/operation: the story
		// summary's email is routing, its balance is finance, its lesson is a
		// personal reflection.
		if len(claimKinds) > 1 {
			if len(fam.itemDomains) == len(claimKinds) {
				cl.Domain, cl.SubDomain, cl.Operation = fam.itemDomains[i].domain, fam.itemDomains[i].subDomain, fam.itemDomains[i].operation
			} else {
				return CaseClass{}, fmt.Errorf("family %q has %d list items but %d item profiles", ac.QuestionType, len(claimKinds), len(fam.itemDomains))
			}
		}
		if cc.EvidenceBound && claimValues[i] != "" {
			cl.Verbatim = gen.AnswerVerbatimInEvidence(evidence.String(), claimValues[i])
			cl.Computed = !cl.Verbatim
		}
		if cl.Money {
			cc.MoneyWeight += weight
			cc.MoneyBearing = true
		}
		cc.Claims = append(cc.Claims, cl)
	}
	cc.Arithmetic = fam.arithmetic
	switch {
	case ac.V10Provenance != nil && ac.V10Provenance.Relation != "":
		cc.TwinRelation = ac.V10Provenance.Relation
	case ac.TwinGroup != "":
		cc.TwinRelation = "twin_group"
	case fam.pair:
		cc.TwinRelation = "counterfactual_pair"
	}
	if ac.TwinGroup != "" {
		cc.Gates = append(cc.Gates, GateTwin)
		cc.CascadeDependent = true
	}
	if fam.pair {
		cc.Gates = append(cc.Gates, GatePair)
		cc.CascadeDependent = true
	}
	if ac.V10Provenance != nil {
		cc.Gates = append(cc.Gates, GateProvenance)
	}
	if fam.catalog {
		cc.Gates = append(cc.Gates, GateCatalog)
	}
	return cc, nil
}

// knownClaimKind admits exactly the answer kinds protocol defines. New kinds
// (issue #1824's v13 grader kinds) are added here by importing their protocol
// constants when they land — never retyped by name — so a generator emitting a
// kind the grader does not define fails the audit closed.
func knownClaimKind(kind string) bool {
	switch kind {
	case protocol.AnswerValue, protocol.AnswerNumber, protocol.AnswerMoney, protocol.AnswerDirection,
		protocol.AnswerList, protocol.AnswerOrderedList, protocol.AnswerDuration, protocol.AnswerReversal,
		protocol.AnswerPersistence, protocol.AnswerDecline, protocol.AnswerAcknowledge, protocol.AnswerChitchat:
		return true
	}
	return false
}

func detectLanguage(text string) string {
	for _, r := range text {
		if r > unicode.MaxASCII && unicode.IsLetter(r) {
			return "non-en"
		}
	}
	return "en"
}

type itemProfile struct {
	domain, subDomain, operation string
}

type profile struct {
	domain, subDomain, operation string
	arithmetic                   bool
	openProgram                  bool
	pair                         bool
	catalog                      bool
	abstention                   bool
	itemDomains                  []itemProfile
}

// familyProfile maps a memory QuestionType to its semantic profile. Every v8+
// family emitted by the generator must appear here (or match a prefix rule);
// Audit fails otherwise.
func familyProfile(family string) (profile, bool) {
	finance := itemProfile{DomainBusiness, "project-finance", OpBalanceArithmetic}
	routing := itemProfile{DomainBusiness, "review-routing", OpLatestByTime}
	lesson := itemProfile{DomainPersonal, "reflection", OpVerbatimSelect}
	direction := itemProfile{DomainBusiness, "project-finance", OpDirectionOf}
	switch family {
	// Coherent-world people, projects, trips.
	case "world-contact-current":
		return profile{domain: DomainPersonal, subDomain: "contacts", operation: OpLatestByTime}, true
	case "world-contact-previous":
		return profile{domain: DomainPersonal, subDomain: "contacts", operation: OpPriorState}, true
	case "world-isolation-contact-current":
		return profile{domain: DomainPersonal, subDomain: "contacts-cross-graph", operation: OpLatestByTime}, true
	case "world-project-outstanding":
		return profile{domain: DomainBusiness, subDomain: "accounts-payable", operation: OpBalanceArithmetic, arithmetic: true}, true
	case "world-project-lead-current":
		return profile{domain: DomainBusiness, subDomain: "project-ownership", operation: OpOwnerOf}, true
	case "world-project-lead-previous":
		return profile{domain: DomainBusiness, subDomain: "project-ownership", operation: OpPriorState}, true
	case "world-trip-current":
		return profile{domain: DomainPersonal, subDomain: "travel", operation: OpQuantitySum, arithmetic: true}, true
	case "world-trip-longest-current":
		return profile{domain: DomainPersonal, subDomain: "travel", operation: OpQuantityMax, arithmetic: true}, true
	case "world-trip-changed-leg-previous":
		return profile{domain: DomainPersonal, subDomain: "travel", operation: OpPriorState}, true
	case "world-trip-changed-leg-current":
		return profile{domain: DomainPersonal, subDomain: "travel", operation: OpQuantitySelect}, true
	// Story arcs.
	case "world-story-balance-current", "world-story-post-approval-balance":
		return profile{domain: DomainBusiness, subDomain: "project-finance", operation: OpBalanceArithmetic, arithmetic: true}, true
	case "world-story-budget-delta":
		return profile{domain: DomainBusiness, subDomain: "project-finance", operation: OpQuantityExtract}, true
	case "world-story-later-net-change":
		return profile{domain: DomainBusiness, subDomain: "project-finance", operation: OpBalanceArithmetic, arithmetic: true,
			itemDomains: []itemProfile{direction, finance}}, true
	case "world-story-contact-current":
		return profile{domain: DomainBusiness, subDomain: "review-routing", operation: OpLatestByTime}, true
	case "world-story-lesson":
		return profile{domain: DomainPersonal, subDomain: "reflection", operation: OpVerbatimSelect}, true
	case "world-story-outcome-summary":
		return profile{domain: DomainBusiness, subDomain: "project-finance", operation: OpBalanceArithmetic, arithmetic: true,
			itemDomains: []itemProfile{routing, finance, lesson}}, true
	// Anti-family-compiler record families.
	case "record-balance-plain", "record-balance-adjusted", "record-balance-superseded", "record-balance-capped",
		"record-balance-forgiven", "record-balance-referred":
		return profile{domain: DomainBusiness, subDomain: "accounts", operation: OpBalanceArithmetic, arithmetic: true}, true
	case "record-balance-cf-base", "record-balance-cf-variant":
		return profile{domain: DomainBusiness, subDomain: "accounts", operation: OpBalanceArithmetic, arithmetic: true, pair: true}, true
	// Parser-divergence canaries.
	case "parser-divergence-negation":
		return profile{domain: DomainPersonal, subDomain: "health", operation: OpNegationSelect}, true
	case "parser-divergence-retraction":
		return profile{domain: DomainPersonal, subDomain: "personal-finance", operation: OpLatestByTime}, true
	case "parser-divergence-hypothetical":
		return profile{domain: DomainBusiness, subDomain: "invoices", operation: OpCounterfactual}, true
	case "parser-divergence-reported-speech":
		return profile{domain: DomainPersonal, subDomain: "household", operation: OpReportedSpeech}, true
	// World-native integrity tail.
	case "conversational-chitchat":
		return profile{domain: DomainConversational, subDomain: "chitchat", operation: OpChitchat}, true
	case "conversational-declarative":
		return profile{domain: DomainConversational, subDomain: "preferences", operation: OpAcknowledge}, true
	case "declarative-behavior":
		return profile{domain: DomainConversational, subDomain: "preferences", operation: OpPreferenceApply}, true
	case "world-canary":
		return profile{domain: DomainIntegrity, subDomain: "registration", operation: OpAttributedSelect}, true
	case "world-injection-resistance":
		return profile{domain: DomainBusiness, subDomain: "accounts-payable", operation: OpBalanceArithmetic, arithmetic: true}, true
	}
	// Version-carrying and v13 families land under stable affixes; classify them
	// by affix so the audit keeps running while the envelope PR pins their
	// exact profiles.
	switch {
	// Open query programs: every contract from v10 up re-renders the family
	// under "v<N>-open-program".
	case strings.HasSuffix(family, "-open-program"):
		return profile{domain: DomainBusiness, subDomain: "ledger-programs", operation: OpBalanceArithmetic, arithmetic: true, openProgram: true}, true
	case strings.HasPrefix(family, "abstention-") || strings.HasSuffix(family, "-abstention"):
		return profile{domain: DomainPersonal, subDomain: "abstention", operation: OpAbstain, abstention: true}, true
	case strings.HasPrefix(family, "clarify-"):
		return profile{domain: DomainPersonal, subDomain: "clarification", operation: OpClarify}, true
	}
	return profile{}, false
}

// Envelope holds the v13 caps and floors the 40-seed CI gate asserts. They are
// defined here (issue #1830) and asserted green only once the v13 envelope PR
// rebalances the mix; on v12 they document the gap (see docs/v13-family-mix-study.md).
type Envelope struct {
	MemoryCasesTarget      int     // exact memory suite size per seed
	MoneyWeightHardCap     float64 // share of memory weight
	MoneyWeightTarget      float64
	MoneyBearingCasesCap   int
	MonetaryOpenPrograms   int // exact
	ArithmeticShareCap     float64
	PersonalWeightFloor    float64
	BusinessWeightFloor    float64
	SubDomainWeightCap     float64
	KindOperationWeightCap float64
	AbstentionShareTarget  float64
	AbstentionShareBand    float64
	TwinCoverageFloor      float64
	GateExposedShareCap    float64
	SingleErrorCascadeCap  float64
}

// V13Envelope is the plan's v13.0 memory-mix envelope.
var V13Envelope = Envelope{
	MemoryCasesTarget:      250,
	MoneyWeightHardCap:     0.15,
	MoneyWeightTarget:      0.12,
	MoneyBearingCasesCap:   22,
	MonetaryOpenPrograms:   0,
	ArithmeticShareCap:     0.20,
	PersonalWeightFloor:    0.30,
	BusinessWeightFloor:    0.40,
	SubDomainWeightCap:     0.20,
	KindOperationWeightCap: 0.15,
	AbstentionShareTarget:  0.10,
	AbstentionShareBand:    0.01,
	TwinCoverageFloor:      0.40,
	GateExposedShareCap:    0.40,
	SingleErrorCascadeCap:  0.04,
}

// Violation is one envelope rule a seed report breaks.
type Violation struct {
	Rule     string  `json:"rule"`
	Observed float64 `json:"observed"`
	Limit    float64 `json:"limit"`
}

func (v Violation) String() string {
	return fmt.Sprintf("%s: observed %.4f, limit %.4f", v.Rule, v.Observed, v.Limit)
}

// Check evaluates report against env and returns every violated rule.
func (env Envelope) Check(report SeedReport) []Violation {
	var out []Violation
	add := func(rule string, observed, limit float64, bad bool) {
		if bad {
			out = append(out, Violation{Rule: rule, Observed: observed, Limit: limit})
		}
	}
	w := report.Weight
	if w == 0 {
		return []Violation{{Rule: "empty memory suite"}}
	}
	add("memory cases == target", float64(report.MemoryCases), float64(env.MemoryCasesTarget), report.MemoryCases != env.MemoryCasesTarget)
	add("money weight share <= hard cap", report.MoneyShare, env.MoneyWeightHardCap, report.MoneyShare > env.MoneyWeightHardCap)
	add("money-bearing cases <= cap", float64(report.MoneyBearingCases), float64(env.MoneyBearingCasesCap), report.MoneyBearingCases > env.MoneyBearingCasesCap)
	add("monetary open programs == 0", float64(report.MonetaryOpenPrograms), float64(env.MonetaryOpenPrograms), report.MonetaryOpenPrograms != env.MonetaryOpenPrograms)
	add("arithmetic share <= cap", report.ArithmeticShare, env.ArithmeticShareCap, report.ArithmeticShare > env.ArithmeticShareCap)
	personal := report.Domains[DomainPersonal] / w
	business := report.Domains[DomainBusiness] / w
	add("personal weight >= floor", personal, env.PersonalWeightFloor, personal < env.PersonalWeightFloor)
	add("business weight >= floor", business, env.BusinessWeightFloor, business < env.BusinessWeightFloor)
	for _, key := range sortedKeys(report.SubDomains) {
		share := report.SubDomains[key] / w
		add("sub-domain "+key+" <= cap", share, env.SubDomainWeightCap, share > env.SubDomainWeightCap)
	}
	for _, key := range sortedKeys(report.KindOperation) {
		share := report.KindOperation[key] / w
		add("kind×operation "+key+" <= cap", share, env.KindOperationWeightCap, share > env.KindOperationWeightCap)
	}
	lo, hi := env.AbstentionShareTarget-env.AbstentionShareBand, env.AbstentionShareTarget+env.AbstentionShareBand
	add("abstention share within band", report.AbstentionShare, env.AbstentionShareTarget, report.AbstentionShare < lo || report.AbstentionShare > hi)
	add("twin coverage >= floor", report.TwinCoverage, env.TwinCoverageFloor, report.TwinCoverage < env.TwinCoverageFloor)
	add("gate-exposed share <= cap", report.GateExposedShare, env.GateExposedShareCap, report.GateExposedShare > env.GateExposedShareCap)
	add("single-error cascade <= cap", report.MaxSingleErrorCascade, env.SingleErrorCascadeCap, report.MaxSingleErrorCascade > env.SingleErrorCascadeCap)
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
