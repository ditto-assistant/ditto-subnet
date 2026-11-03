package familypanel

import (
	"math"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/internal/familygate"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/universe"
)

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func TestTrivialMaxIsTheWeakestOwner(t *testing.T) {
	if got := TrivialMax([]float64{0.9, 0.4, 1}); !near(got, 0.4) {
		t.Fatalf("TrivialMax = %v, want 0.4", got)
	}
	if got := TrivialMax(nil); got != 0 {
		t.Fatalf("TrivialMax(nil) = %v, want 0", got)
	}
}

func TestUnsolvedMaxMatchesHandWorkedCouplings(t *testing.T) {
	for _, tc := range []struct {
		name  string
		rates []float64
		k     int
		want  float64
	}{
		// 87.5% of cases solved by all five, 12.5% by exactly one each.
		{"five at 0.9", []float64{0.9, 0.9, 0.9, 0.9, 0.9}, 2, 0.125},
		{"five at 0.3", []float64{0.3, 0.3, 0.3, 0.3, 0.3}, 2, 0.875},
		// Two owners solve everything: no case can have fewer than two.
		{"two perfect owners", []float64{1, 1, 0, 0, 0}, 2, 0},
		{"total solve mass below k-1", []float64{0.2, 0.2, 0.2}, 2, 1},
		{"fewer owners than k", []float64{1}, 2, 1},
		{"one perfect owner", []float64{1, 0.5, 0.5}, 2, 0.5},
	} {
		if got := UnsolvedMax(tc.rates, tc.k); !near(got, tc.want) {
			t.Errorf("%s: UnsolvedMax = %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestUnsolvedMaxBoundsTheIndependentCoupling(t *testing.T) {
	for _, rates := range [][]float64{
		{0.9, 0.8, 0.7, 0.6, 0.5}, {0.25, 0.5, 0.75, 1, 0}, {0.1, 0.1, 0.9, 0.9, 0.5},
	} {
		trivial, unsolved := Independent(rates, 2)
		if unsolved > UnsolvedMax(rates, 2)+1e-9 {
			t.Errorf("%v: independent unsolved %v exceeds the bound %v", rates, unsolved, UnsolvedMax(rates, 2))
		}
		if trivial > TrivialMax(rates)+1e-9 {
			t.Errorf("%v: independent trivial %v exceeds the bound %v", rates, trivial, TrivialMax(rates))
		}
	}
}

func TestIndependent(t *testing.T) {
	trivial, unsolved := Independent([]float64{0.5, 0.5}, 2)
	if !near(trivial, 0.25) || !near(unsolved, 0.75) {
		t.Fatalf("Independent = (%v, %v), want (0.25, 0.75)", trivial, unsolved)
	}
}

func TestLabelDatasetLabelsEveryProgramCase(t *testing.T) {
	const seed = 4081349795126678977 // a gate-3 canary seed
	l, err := LabelDataset(seed, "full", protocol.BenchVersionV14)
	if err != nil {
		t.Fatal(err)
	}
	// Full profile: four open-program business groups and six personal groups.
	if len(l.Family) != 40 {
		t.Fatalf("labelled %d program cases, want 40", len(l.Family))
	}
	counts := map[string]int{}
	for _, f := range l.Family {
		counts[f]++
	}
	for f, n := range counts {
		if n%4 != 0 {
			t.Errorf("family %s has %d cases, want whole metamorphic groups", f, n)
		}
		if _, ok := universe.V14FamilyMixFor(f); !ok {
			t.Errorf("family %s is not in the v14 family-mix ledger", f)
		}
	}
	for g := 0; g < 4; g++ {
		f := string(universe.V13FamilyOfVersion(seed, 4*g, protocol.BenchVersionV14))
		if counts[f] == 0 {
			t.Errorf("business group %d family %s missing", g, f)
		}
	}
	if !strings.HasPrefix(l.DatasetSHA256, "4134b18a") {
		t.Errorf("dataset %s, want the canary's 4134b18a...", l.DatasetSHA256)
	}
}

// syntheticLabels builds labels for families named f0..fN with cases c<f>-<i>.
func syntheticLabels(seed int64, families map[string]int) Labels {
	l := Labels{Seed: seed, DatasetSHA256: "d" + string(rune('0'+seed%10)), Family: map[string]string{}}
	for f, n := range families {
		for i := 0; i < n; i++ {
			l.Family[f+"-"+string(rune('a'+i))] = f
		}
	}
	return l
}

func TestEvaluateJudgesWithFamilygateThresholds(t *testing.T) {
	fams := map[string]int{"identity-join": 4, "school-logistics": 4}
	labelsFor := func(seed int64) (Labels, error) { return syntheticLabels(seed, fams), nil }
	var runs []Run
	for seed := int64(1); seed <= 5; seed++ {
		l := syntheticLabels(seed, fams)
		r := Run{Run: "r", Owner: "owner-" + string(rune('0'+seed)), Seed: seed, DatasetSHA256: l.DatasetSHA256}
		for id, f := range l.Family {
			score := 1.0
			// identity-join: owners 1-3 solve all 4, owners 4-5 solve 2 of 4.
			if f == "identity-join" && seed > 3 && (id == f+"-a" || id == f+"-b") {
				score = 0
			}
			r.PerCase = append(r.PerCase, protocol.CaseScore{CaseID: id, Score: score})
		}
		runs = append(runs, r)
	}
	got, err := Evaluate(runs, labelsFor, familygate.DefaultParams(), Options{MinPanelOwners: 5, MinOwnerCases: 4})
	if err != nil {
		t.Fatal(err)
	}
	by := map[string]FamilyResult{}
	for _, r := range got {
		by[r.Family] = r
	}
	if r := by["school-logistics"]; r.Verdict != familygate.HoldTrivial || !near(r.TrivialMax, 1) {
		t.Errorf("school-logistics = %s trivial %v, want hold-trivial at 1", r.Verdict, r.TrivialMax)
	}
	ij := by["identity-join"]
	if ij.Verdict != familygate.Admit {
		t.Errorf("identity-join = %s %v, want admit", ij.Verdict, ij.Reasons)
	}
	if !near(ij.TrivialMax, 0.5) || ij.Seeds != 5 || len(ij.Owners) != 5 {
		t.Errorf("identity-join measured %+v", ij)
	}
}

func TestEvaluateRefusesMismatchedOrIncompleteRuns(t *testing.T) {
	fams := map[string]int{"identity-join": 4}
	labelsFor := func(seed int64) (Labels, error) { return syntheticLabels(seed, fams), nil }
	l := syntheticLabels(1, fams)
	full := []protocol.CaseScore{}
	for id := range l.Family {
		full = append(full, protocol.CaseScore{CaseID: id, Score: 1})
	}
	p, o := familygate.DefaultParams(), DefaultOptions()
	if _, err := Evaluate([]Run{{Run: "r", Owner: "o", Seed: 1, DatasetSHA256: "other", PerCase: full}}, labelsFor, p, o); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Errorf("mismatched digest: err = %v", err)
	}
	if _, err := Evaluate([]Run{{Run: "r", Owner: "o", Seed: 1, DatasetSHA256: l.DatasetSHA256, PerCase: full[:2]}}, labelsFor, p, o); err == nil || !strings.Contains(err.Error(), "scored 2 of 4") {
		t.Errorf("incomplete run: err = %v", err)
	}
	if _, err := Evaluate([]Run{{Run: "r", Seed: 1, DatasetSHA256: l.DatasetSHA256, PerCase: full}}, labelsFor, p, o); err == nil {
		t.Error("ownerless run accepted")
	}
}

func TestThinPanelNeverAdmits(t *testing.T) {
	fams := map[string]int{"identity-join": 4}
	labelsFor := func(seed int64) (Labels, error) { return syntheticLabels(seed, fams), nil }
	var runs []Run
	for seed := int64(1); seed <= 3; seed++ {
		l := syntheticLabels(seed, fams)
		r := Run{Run: "r", Owner: "owner-" + string(rune('0'+seed)), Seed: seed, DatasetSHA256: l.DatasetSHA256}
		// Owners 1 and 2 solve everything, owner 3 half: admissible rates.
		for _, id := range []string{"identity-join-a", "identity-join-b", "identity-join-c", "identity-join-d"} {
			score := 1.0
			if seed == 3 && id < "identity-join-c" {
				score = 0
			}
			r.PerCase = append(r.PerCase, protocol.CaseScore{CaseID: id, Score: score})
		}
		runs = append(runs, r)
	}
	got, err := Evaluate(runs, labelsFor, familygate.DefaultParams(), DefaultOptions())
	if err != nil {
		t.Fatal(err)
	}
	if got[0].Verdict != HoldThinPanel {
		t.Fatalf("verdict %s %v, want %s", got[0].Verdict, got[0].Reasons, HoldThinPanel)
	}
}
