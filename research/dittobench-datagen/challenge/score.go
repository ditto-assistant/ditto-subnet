package challenge

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

// Case statuses after a panel run.
const (
	CaseDiscriminating    = "discriminating"
	CaseTrivial           = "trivial"
	CaseUnsolved          = "unsolved"
	CaseDuplicate         = "duplicate"
	CaseInsufficientPanel = "insufficient_panel"
)

// Pack and run statuses after scoring.
const (
	PackScored            = "scored"
	PackInsufficientPanel = "insufficient_panel"
)

// ScoreParams are the operator-tunable scoring knobs. Platform stores them in
// a revisioned settings row and passes the same values to every scorer, so a
// pack's score is reproducible from (pack, panel results, params).
type ScoreParams struct {
	// MinPanel is the fewest eligible panel agents that must have a clean
	// result for a case before it can be judged at all.
	MinPanel int `json:"min_panel"`
	// MinSolverOwners is how many distinct owner families must solve a case
	// for it to count as solvable. Two by default: one colluding agent that
	// baked a pack's answers into its image cannot make a case "solvable".
	MinSolverOwners int `json:"min_solver_owners"`
	// SolveThreshold is the per-case score that counts as solved.
	SolveThreshold float64 `json:"solve_threshold"`
	// ChampionBonus multiplies credit by (1 + bonus) when the champion fails a
	// solvable case: finding the king's blind spot is worth the most.
	ChampionBonus float64 `json:"champion_bonus"`
	// TargetCases is the pack size at which full credit becomes reachable; a
	// smaller pack is normalized as if it had this many cases.
	TargetCases int `json:"target_cases"`
}

// DefaultScoreParams are the shipped v1 values.
func DefaultScoreParams() ScoreParams {
	return ScoreParams{
		MinPanel:        3,
		MinSolverOwners: 2,
		SolveThreshold:  0.5,
		ChampionBonus:   0.5,
		TargetCases:     32,
	}
}

// Validate rejects parameters that would make scoring meaningless.
func (p ScoreParams) Validate() error {
	switch {
	case p.MinPanel < 2 || p.MinPanel > 32:
		return errors.New("min_panel must be in 2..32")
	case p.MinSolverOwners < 1 || p.MinSolverOwners > p.MinPanel:
		return errors.New("min_solver_owners must be in 1..min_panel")
	case !(p.SolveThreshold > 0 && p.SolveThreshold <= 1):
		return errors.New("solve_threshold must be in (0, 1]")
	case !(p.ChampionBonus >= 0 && p.ChampionBonus <= 2):
		return errors.New("champion_bonus must be in [0, 2]")
	case p.TargetCases < MinCases || p.TargetCases > MaxCases:
		return fmt.Errorf("target_cases must be in %d..%d", MinCases, MaxCases)
	}
	return nil
}

// PanelCaseResult is one agent's outcome on one pack case. Fault marks an
// infrastructure or harness-transport failure the scorer attributes to itself;
// a faulted result is excluded rather than counted as a miss, so a scorer blip
// can neither manufacture "failed by the champion" nor sink a pack.
type PanelCaseResult struct {
	CaseID string  `json:"case_id"`
	Score  float64 `json:"score"`
	Fault  bool    `json:"fault,omitempty"`
}

// PanelMember is one agent the pack ran against.
type PanelMember struct {
	AgentID string `json:"agent_id"`
	// OwnerKey groups agents under common control (the miner coldkey). Solvers
	// are counted per owner, and the generator's own owner never counts.
	OwnerKey string            `json:"owner_key"`
	Champion bool              `json:"champion,omitempty"`
	Results  []PanelCaseResult `json:"results"`
}

// ScoreInput is everything Score needs besides the pack.
type ScoreInput struct {
	// GeneratorOwnerKey excludes the generator's own agents from the panel.
	GeneratorOwnerKey string        `json:"generator_owner_key"`
	Panel             []PanelMember `json:"panel"`
	// ClaimedFingerprints are case fingerprints an earlier pack already holds
	// credit for. Copies score zero for the later pack.
	ClaimedFingerprints []string `json:"claimed_fingerprints,omitempty"`
}

// CaseVerdict is the judged outcome of one pack case.
type CaseVerdict struct {
	CaseID         string  `json:"case_id"`
	Fingerprint    string  `json:"fingerprint"`
	Status         string  `json:"status"`
	PanelSize      int     `json:"panel_size"`
	Solvers        int     `json:"solvers"`
	SolverOwners   int     `json:"solver_owners"`
	MeanScore      float64 `json:"mean_score"`
	ChampionFailed bool    `json:"champion_failed"`
	Credit         float64 `json:"credit"`
}

// PackScore is a pack's discrimination score against one panel.
type PackScore struct {
	Status         string        `json:"status"`
	Score          float64       `json:"score"`
	RawCredit      float64       `json:"raw_credit"`
	Discriminating int           `json:"discriminating"`
	Trivial        int           `json:"trivial"`
	Unsolved       int           `json:"unsolved"`
	Duplicate      int           `json:"duplicate"`
	UnsolvedRate   float64       `json:"unsolved_rate"`
	Cases          []CaseVerdict `json:"cases"`
}

// CaseRef is the identity Score needs for one case. Platform scores from
// stored refs (the fingerprints the validation report returned) without
// re-decoding the pack.
type CaseRef struct {
	CaseID      string `json:"case_id"`
	Fingerprint string `json:"fingerprint"`
}

// Refs returns the pack's case refs in pack order.
func (p Pack) Refs() []CaseRef {
	out := make([]CaseRef, len(p.Cases))
	for i, c := range p.Cases {
		out[i] = CaseRef{CaseID: c.ID, Fingerprint: Fingerprint(c)}
	}
	return out
}

// Score judges a validated pack against panel results; see ScoreCases.
func Score(p Pack, in ScoreInput, params ScoreParams) (PackScore, error) {
	return ScoreCases(p.Refs(), in, params)
}

// ScoreCases judges a validated pack's cases against panel results.
//
// For each case, over eligible panel agents (not owned by the generator, with
// a non-faulted result):
//
//	solvable       ⇔ solved (score ≥ threshold) by ≥ MinSolverOwners owners
//	trivial        ⇔ solvable and solved by every eligible agent
//	discriminating ⇔ solvable and not trivial
//	credit         = (1 − mean score) × (1 + ChampionBonus·[champion failed])
//
// and for the pack, with n cases and u unsolved among the non-duplicate ones:
//
//	score = Σ credit / ((1 + ChampionBonus) · max(n, TargetCases)) × (1 − u/(n − dup))²
//
// The squared solvability factor is the counterweight to the incentive to
// "defeat" agents with broken cases: a pack where half the cases nobody can
// solve keeps at most a quarter of its credit.
//
// Panel members are processed in AgentID order so the floating-point sums are
// identical in every implementation (Platform's Python port included).
func ScoreCases(cases []CaseRef, in ScoreInput, params ScoreParams) (PackScore, error) {
	if err := params.Validate(); err != nil {
		return PackScore{}, err
	}
	panel := append([]PanelMember(nil), in.Panel...)
	sort.Slice(panel, func(i, j int) bool { return panel[i].AgentID < panel[j].AgentID })
	byAgent := make([]map[string]PanelCaseResult, len(panel))
	for i, m := range panel {
		if m.AgentID == "" {
			return PackScore{}, errors.New("panel member has an empty agent_id")
		}
		if i > 0 && panel[i-1].AgentID == m.AgentID {
			return PackScore{}, fmt.Errorf("duplicate panel agent %q", m.AgentID)
		}
		byAgent[i] = map[string]PanelCaseResult{}
		for _, r := range m.Results {
			if math.IsNaN(r.Score) || r.Score < 0 || r.Score > 1 {
				return PackScore{}, fmt.Errorf("agent %q case %q score out of range", m.AgentID, r.CaseID)
			}
			byAgent[i][r.CaseID] = r
		}
	}
	claimed := map[string]bool{}
	for _, fp := range in.ClaimedFingerprints {
		claimed[fp] = true
	}

	out := PackScore{Status: PackScored, Cases: make([]CaseVerdict, 0, len(cases))}
	for _, c := range cases {
		cv := CaseVerdict{CaseID: c.CaseID, Fingerprint: c.Fingerprint}
		if claimed[cv.Fingerprint] {
			cv.Status = CaseDuplicate
			out.Duplicate++
			out.Cases = append(out.Cases, cv)
			continue
		}
		var sum float64
		solverOwners := map[string]bool{}
		for i, m := range panel {
			if in.GeneratorOwnerKey != "" && m.OwnerKey == in.GeneratorOwnerKey {
				continue
			}
			r, ok := byAgent[i][c.CaseID]
			if !ok || r.Fault {
				continue
			}
			cv.PanelSize++
			sum += r.Score
			if r.Score >= params.SolveThreshold {
				cv.Solvers++
				solverOwners[m.OwnerKey] = true
			} else if m.Champion {
				cv.ChampionFailed = true
			}
		}
		cv.SolverOwners = len(solverOwners)
		if cv.PanelSize > 0 {
			cv.MeanScore = sum / float64(cv.PanelSize)
		}
		switch {
		case cv.PanelSize < params.MinPanel:
			cv.Status = CaseInsufficientPanel
			out.Status = PackInsufficientPanel
		case cv.SolverOwners < params.MinSolverOwners:
			cv.Status = CaseUnsolved
			out.Unsolved++
		case cv.Solvers == cv.PanelSize:
			cv.Status = CaseTrivial
			out.Trivial++
		default:
			cv.Status = CaseDiscriminating
			out.Discriminating++
			cv.Credit = 1 - cv.MeanScore
			if cv.ChampionFailed {
				cv.Credit *= 1 + params.ChampionBonus
			}
			out.RawCredit += cv.Credit
		}
		out.Cases = append(out.Cases, cv)
	}
	if out.Status != PackScored {
		out.RawCredit = 0
		for i := range out.Cases {
			out.Cases[i].Credit = 0
		}
		return out, nil
	}
	considered := len(cases) - out.Duplicate
	if considered > 0 {
		out.UnsolvedRate = float64(out.Unsolved) / float64(considered)
	}
	denom := len(cases)
	if denom < params.TargetCases {
		denom = params.TargetCases
	}
	solvable := 1 - out.UnsolvedRate
	out.Score = out.RawCredit / ((1 + params.ChampionBonus) * float64(denom)) * solvable * solvable
	if out.Score > 1 {
		out.Score = 1
	}
	return out, nil
}

// SeedPanel is one seed's pack, reduced to case refs, and its panel results.
type SeedPanel struct {
	Seed  string        `json:"seed"`
	Cases []CaseRef     `json:"cases"`
	Panel []PanelMember `json:"panel"`
}

// RunInput is one generator evaluation: the packs it produced for each of the
// run's secret seeds, each run against the same panel.
type RunInput struct {
	GeneratorOwnerKey string      `json:"generator_owner_key"`
	Seeds             []SeedPanel `json:"seeds"`
	// ClaimedFingerprints are case fingerprints earlier runs (of any
	// generator) already hold credit for.
	ClaimedFingerprints []string `json:"claimed_fingerprints,omitempty"`
}

// RunScore is a generator's score for one evaluation.
type RunScore struct {
	Status string      `json:"status"`
	Score  float64     `json:"score"`
	Seeds  []PackScore `json:"seeds"`
}

// ScoreRun scores a generator evaluation: each seed's pack with ScoreCases,
// then the mean pack score across seeds.
//
// Seeds are scored in order, and every case fingerprint a seed emits joins the
// claimed set for later seeds. A generator that ignores its seed therefore
// scores its first pack and duplicates for the rest: the mean rewards a
// program whose output is fresh for every seed, not one good sample. Any seed
// with an insufficient panel makes the whole run insufficient (re-queued, no
// score), so a run is never judged on a subset of its seeds.
func ScoreRun(in RunInput, params ScoreParams) (RunScore, error) {
	if len(in.Seeds) == 0 {
		return RunScore{}, errors.New("run has no seeds")
	}
	claimed := append([]string(nil), in.ClaimedFingerprints...)
	out := RunScore{Status: PackScored, Seeds: make([]PackScore, 0, len(in.Seeds))}
	var sum float64
	for _, seed := range in.Seeds {
		ps, err := ScoreCases(seed.Cases, ScoreInput{
			GeneratorOwnerKey:   in.GeneratorOwnerKey,
			Panel:               seed.Panel,
			ClaimedFingerprints: claimed,
		}, params)
		if err != nil {
			return RunScore{}, fmt.Errorf("seed %s: %w", seed.Seed, err)
		}
		if ps.Status != PackScored {
			out.Status = PackInsufficientPanel
		}
		sum += ps.Score
		out.Seeds = append(out.Seeds, ps)
		for _, c := range seed.Cases {
			claimed = append(claimed, c.Fingerprint)
		}
	}
	if out.Status != PackScored {
		return out, nil
	}
	out.Score = sum / float64(len(in.Seeds))
	return out, nil
}
