// Package familypanel judges bench v14 candidate families (SN-351) from
// executed agent runs whose datasets do not share cases.
//
// familygate judges a family from per-case panel verdicts: the same case,
// answered by every panel agent, is trivial, discriminating or unsolved. A
// generator round produces that shape. An isolated benchmark canary does not:
// its seed derives from (agent, validator), so no two agents ever answer the
// same case. What a set of canaries does measure is, per family, each owner's
// solve rate p_i on that family's cases.
//
// Those marginals bound the panel verdicts without knowing how case
// difficulty correlates across agents:
//
//   - the trivial rate (every panel owner solves the case) is at most
//     min_i p_i, reached when difficulty is perfectly shared;
//   - the unsolved rate (fewer than MinSolverOwners owners solve the case) is
//     at most UnsolvedMax(p), the largest P(N < k) over every coupling with
//     these marginals.
//
// Judge feeds those worst cases into familygate's own thresholds, so a family
// that admits here admits under every coupling. The independent-coupling
// estimates are reported beside them as the central value.
//
// Labels come from the generator itself: business program cases take the
// family V13FamilyOfVersion assigns their group, personal program cases the
// personal-life domain in their provenance. Labels regenerates the dataset, so
// a run whose dataset digest differs from the regenerated one is refused.
package familypanel

import (
	"fmt"
	"math"
	"sort"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/internal/familygate"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

// personalDomainSemantic is the ontology term that names a personal program
// case's domain.
const personalDomainSemantic = "personal-life domain"

// Labels is one regenerated dataset's family label per program case.
type Labels struct {
	Seed          int64             `json:"seed"`
	DatasetSHA256 string            `json:"dataset_sha256"`
	Family        map[string]string `json:"family"`
}

// Families returns the distinct families present, sorted.
func (l Labels) Families() []string {
	seen := map[string]bool{}
	for _, f := range l.Family {
		seen[f] = true
	}
	out := make([]string, 0, len(seen))
	for f := range seen {
		out = append(out, f)
	}
	sort.Strings(out)
	return out
}

// LabelDataset regenerates the dataset for seed and labels every business and
// personal program case. It fails if any program case in the artifact goes
// unlabelled, or a generated program case is missing from the artifact.
func LabelDataset(seed int64, runSize string, benchVersion int) (Labels, error) {
	prof, ok := gen.ProfileForVersion(runSize, benchVersion)
	if !ok {
		return Labels{}, fmt.Errorf("run size %q has no profile at bench v%d", runSize, benchVersion)
	}
	art, err := gen.GenerateDatasetWithSurface(seed, prof, benchVersion, gen.SurfaceOptions{})
	if err != nil {
		return Labels{}, err
	}
	sha, _, err := art.SHA256Hex()
	if err != nil {
		return Labels{}, err
	}
	env, ok := gen.V13EnvelopeForRunSize(runSize)
	if !ok {
		return Labels{}, fmt.Errorf("run size %q has no v13 envelope", runSize)
	}
	labels := Labels{Seed: seed, DatasetSHA256: sha, Family: map[string]string{}}
	// The envelope spends its first business slots on the enterprise slice;
	// the rest are the open programs whose group index carries the family.
	legacy := env.BusinessPrograms - min(12, env.BusinessPrograms)
	business := map[string]string{}
	if legacy > 0 {
		cases, err := universe.GenerateV13ProgramsForVersion(seed, legacy, benchVersion)
		if err != nil {
			return Labels{}, err
		}
		for i, c := range cases {
			business[c.Plan.Case.ID] = string(universe.V13FamilyOfVersion(seed, i, benchVersion))
		}
	}
	for _, c := range art.MemoryCases {
		switch c.QuestionType {
		case universe.V13ProgramQuestionType:
			family, ok := business[c.ID]
			if !ok {
				return Labels{}, fmt.Errorf("seed %d: business program case %s was not regenerated", seed, c.ID)
			}
			labels.Family[c.ID] = family
			delete(business, c.ID)
		case universe.V13PersonalQuestionType:
			domain := ""
			if c.V10Provenance != nil {
				for _, term := range c.V10Provenance.Ontology {
					if term.Semantic == personalDomainSemantic {
						domain = term.Wire
					}
				}
			}
			if domain == "" {
				return Labels{}, fmt.Errorf("seed %d: personal program case %s has no domain", seed, c.ID)
			}
			labels.Family[c.ID] = domain
		}
	}
	if len(business) > 0 {
		return Labels{}, fmt.Errorf("seed %d: %d generated business program cases are not in the dataset", seed, len(business))
	}
	return labels, nil
}

// Run is one executed, isolated run of one agent on one dataset.
type Run struct {
	// Run names the evidence (a canary id). Carried, never judged.
	Run string `json:"run"`
	// Owner is the agent's owner key; panel membership is per owner.
	Owner         string               `json:"owner"`
	Seed          int64                `json:"seed,string"`
	DatasetSHA256 string               `json:"dataset_sha256"`
	PerCase       []protocol.CaseScore `json:"per_case"`
}

// OwnerStat is one owner's record on one family.
type OwnerStat struct {
	Owner       string  `json:"owner"`
	Cases       int     `json:"cases"`
	Solved      int     `json:"solved"`
	AtThreshold int     `json:"at_threshold"`
	Rate        float64 `json:"solve_rate"`
}

// FamilyResult is one family's measurement and verdict.
type FamilyResult struct {
	Family string `json:"family"`
	// Status is the V14FamilyMix ledger status, or "unlisted".
	Status          string      `json:"status"`
	Seeds           int         `json:"seeds"`
	Cases           int         `json:"cases"`
	Owners          []OwnerStat `json:"owners"`
	TrivialMax      float64     `json:"trivial_max"`
	UnsolvedMax     float64     `json:"unsolved_max"`
	TrivialIndep    float64     `json:"trivial_independent"`
	UnsolvedIndep   float64     `json:"unsolved_independent"`
	AtThresholdRate float64     `json:"at_threshold_rate"`
	MinOwnerCases   int         `json:"min_owner_cases"`
	Verdict         string      `json:"verdict"`
	Reasons         []string    `json:"reasons,omitempty"`
}

// Options bound the evidence a verdict needs beyond familygate's thresholds.
type Options struct {
	// MinPanelOwners is the fewest distinct owners with cases of the family.
	MinPanelOwners int
	// MinOwnerCases is the fewest cases every panel owner must have answered.
	MinOwnerCases int
}

// DefaultOptions require a five-owner panel, as the generator round does, and
// at least two metamorphic groups (eight cases) per owner.
func DefaultOptions() Options { return Options{MinPanelOwners: 5, MinOwnerCases: 8} }

// HoldThinPanel is the verdict for a family the runs cover too thinly.
const HoldThinPanel = "hold-thin-panel"

// Evaluate labels every run's cases and judges each family it finds.
// labelsFor returns the labels of one seed (LabelDataset, or a cache of it).
func Evaluate(runs []Run, labelsFor func(seed int64) (Labels, error), p familygate.Params, o Options) ([]FamilyResult, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	type acc struct {
		seeds  map[int64]bool
		owners map[string]*OwnerStat
	}
	by := map[string]*acc{}
	for _, r := range runs {
		if r.Owner == "" {
			return nil, fmt.Errorf("run %s has no owner", r.Run)
		}
		labels, err := labelsFor(r.Seed)
		if err != nil {
			return nil, fmt.Errorf("run %s: %w", r.Run, err)
		}
		if r.DatasetSHA256 != labels.DatasetSHA256 {
			return nil, fmt.Errorf("run %s: dataset %s does not match regenerated seed %d (%s)", r.Run, r.DatasetSHA256, r.Seed, labels.DatasetSHA256)
		}
		seen := map[string]bool{}
		for _, cs := range r.PerCase {
			family, ok := labels.Family[cs.CaseID]
			if !ok {
				continue
			}
			if seen[cs.CaseID] {
				return nil, fmt.Errorf("run %s: case %s scored twice", r.Run, cs.CaseID)
			}
			seen[cs.CaseID] = true
			a := by[family]
			if a == nil {
				a = &acc{seeds: map[int64]bool{}, owners: map[string]*OwnerStat{}}
				by[family] = a
			}
			a.seeds[r.Seed] = true
			s := a.owners[r.Owner]
			if s == nil {
				s = &OwnerStat{Owner: r.Owner}
				a.owners[r.Owner] = s
			}
			s.Cases++
			if cs.Score >= p.SolveThreshold {
				s.Solved++
				if math.Abs(cs.Score-p.SolveThreshold) < 1e-9 {
					s.AtThreshold++
				}
			}
		}
		if len(seen) != len(labels.Family) {
			return nil, fmt.Errorf("run %s: scored %d of %d program cases", r.Run, len(seen), len(labels.Family))
		}
	}
	out := make([]FamilyResult, 0, len(by))
	for family, a := range by {
		res := FamilyResult{Family: family, Seeds: len(a.seeds), Status: "unlisted"}
		if e, ok := universe.V14FamilyMixFor(family); ok {
			res.Status = string(e.Status)
		}
		rates := make([]float64, 0, len(a.owners))
		solved, atThreshold := 0, 0
		res.MinOwnerCases = math.MaxInt
		for _, s := range a.owners {
			s.Rate = float64(s.Solved) / float64(s.Cases)
			res.Owners = append(res.Owners, *s)
			rates = append(rates, s.Rate)
			res.Cases += s.Cases
			solved += s.Solved
			atThreshold += s.AtThreshold
			res.MinOwnerCases = min(res.MinOwnerCases, s.Cases)
		}
		sort.Slice(res.Owners, func(i, j int) bool { return res.Owners[i].Owner < res.Owners[j].Owner })
		res.TrivialMax = TrivialMax(rates)
		res.UnsolvedMax = UnsolvedMax(rates, p.MinSolverOwners)
		res.TrivialIndep, res.UnsolvedIndep = Independent(rates, p.MinSolverOwners)
		if solved > 0 {
			res.AtThresholdRate = float64(atThreshold) / float64(solved)
		}
		res.Verdict, res.Reasons = judge(res, len(rates), p, o)
		out = append(out, res)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Family < out[j].Family })
	return out, nil
}

func judge(r FamilyResult, owners int, p familygate.Params, o Options) (string, []string) {
	row := familygate.FamilyRow{
		Family:          r.Family,
		Rounds:          r.Seeds,
		Cases:           r.Cases,
		Judged:          r.Cases,
		TrivialRate:     r.TrivialMax,
		UnsolvedRate:    r.UnsolvedMax,
		AtThresholdRate: r.AtThresholdRate,
	}
	verdict, reasons := familygate.Judge(row, p)
	if owners < o.MinPanelOwners {
		reasons = append(reasons, fmt.Sprintf("%d panel owner(s) < %d", owners, o.MinPanelOwners))
		if verdict == familygate.Admit {
			verdict = HoldThinPanel
		}
	}
	if r.MinOwnerCases < o.MinOwnerCases {
		reasons = append(reasons, fmt.Sprintf("an owner answered %d case(s) < %d", r.MinOwnerCases, o.MinOwnerCases))
		if verdict == familygate.Admit {
			verdict = HoldThinPanel
		}
	}
	return verdict, reasons
}

// TrivialMax is the largest share of cases every owner solves, over every
// coupling with solve rates rates: min_i p_i.
func TrivialMax(rates []float64) float64 {
	if len(rates) == 0 {
		return 0
	}
	m := 1.0
	for _, r := range rates {
		m = math.Min(m, r)
	}
	return m
}

// UnsolvedMax bounds from above the share of cases fewer than k owners solve,
// over every coupling with solve rates rates.
//
// Split the case space into U (fewer than k solvers, mass q) and S (mass
// s = 1-q). On S each owner's solve mass is at most s, so at least
// p_i - min(p_i, s) of it falls in U, and U holds at most (k-1)*q solve mass.
// Any coupling with unsolved share q therefore satisfies
// sum(p_i - min(p_i, s)) <= (k-1)*(1-s). That condition only gets easier as s
// grows, so 1 - (smallest s meeting it) bounds q for every coupling. The bound
// is tight whenever the remaining marginals cover S at least k deep.
func UnsolvedMax(rates []float64, k int) float64 {
	if len(rates) < k {
		return 1
	}
	fits := func(s float64) bool {
		inU := 0.0
		for _, p := range rates {
			inU += p - math.Min(p, s)
		}
		return inU <= float64(k-1)*(1-s)+1e-12
	}
	if fits(0) {
		return 1
	}
	lo, hi := 0.0, 1.0
	for i := 0; i < 60; i++ {
		mid := (lo + hi) / 2
		if fits(mid) {
			hi = mid
		} else {
			lo = mid
		}
	}
	return 1 - hi
}

// Independent returns the trivial and unsolved shares when owners solve
// independently: prod p_i and P(N < k) for a Poisson-binomial N.
func Independent(rates []float64, k int) (trivial, unsolved float64) {
	dist := []float64{1}
	trivial = 1
	for _, p := range rates {
		trivial *= p
		next := make([]float64, len(dist)+1)
		for n, mass := range dist {
			next[n] += mass * (1 - p)
			next[n+1] += mass * p
		}
		dist = next
	}
	for n := 0; n < k && n < len(dist); n++ {
		unsolved += dist[n]
	}
	if len(rates) == 0 {
		trivial = 0
	}
	return trivial, unsolved
}
