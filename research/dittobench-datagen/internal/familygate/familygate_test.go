package familygate

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

func loadEvidence(t *testing.T) []CaseRow {
	t.Helper()
	raw, err := os.ReadFile("testdata/sn351_evidence.json")
	if err != nil {
		t.Fatal(err)
	}
	var rows []CaseRow
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatal(err)
	}
	return rows
}

// TestSN351EvidenceVerdicts replays the generator-round evidence SN-351 cites
// (transcribed per case or per column aggregate into testdata) and pins the
// verdict the default gate reaches for each family. The always-trivial shapes
// are held as trivial, the always-unsolved ones as unsolved, and the
// discriminating revert chain SN-353 ports is admitted.
func TestSN351EvidenceVerdicts(t *testing.T) {
	rows, err := Evaluate(loadEvidence(t), DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"public-default":           HoldTrivial,
		"needle-and-as-of-columns": HoldTrivial,
		"conditional":              HoldUnsolved,
		"alias-reroute":            HoldUnsolved,
		"owner-alias-email":        HoldUnsolved,
		"revert-to-original":       Admit,
		"revised-cheapest":         HoldUnsolved,
	}
	if len(rows) != len(want) {
		t.Fatalf("got %d families, want %d", len(rows), len(want))
	}
	for _, r := range rows {
		if r.Verdict != want[r.Family] {
			t.Errorf("%s: verdict %s (%v), want %s", r.Family, r.Verdict, r.Reasons, want[r.Family])
		}
	}
	byFamily := map[string]FamilyRow{}
	for _, r := range rows {
		byFamily[r.Family] = r
	}
	pd := byFamily["public-default"]
	if pd.Judged != 76 || pd.Trivial != 76 || pd.Duplicate != 4 || pd.Rounds != 3 || pd.TrivialRate != 1 {
		t.Fatalf("public default row: %+v", pd)
	}
	rv := byFamily["revert-to-original"]
	if rv.Rounds != 3 || rv.Judged != 4 || rv.Trivial != 2 || rv.Discriminating != 2 || rv.Unsolved != 0 {
		t.Fatalf("revert row: %+v", rv)
	}
	if got := byFamily["conditional"]; got.UnsolvedRate < 0.85 || got.Discriminating != 1 {
		t.Fatalf("conditional row: %+v", got)
	}
}

func TestGateHoldsTooFewRounds(t *testing.T) {
	rows := []CaseRow{
		{Round: 1, Family: "x", PanelSize: 5, Solvers: 2, SolverOwners: 2},
		{Round: 2, Family: "x", PanelSize: 5, Solvers: 3, SolverOwners: 3},
	}
	got, err := Evaluate(rows, DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Verdict != HoldInsufficient {
		t.Fatalf("two rounds: %+v", got[0])
	}
	rows = append(rows, CaseRow{Round: 3, Family: "x", PanelSize: 5, Solvers: 4, SolverOwners: 4})
	got, err = Evaluate(rows, DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Verdict != Admit {
		t.Fatalf("three rounds: %+v", got[0])
	}
}

// One owner solving a case is not ">= 2 independent owners": a single agent
// that baked a family into its image cannot make the family admissible.
func TestGateCountsSolverOwnersNotSolvers(t *testing.T) {
	rows := []CaseRow{
		{Round: 1, Family: "x", PanelSize: 5, Solvers: 3, SolverOwners: 1},
		{Round: 2, Family: "x", PanelSize: 5, Solvers: 3, SolverOwners: 1},
		{Round: 3, Family: "x", PanelSize: 5, Solvers: 3, SolverOwners: 2},
	}
	got, err := Evaluate(rows, DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Verdict != HoldUnsolved || got[0].Unsolved != 2 {
		t.Fatalf("owner count: %+v", got[0])
	}
}

func TestGateExcludesDuplicatesFromRates(t *testing.T) {
	rows := []CaseRow{
		{Round: 1, Family: "x", Status: challenge.CaseDiscriminating},
		{Round: 2, Family: "x", Status: challenge.CaseDiscriminating},
		{Round: 3, Family: "x", Status: challenge.CaseTrivial},
		{Round: 3, Family: "x", Status: challenge.CaseDuplicate, Count: 10},
		{Round: 3, Family: "x", Status: challenge.CaseInsufficientPanel, Count: 10},
	}
	got, err := Evaluate(rows, DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	r := got[0]
	if r.Judged != 3 || r.Cases != 23 || r.Verdict != Admit {
		t.Fatalf("duplicates leaked into the rates: %+v", r)
	}
	only := []CaseRow{{Round: 1, Family: "y", Status: challenge.CaseDuplicate}}
	got, err = Evaluate(only, DefaultParams())
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Verdict != HoldNoJudgedCases {
		t.Fatalf("all-duplicate family: %+v", got[0])
	}
}

func TestGateRejectsBadRows(t *testing.T) {
	for name, row := range map[string]CaseRow{
		"no family":      {Round: 1, PanelSize: 5},
		"unknown status": {Round: 1, Family: "x", Status: "easy"},
		"too many":       {Round: 1, Family: "x", PanelSize: 5, Solvers: 6, SolverOwners: 2},
		"owners>solvers": {Round: 1, Family: "x", PanelSize: 5, Solvers: 1, SolverOwners: 2},
		"no panel":       {Round: 1, Family: "x"},
		"negative count": {Round: 1, Family: "x", Status: challenge.CaseTrivial, Count: -1},
	} {
		if _, err := Evaluate([]CaseRow{row}, DefaultParams()); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	bad := DefaultParams()
	bad.MaxTrivialRate = 2
	if _, err := Evaluate(nil, bad); err == nil {
		t.Fatal("accepted an out-of-range trivial cap")
	}
}

// TestAtThresholdFlagsHalfCreditSolves pins the SN-352 signal: when every
// solver scored exactly the solve threshold and everyone else scored zero, the
// panel mean is threshold * solvers / panel.
func TestAtThresholdFlagsHalfCreditSolves(t *testing.T) {
	cases := []struct {
		row  CaseRow
		want bool
	}{
		{CaseRow{PanelSize: 5, Solvers: 5, MeanScore: 0.5}, true},
		{CaseRow{PanelSize: 5, Solvers: 2, MeanScore: 0.2}, true},
		{CaseRow{PanelSize: 5, Solvers: 2, MeanScore: 0.4}, false},
		{CaseRow{PanelSize: 5, Solvers: 3, MeanScore: 0.7}, false},
		{CaseRow{PanelSize: 5, Solvers: 0, MeanScore: 0}, false},
	}
	for _, c := range cases {
		if got := AtThreshold(c.row, 0.5); got != c.want {
			t.Errorf("%+v: got %v, want %v", c.row, got, c.want)
		}
	}
}

func TestMarkdownListsEveryFamily(t *testing.T) {
	p := DefaultParams()
	rows, err := Evaluate(loadEvidence(t), p)
	if err != nil {
		t.Fatal(err)
	}
	md := Markdown(rows, p)
	for _, r := range rows {
		if !strings.Contains(md, "| "+r.Family+" |") {
			t.Fatalf("table omits %s:\n%s", r.Family, md)
		}
	}
	if !strings.Contains(md, "trivial rate <= 0.60") {
		t.Fatalf("table omits the gate line:\n%s", md)
	}
}
