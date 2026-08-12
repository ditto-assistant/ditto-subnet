package main

import (
	"math"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/datagen"
	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestErrDrawDeterministicAndCalibrated pins that the mid-tier error draw is
// byte-reproducible (same key+salt → same outcome) and that its empirical fail
// rate over many keys tracks the requested rate (it is a hash-uniform draw,
// not a biased one).
func TestErrDrawDeterministicAndCalibrated(t *testing.T) {
	if errDraw("k1", "A", 0.3) != errDraw("k1", "A", 0.3) {
		t.Fatal("errDraw is not deterministic")
	}
	if errDraw("k1", "A", 0) != 1 {
		t.Fatal("rate 0 must always pass")
	}
	if errDraw("k1", "A", 1) != 0 {
		t.Fatal("rate 1 must always fail")
	}
	for _, rate := range []float64{0.05, 0.25} {
		fails := 0
		n := 20000
		for i := 0; i < n; i++ {
			if errDraw(string(rune('a'+i%26))+string(rune('A'+(i/26)%26))+string(rune('0'+(i/676)%10))+string(rune(i)), "A", rate) == 0 {
				fails++
			}
		}
		got := float64(fails) / float64(n)
		if math.Abs(got-rate) > 0.02 {
			t.Errorf("empirical fail rate %.3f for requested %.2f", got, rate)
		}
	}
}

// TestEvalSeedDeterministic pins that scoring one generated dataset twice
// yields identical per-strategy results — the study's noise floor is zero, so
// every reported spread is dataset + modeled-agent variance, not tooling noise.
func TestEvalSeedDeterministic(t *testing.T) {
	prof, _ := gen.ProfileForVersion("full", protocol.BenchVersionV7)
	a, err := gen.GenerateDataset(42, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatal(err)
	}
	mk := func() *versionResult {
		vr := &versionResult{
			Runs: map[string][]seedResult{}, Cases: map[string][][]caseScore{},
			PairB: map[string][]float64{}, Exp: map[string][]float64{}, ExpFlat: map[string][]float64{},
		}
		vr.Seeds = []int64{42}
		evalSeed(vr, a, protocol.BenchVersionV7, 42)
		return vr
	}
	v1, v2 := mk(), mk()
	if !reflect.DeepEqual(v1.Runs, v2.Runs) || !reflect.DeepEqual(v1.PairB, v2.PairB) || !reflect.DeepEqual(v1.Exp, v2.Exp) {
		t.Fatal("evalSeed is not deterministic across identical inputs")
	}
	// The oracle strategy must score a clean 1.0 composite: the study's ceiling
	// rides the same answer-key sufficiency invariant as TestV7OracleSolvable.
	if v1.OracleFailures != 0 {
		t.Fatalf("oracle failed %d cases", v1.OracleFailures)
	}
	or := v1.Runs["oracle"][0]
	if or.Composite != 1 || or.MemMean != 1 || or.ToolMean != 1 {
		t.Fatalf("oracle composite %v", or)
	}
	// The strong tier sits between the naive tiers and the oracle — the
	// decision-boundary regime the study models.
	st := v1.Runs["strong"][0].Composite
	if st <= v1.Runs["overlap"][0].Composite || st >= 1 {
		t.Fatalf("strong composite %.3f is not in the mid tier", st)
	}
	// Expected (structural) composite is within Bernoulli reach of the realized
	// one: |realized - expected| under 10 SDs of the ~250-case binomial.
	if math.Abs(st-v1.Exp["strong"][0]) > 0.15 {
		t.Fatalf("strong realized %.3f vs expected %.3f", st, v1.Exp["strong"][0])
	}
	// The champion anchors order correctly on the deepened suite: weak below
	// strong, both below the legacy mid tier and above the naive tiers.
	cs, cw := v1.Runs["champS"][0].Composite, v1.Runs["champW"][0].Composite
	if !(cw < cs && cs < st) {
		t.Fatalf("champion ordering broken: champW=%.3f champS=%.3f strong=%.3f", cw, cs, st)
	}
}

// TestChampionTablesMatchAnchors pins the ported champion-tier tables against
// the calibration outcome of gen's TestV7ChampionTierLandsNearTarget (the
// source of truth this file duplicates, since test symbols are not
// importable): on the same five seeds, the expected FLAT mean of the weak
// anchor lands in the deepened target band [0.28, 0.42] on v7, and the strong
// anchor reproduces the pre-deepening rebench range [0.78, 0.88] on v6. If
// the gen tables move and these copies are not updated, this fails.
func TestChampionTablesMatchAnchors(t *testing.T) {
	if testing.Short() {
		t.Skip("multi-dataset generation pass")
	}
	seeds := []int64{11, 22, 33, 44, 55}
	flat := func(bv int, pass func(qt string) float64, passTool func(cat string) float64) float64 {
		prof, _ := gen.ProfileForVersion("full", bv)
		sum, n := 0.0, 0
		for _, seed := range seeds {
			a, err := gen.GenerateDataset(seed, prof, bv)
			if err != nil {
				t.Fatal(err)
			}
			for _, c := range a.MemoryCases {
				sum += pass(c.QuestionType)
				n++
			}
			for _, c := range a.ToolCases {
				sum += passTool(c.Category)
				n++
			}
		}
		return sum / float64(n)
	}
	weakV7 := flat(protocol.BenchVersionV7,
		func(qt string) float64 { return champWeakRate(champPassMemS(qt)) },
		func(cat string) float64 { return champWeakRate(champPassToolS(cat)) })
	strongV6 := flat(protocol.BenchVersionV6, champPassMemS, champPassToolS)
	strongV7 := flat(protocol.BenchVersionV7, champPassMemS, champPassToolS)
	t.Logf("flat means: strong v6=%.3f v7=%.3f, weak v7=%.3f", strongV6, strongV7, weakV7)
	if weakV7 < 0.28 || weakV7 > 0.42 {
		t.Errorf("weak anchor flat mean %.3f outside [0.28,0.42]", weakV7)
	}
	if strongV6 < 0.78 || strongV6 > 0.88 {
		t.Errorf("strong anchor v6 flat mean %.3f outside [0.78,0.88]", strongV6)
	}
	if strongV7 > strongV6*0.75 {
		t.Errorf("strong anchor did not drop enough: v6=%.3f v7=%.3f", strongV6, strongV7)
	}
}

// TestSeedsForMath pins the confirmation-seed formula against a hand-computed
// value: n = ceil(((z_a+z_b)*sd/gap)^2).
func TestSeedsForMath(t *testing.T) {
	if got := seedsFor(0.0279, 0.01, 1.645, 1.645); got != 85 {
		t.Errorf("seedsFor(0.0279, 0.01, 95%% power) = %d, want 85", got)
	}
	if got := seedsFor(0.0259, 0.005, 1.645, 0); got != 73 {
		t.Errorf("seedsFor(0.0259, 0.005, detect) = %d, want 73", got)
	}
	if got := seedsFor(0.001, 0.5, 1.645, 1.645); got != 1 {
		t.Errorf("tiny sd must floor at 1 seed, got %d", got)
	}
}

func TestParseBenchVersions(t *testing.T) {
	got, err := parseBenchVersions(" 8,9,10,8 ")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, []int{protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10}) {
		t.Fatalf("versions=%v, want [8 9 10]", got)
	}
	for _, raw := range []string{"", ",", "9,garbage", "11"} {
		if _, err := parseBenchVersions(raw); err == nil {
			t.Errorf("parseBenchVersions(%q) unexpectedly succeeded", raw)
		}
	}
}

func TestSummarizeFamilyMixCountsAbsenceAndVariation(t *testing.T) {
	mixes := []map[string]int{
		{"set_effort": 1, "stale_context_web": 4},
		{"set_effort": 1, "stale_context_web": 6},
		{"set_effort": 1, "world_memory_update": 2},
	}
	got := summarizeFamilyMix(mixes)
	if got.Runs != 3 || got.DistinctHistograms != 3 {
		t.Fatalf("summary header=%+v", got)
	}
	byFamily := map[string]FamilyMixEntry{}
	for _, entry := range got.Families {
		byFamily[entry.Family] = entry
	}
	if entry := byFamily["set_effort"]; entry.RunsPresent != 3 || entry.Min != 1 || entry.Max != 1 || entry.Mean != 1 {
		t.Errorf("set_effort=%+v", entry)
	}
	if entry := byFamily["stale_context_web"]; entry.RunsPresent != 2 || entry.Min != 0 || entry.Max != 6 || entry.Mean != 3.3333 {
		t.Errorf("stale_context_web=%+v", entry)
	}
	if entry := byFamily["stale_context_web"]; entry.Variance != 6.2222 {
		t.Errorf("stale_context_web variance=%v, want 6.2222", entry.Variance)
	}
	if entry := byFamily["world_memory_update"]; entry.RunsPresent != 1 || entry.Min != 0 || entry.Max != 2 || entry.Mean != 0.6667 {
		t.Errorf("world_memory_update=%+v", entry)
	}
}

func TestV9StudyRunReportsFinalFamilyMix(t *testing.T) {
	if testing.Short() {
		t.Skip("generates several full datasets")
	}
	result := runVersion(protocol.BenchVersionV9, "full", 1, 3, "")
	tool := summarizeFamilyMix(result.ToolMixes)
	memory := summarizeFamilyMix(result.MemoryMixes)
	if tool.Runs != 3 || len(tool.Families) != 53 || tool.DistinctHistograms < 2 {
		t.Errorf("tool mix summary=%+v", tool)
	}
	if memory.Runs != 3 || len(memory.Families) != 22 || memory.DistinctHistograms < 2 {
		t.Errorf("memory mix summary=%+v", memory)
	}
	for _, entry := range tool.Families {
		if entry.Min < 1 || entry.RunsPresent != 3 {
			t.Errorf("tool family lacks full-run floor: %+v", entry)
		}
	}
	summary := result.summarize(0.007)
	if _, ok := summary["strategies"]; ok {
		t.Fatal("uncalibrated v9 study published simulated strategy evidence")
	}
	calibration := summary["score_calibration"].(map[string]string)
	if calibration["status"] != "pending" || !strings.Contains(calibration["reason"], "explicit measured rate") {
		t.Fatalf("v9 calibration warning=%v", calibration)
	}
	contract := summary["final_distribution_contract"].(map[string]any)
	worldCounts := contract["world_action_counts"].(CountDistribution)
	composedCounts := contract["composed_case_counts"].(CountDistribution)
	if worldCounts.Min < datagen.V9FullWorldActionMinimum || worldCounts.Max > datagen.V9FullWorldActionTarget {
		t.Errorf("world counts=%+v", worldCounts)
	}
	if composedCounts.Min < datagen.V9FullComposedCaseMinimum {
		t.Errorf("composed counts=%+v", composedCounts)
	}
}

func TestV9StudyDoesNotWriteUncalibratedGStudyInput(t *testing.T) {
	dir := t.TempDir()
	result := runVersion(protocol.BenchVersionV9, "small", 1, 2, dir)
	if len(result.Runs) != 0 || len(result.Cases) != 0 {
		t.Fatalf("v9 populated uncalibrated score runs: runs=%d cases=%d", len(result.Runs), len(result.Cases))
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("v9 wrote uncalibrated G-study files: %v", entries)
	}
}
