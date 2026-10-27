// Package familygate is the admission gate a candidate family must clear
// before it enters bench v14 datagen (SN-351).
//
// The generator competition already measures how the current top panel does
// on every case a generator emits (challenge.ScoreCases): a case is trivial
// when every eligible agent solves it, unsolved when fewer than
// min_solver_owners distinct owners solve it, and discriminating otherwise.
// The rounds behind SN-351 show the two ways a family can add the wrong kind
// of mass. Some shapes are solved by every top agent in every round (the public
// default's update chain, two-hop, single retraction and scattered list, and
// single-hop as-of / needle shapes). They add easy, cue-solvable cases. Others
// are solved by nobody (conditional, alias-reroute at their current
// difficulty). They inflate difficulty with cases that may be broken.
//
// Evaluate folds per-case panel verdicts, labelled by family, into one row per
// family and applies the rule:
//
//   - at least MinRounds distinct rounds (the panel is five agents, so one
//     round is noise);
//   - trivial rate at most MaxTrivialRate (target <= 60%);
//   - unsolved rate at most MaxUnsolvedRate. A case counts as solved only
//     when >= MinSolverOwners distinct owners solve it, which mirrors
//     challenge.ScoreParams.MinSolverOwners, so this is the ">= 2 independent
//     owners solve it" condition.
//
// The rates are taken over judged cases: duplicates and cases with too small a
// panel are reported but carry no verdict. The input is the same verdict shape
// Backroom's generator round view and an offline panel run both produce, so a
// private operator column and a v14 qualification run share one report.
package familygate

import (
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

// Params are the gate thresholds.
type Params struct {
	// MinRounds is the fewest distinct rounds a family needs before it can
	// be admitted.
	MinRounds int `json:"min_rounds"`
	// MinSolverOwners is how many distinct owners must solve a case for it
	// to count as solvable. It is only used when a row carries no status.
	MinSolverOwners int `json:"min_solver_owners"`
	// SolveThreshold is the per-case score that counts as solved. It is only
	// used to flag cases where every solver sat exactly on the threshold.
	SolveThreshold float64 `json:"solve_threshold"`
	// MaxTrivialRate is the highest share of judged cases the whole panel
	// may solve.
	MaxTrivialRate float64 `json:"max_trivial_rate"`
	// MaxUnsolvedRate is the highest share of judged cases that fewer than
	// MinSolverOwners owners may solve.
	MaxUnsolvedRate float64 `json:"max_unsolved_rate"`
	// MaxAtThresholdRate is the highest share of solved (discriminating or
	// trivial) cases whose every solver scored exactly SolveThreshold. Above
	// it, the family is "solved" only through partial credit (SN-352): a
	// two-part case where agents earn the easy half and nobody earns the
	// hard half reads as solvable at threshold 0.5 without being solved.
	MaxAtThresholdRate float64 `json:"max_at_threshold_rate"`
}

// DefaultParams are the SN-351 values. MinSolverOwners and SolveThreshold
// follow the generator competition's shipped scoring so a family is held to
// the same solvability line its generator cases were judged on. The unsolved
// cap is the initial value: it holds every family SN-351 lists as unsolved
// (conditional, alias-reroute, owner-alias-email) and admits the
// discriminating chains SN-353 cites. Recheck it after one season.
func DefaultParams() Params {
	score := challenge.DefaultScoreParams()
	return Params{
		MinRounds:          3,
		MinSolverOwners:    score.MinSolverOwners,
		SolveThreshold:     score.SolveThreshold,
		MaxTrivialRate:     0.60,
		MaxUnsolvedRate:    1.0 / 3,
		MaxAtThresholdRate: 0.5,
	}
}

// Validate rejects thresholds that would make the gate meaningless.
func (p Params) Validate() error {
	switch {
	case p.MinRounds < 1:
		return fmt.Errorf("min_rounds must be at least 1")
	case p.MinSolverOwners < 1:
		return fmt.Errorf("min_solver_owners must be at least 1")
	case !(p.SolveThreshold > 0 && p.SolveThreshold <= 1):
		return fmt.Errorf("solve_threshold must be in (0, 1]")
	case !(p.MaxTrivialRate >= 0 && p.MaxTrivialRate <= 1):
		return fmt.Errorf("max_trivial_rate must be in [0, 1]")
	case !(p.MaxUnsolvedRate >= 0 && p.MaxUnsolvedRate <= 1):
		return fmt.Errorf("max_unsolved_rate must be in [0, 1]")
	case !(p.MaxAtThresholdRate >= 0 && p.MaxAtThresholdRate <= 1):
		return fmt.Errorf("max_at_threshold_rate must be in [0, 1]")
	}
	return nil
}

// CaseRow is one judged case of one family. Status is a challenge case status;
// when it is empty the row is judged from Solvers, SolverOwners and PanelSize.
type CaseRow struct {
	Round        int     `json:"round"`
	Column       string  `json:"column,omitempty"`
	Family       string  `json:"family"`
	CaseID       string  `json:"case_id,omitempty"`
	Status       string  `json:"status,omitempty"`
	PanelSize    int     `json:"panel_size"`
	Solvers      int     `json:"solvers"`
	SolverOwners int     `json:"solver_owners"`
	MeanScore    float64 `json:"mean_score"`
	// Source names the evidence: submission UUID, source SHA, or the run
	// that produced the row. It is carried into the report, never judged.
	Source string `json:"source,omitempty"`
	// Count is how many identical cases the row stands for; 0 means one. It
	// lets a report transcribe a column aggregate ("trivial 32/32") without
	// inventing case IDs.
	Count int `json:"count,omitempty"`
}

// Verdicts.
const (
	Admit               = "admit"
	HoldTrivial         = "hold-trivial"
	HoldUnsolved        = "hold-unsolved"
	HoldInsufficient    = "hold-insufficient-evidence"
	HoldNoJudgedCases   = "hold-no-judged-cases"
	HoldHalfCredit      = "hold-half-credit"
	statusUnknownReason = "unknown case status"
)

// FamilyRow is one family's measurement and verdict.
type FamilyRow struct {
	Family          string   `json:"family"`
	Rounds          int      `json:"rounds"`
	Cases           int      `json:"cases"`
	Judged          int      `json:"judged"`
	Discriminating  int      `json:"discriminating"`
	Trivial         int      `json:"trivial"`
	Unsolved        int      `json:"unsolved"`
	Duplicate       int      `json:"duplicate"`
	Insufficient    int      `json:"insufficient_panel"`
	TrivialRate     float64  `json:"trivial_rate"`
	UnsolvedRate    float64  `json:"unsolved_rate"`
	AtThresholdRate float64  `json:"at_threshold_rate"`
	SolverOwners    int      `json:"max_solver_owners"`
	AtThreshold     int      `json:"at_threshold"`
	Sources         []string `json:"sources,omitempty"`
	Verdict         string   `json:"verdict"`
	Reasons         []string `json:"reasons,omitempty"`
}

// Evaluate folds case rows into one verdict per family, sorted by family.
func Evaluate(rows []CaseRow, p Params) ([]FamilyRow, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	type acc struct {
		row     FamilyRow
		rounds  map[int]bool
		sources map[string]bool
	}
	by := map[string]*acc{}
	for i, r := range rows {
		family := strings.TrimSpace(r.Family)
		if family == "" {
			return nil, fmt.Errorf("row %d has no family", i)
		}
		a := by[family]
		if a == nil {
			a = &acc{row: FamilyRow{Family: family}, rounds: map[int]bool{}, sources: map[string]bool{}}
			by[family] = a
		}
		if r.Count < 0 {
			return nil, fmt.Errorf("row %d (%s): negative count", i, family)
		}
		if r.Round <= 0 {
			return nil, fmt.Errorf("row %d (%s): missing or invalid round", i, family)
		}
		n := max(r.Count, 1)
		a.row.Cases += n
		a.rounds[r.Round] = true
		if r.Source != "" {
			a.sources[r.Source] = true
		}
		status, err := caseStatus(r, p)
		if err != nil {
			return nil, fmt.Errorf("row %d (%s): %w", i, family, err)
		}
		// caseStatus has validated the counts; a status-only row carries none.
		if r.PanelSize > 0 && r.SolverOwners > a.row.SolverOwners {
			a.row.SolverOwners = r.SolverOwners
		}
		switch status {
		case challenge.CaseDiscriminating:
			a.row.Discriminating += n
		case challenge.CaseTrivial:
			a.row.Trivial += n
		case challenge.CaseUnsolved:
			a.row.Unsolved += n
		case challenge.CaseDuplicate:
			a.row.Duplicate += n
		case challenge.CaseInsufficientPanel:
			a.row.Insufficient += n
		}
		solved := status == challenge.CaseDiscriminating || status == challenge.CaseTrivial
		if solved && AtThreshold(r, p.SolveThreshold) {
			a.row.AtThreshold += n
		}
	}
	out := make([]FamilyRow, 0, len(by))
	for _, a := range by {
		row := a.row
		row.Rounds = len(a.rounds)
		row.Judged = row.Discriminating + row.Trivial + row.Unsolved
		for s := range a.sources {
			row.Sources = append(row.Sources, s)
		}
		sort.Strings(row.Sources)
		if row.Judged > 0 {
			row.TrivialRate = float64(row.Trivial) / float64(row.Judged)
			row.UnsolvedRate = float64(row.Unsolved) / float64(row.Judged)
		}
		if solved := row.Discriminating + row.Trivial; solved > 0 {
			row.AtThresholdRate = float64(row.AtThreshold) / float64(solved)
		}
		row.Verdict, row.Reasons = verdict(row, p)
		out = append(out, row)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Family < out[j].Family })
	return out, nil
}

func caseStatus(r CaseRow, p Params) (string, error) {
	// Counts are validated whenever present, including on rows that carry a
	// status, so a malformed row never reaches the report.
	if r.PanelSize < 0 || r.Solvers < 0 || r.SolverOwners < 0 ||
		(r.PanelSize > 0 && (r.Solvers > r.PanelSize || r.SolverOwners > r.Solvers)) ||
		(r.PanelSize == 0 && (r.Solvers > 0 || r.SolverOwners > 0)) {
		return "", fmt.Errorf("inconsistent counts: panel %d, solvers %d, solver owners %d", r.PanelSize, r.Solvers, r.SolverOwners)
	}
	switch r.Status {
	case challenge.CaseDiscriminating, challenge.CaseTrivial, challenge.CaseUnsolved,
		challenge.CaseDuplicate, challenge.CaseInsufficientPanel:
		return r.Status, nil
	case "":
	default:
		return "", fmt.Errorf("%s %q", statusUnknownReason, r.Status)
	}
	if r.PanelSize == 0 {
		return "", fmt.Errorf("a row without a status needs panel_size, solvers and solver_owners")
	}
	switch {
	case r.SolverOwners < p.MinSolverOwners:
		return challenge.CaseUnsolved, nil
	case r.Solvers == r.PanelSize:
		return challenge.CaseTrivial, nil
	}
	return challenge.CaseDiscriminating, nil
}

// AtThreshold reports whether every solver of a case scored exactly the solve
// threshold and every other agent scored zero: the panel mean equals
// threshold * solvers / panel. A family full of these cases is solved only by
// partial credit (SN-352), which the report surfaces instead of hiding inside
// the solved count.
func AtThreshold(r CaseRow, threshold float64) bool {
	if r.PanelSize <= 0 || r.Solvers <= 0 {
		return false
	}
	floor := threshold * float64(r.Solvers) / float64(r.PanelSize)
	return math.Abs(r.MeanScore-floor) < 1e-9
}

// Judge applies the admission rule to one family row's rates and rounds. It
// is what Evaluate uses, exported so a report that measures the rates another
// way (internal/familypanel) is held to the same thresholds.
func Judge(row FamilyRow, p Params) (string, []string) { return verdict(row, p) }

func verdict(row FamilyRow, p Params) (string, []string) {
	if row.Judged == 0 {
		return HoldNoJudgedCases, []string{"no judged cases (every case was a duplicate or lacked a panel)"}
	}
	var reasons []string
	held := ""
	hold := func(v, reason string) {
		if held == "" {
			held = v
		}
		reasons = append(reasons, reason)
	}
	if row.UnsolvedRate > p.MaxUnsolvedRate {
		hold(HoldUnsolved, fmt.Sprintf("unsolved rate %.2f > %.2f: fewer than %d owners solve too many cases", row.UnsolvedRate, p.MaxUnsolvedRate, p.MinSolverOwners))
	}
	if row.AtThresholdRate > p.MaxAtThresholdRate {
		hold(HoldHalfCredit, fmt.Sprintf("%.2f of solved cases are solved only at the %.2f threshold (partial credit, SN-352)", row.AtThresholdRate, p.SolveThreshold))
	}
	if row.TrivialRate > p.MaxTrivialRate {
		hold(HoldTrivial, fmt.Sprintf("trivial rate %.2f > %.2f: the whole panel solves it", row.TrivialRate, p.MaxTrivialRate))
	}
	if row.Rounds < p.MinRounds {
		hold(HoldInsufficient, fmt.Sprintf("%d round(s) < %d", row.Rounds, p.MinRounds))
	}
	if held == "" {
		return Admit, nil
	}
	return held, reasons
}

// markdownCell keeps a label inside one table cell.
func markdownCell(s string) string {
	return strings.NewReplacer(`\`, `\\`, "|", `\|`, "\r", " ", "\n", " ").Replace(s)
}

// Markdown renders the per-family table for the v14 qualification report.
func Markdown(rows []FamilyRow, p Params) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Gate: >= %d rounds, trivial rate <= %.2f, unsolved rate <= %.2f, at-threshold share of solved cases <= %.2f (solved = >= %d owners at score >= %.2f).\n\n",
		p.MinRounds, p.MaxTrivialRate, p.MaxUnsolvedRate, p.MaxAtThresholdRate, p.MinSolverOwners, p.SolveThreshold)
	b.WriteString("| family | rounds | judged | discriminating | trivial | unsolved | trivial rate | unsolved rate | max solver owners | at threshold | at-threshold rate | verdict |\n")
	b.WriteString("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|\n")
	for _, r := range rows {
		fmt.Fprintf(&b, "| %s | %d | %d | %d | %d | %d | %.2f | %.2f | %d | %d | %.2f | %s |\n",
			markdownCell(r.Family), r.Rounds, r.Judged, r.Discriminating, r.Trivial, r.Unsolved,
			r.TrivialRate, r.UnsolvedRate, r.SolverOwners, r.AtThreshold, r.AtThresholdRate, r.Verdict)
	}
	return b.String()
}
