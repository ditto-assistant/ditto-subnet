package challenge

import (
	"encoding/json"
	"flag"
	"math"
	"math/rand"
	"os"
	"sort"
	"testing"
)

var update = flag.Bool("update", false, "rewrite challenge/testdata golden files")

// ScoreVector is one cross-language scoring fixture. Platform's Python port
// (apps/platform/ditto/generator_scoring.py) replays testdata/score_vectors.json
// and must reproduce every field.
type ScoreVector struct {
	Name     string      `json:"name"`
	Cases    []CaseRef   `json:"cases"`
	Input    ScoreInput  `json:"input"`
	Params   ScoreParams `json:"params"`
	Expected PackScore   `json:"expected"`
}

func member(id, owner string, champion bool, scores map[string]float64, faults ...string) PanelMember {
	m := PanelMember{AgentID: id, OwnerKey: owner, Champion: champion}
	faulted := map[string]bool{}
	for _, f := range faults {
		faulted[f] = true
	}
	for caseID, s := range scores {
		m.Results = append(m.Results, PanelCaseResult{CaseID: caseID, Score: s, Fault: faulted[caseID]})
	}
	sort.Slice(m.Results, func(i, j int) bool { return m.Results[i].CaseID < m.Results[j].CaseID })
	return m
}

func uniform(cases []CaseRef, s float64) map[string]float64 {
	out := map[string]float64{}
	for _, c := range cases {
		out[c.CaseID] = s
	}
	return out
}

func randomScores(rng *rand.Rand, cases []CaseRef, pSolve float64) map[string]float64 {
	out := map[string]float64{}
	for _, c := range cases {
		if rng.Float64() < pSolve {
			out[c.CaseID] = 0.5 + 0.5*rng.Float64()
		} else {
			out[c.CaseID] = 0.5 * rng.Float64()
		}
	}
	return out
}

func buildVectors(t *testing.T) []ScoreVector {
	t.Helper()
	cases := ReferencePack(21, testHotkey).Refs()
	params := DefaultScoreParams()
	rng := rand.New(rand.NewSource(2026))
	var vs []ScoreVector
	add := func(name string, cs []CaseRef, in ScoreInput, p ScoreParams) {
		got, err := ScoreCases(cs, in, p)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		vs = append(vs, ScoreVector{Name: name, Cases: cs, Input: in, Params: p, Expected: got})
	}

	add("all_trivial", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a1", "o1", true, uniform(cases, 1)),
		member("a2", "o2", false, uniform(cases, 0.9)),
		member("a3", "o3", false, uniform(cases, 0.75)),
	}}, params)

	add("all_unsolved", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a1", "o1", true, uniform(cases, 0)),
		member("a2", "o2", false, uniform(cases, 0.25)),
		member("a3", "o3", false, uniform(cases, 0)),
	}}, params)

	add("mixed_random_panel", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("c-champ", "o1", true, randomScores(rng, cases, 0.55)),
		member("b-two", "o2", false, randomScores(rng, cases, 0.7)),
		member("a-three", "o3", false, randomScores(rng, cases, 0.6)),
		member("d-four", "o4", false, randomScores(rng, cases, 0.4)),
		member("e-five", "o5", false, randomScores(rng, cases, 0.8)),
	}}, params)

	// The generator's own agent solves everything; it must not count.
	half := map[string]float64{}
	for i, c := range cases {
		half[c.CaseID] = float64(i % 2)
	}
	add("generator_owned_agent_excluded", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a0", "gen", false, uniform(cases, 1)),
		member("a1", "o1", true, uniform(cases, 0)),
		member("a2", "o2", false, half),
		member("a3", "o3", false, half),
	}}, params)

	// Two solvers under one owner are one family: not solvable.
	add("single_owner_solvers_unsolved", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a1", "o1", true, uniform(cases, 0)),
		member("a2", "o2", false, uniform(cases, 1)),
		member("a3", "o2", false, uniform(cases, 1)),
	}}, params)

	add("faults_insufficient_panel", cases, ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a1", "o1", true, uniform(cases, 0)),
		member("a2", "o2", false, uniform(cases, 1)),
		member("a3", "o3", false, uniform(cases, 1), cases[0].CaseID),
	}}, params)

	claimed := []string{cases[0].Fingerprint, cases[5].Fingerprint, cases[9].Fingerprint}
	add("duplicates_claimed", cases, ScoreInput{GeneratorOwnerKey: "gen", ClaimedFingerprints: claimed, Panel: []PanelMember{
		member("x1", "o1", true, randomScores(rng, cases, 0.5)),
		member("x2", "o2", false, randomScores(rng, cases, 0.7)),
		member("x3", "o3", false, randomScores(rng, cases, 0.7)),
		member("x4", "o4", false, randomScores(rng, cases, 0.3)),
	}}, params)

	custom := params
	custom.MinSolverOwners = 1
	custom.ChampionBonus = 0
	custom.TargetCases = 8
	add("custom_params_small_target", cases[:10], ScoreInput{Panel: []PanelMember{
		member("a1", "o1", true, randomScores(rng, cases[:10], 0.5)),
		member("a2", "o2", false, randomScores(rng, cases[:10], 0.5)),
		member("a3", "o3", false, randomScores(rng, cases[:10], 0.5)),
	}}, custom)
	return vs
}

func TestScoreVectorsGolden(t *testing.T) {
	vs := buildVectors(t)
	path := "testdata/score_vectors.json"
	if *update {
		b, err := json.MarshalIndent(vs, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./challenge -run TestScoreVectorsGolden -update)", err)
	}
	var golden []ScoreVector
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}
	if len(golden) != len(vs) {
		t.Fatalf("golden has %d vectors, built %d", len(golden), len(vs))
	}
	for i := range vs {
		got, _ := json.Marshal(vs[i])
		want, _ := json.Marshal(golden[i])
		if string(got) != string(want) {
			t.Errorf("vector %q drifted; rerun with -update if intended", vs[i].Name)
		}
	}
}

func TestScoreSemantics(t *testing.T) {
	vs := map[string]ScoreVector{}
	for _, v := range buildVectors(t) {
		vs[v.Name] = v
	}
	if s := vs["all_trivial"].Expected; s.Score != 0 || s.Trivial != 16 {
		t.Errorf("all_trivial: %+v", s)
	}
	if s := vs["all_unsolved"].Expected; s.Score != 0 || s.Unsolved != 16 || s.UnsolvedRate != 1 {
		t.Errorf("all_unsolved: %+v", s)
	}
	if s := vs["single_owner_solvers_unsolved"].Expected; s.Unsolved != 16 || s.Score != 0 {
		t.Errorf("single owner: %+v", s)
	}
	g := vs["generator_owned_agent_excluded"].Expected
	if g.Discriminating != 8 || g.Unsolved != 8 {
		t.Errorf("generator exclusion: %+v", g)
	}
	// 8 discriminating cases, each with mean 2/3 and failed by the champion:
	// credit (1/3)(1.5)=0.5 each; score 4/(1.5*32) * (1-0.5)^2.
	if want := 4.0 / (1.5 * 32) * 0.25; math.Abs(g.Score-want) > 1e-12 {
		t.Errorf("generator exclusion score %v want %v", g.Score, want)
	}
	if s := vs["faults_insufficient_panel"].Expected; s.Status != PackInsufficientPanel || s.Score != 0 || s.Cases[0].Status != CaseInsufficientPanel {
		t.Errorf("insufficient panel: %+v", s.Cases[0])
	}
	d := vs["duplicates_claimed"].Expected
	if d.Duplicate != 3 || d.Cases[0].Status != CaseDuplicate || d.Cases[0].Credit != 0 {
		t.Errorf("duplicates: %+v", d)
	}
	m := vs["mixed_random_panel"].Expected
	if m.Score <= 0 || m.Score > 1 || m.Discriminating == 0 {
		t.Errorf("mixed: %+v", m)
	}
}

func TestScoreCountsInPackRepeatsAsDuplicates(t *testing.T) {
	cases := ReferencePack(1, testHotkey).Refs()
	repeat := append(append([]CaseRef(nil), cases...), CaseRef{CaseID: "again", Fingerprint: cases[0].Fingerprint})
	scores := uniform(cases, 1)
	scores["again"] = 1
	for k := range scores {
		if k == cases[0].CaseID || k == "again" {
			scores[k] = 0.6
		}
	}
	in := ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a1", "o1", true, scores),
		member("a2", "o2", false, scores),
		member("a3", "o3", false, uniform(repeat, 0)),
	}}
	got, err := ScoreCases(repeat, in, DefaultScoreParams())
	if err != nil {
		t.Fatal(err)
	}
	if got.Duplicate != 1 || got.Cases[len(got.Cases)-1].Status != CaseDuplicate {
		t.Fatalf("in-pack repeat must be a duplicate: %+v", got)
	}
}

func TestScoreRejectsBadInput(t *testing.T) {
	cases := ReferencePack(1, testHotkey).Refs()
	bad := []struct {
		name   string
		in     ScoreInput
		params ScoreParams
	}{
		{"zero params", ScoreInput{}, ScoreParams{}},
		{"duplicate agent", ScoreInput{Panel: []PanelMember{member("a", "o", false, nil), member("a", "p", false, nil)}}, DefaultScoreParams()},
		{"score out of range", ScoreInput{Panel: []PanelMember{member("a", "o", false, map[string]float64{cases[0].CaseID: 1.5})}}, DefaultScoreParams()},
		{"nan score", ScoreInput{Panel: []PanelMember{member("a", "o", false, map[string]float64{cases[0].CaseID: math.NaN()})}}, DefaultScoreParams()},
		{"empty owner", ScoreInput{Panel: []PanelMember{member("a", "", false, nil)}}, DefaultScoreParams()},
		{"duplicate case result", ScoreInput{Panel: []PanelMember{{AgentID: "a", OwnerKey: "o", Results: []PanelCaseResult{
			{CaseID: cases[0].CaseID, Score: 1}, {CaseID: cases[0].CaseID, Score: 0},
		}}}}, DefaultScoreParams()},
	}
	for _, b := range bad {
		if _, err := ScoreCases(cases, b.in, b.params); err == nil {
			t.Errorf("%s accepted", b.name)
		}
	}
}

func TestScoreRejectsACaseIDWithTwoFingerprints(t *testing.T) {
	cases := []CaseRef{{CaseID: "c", Fingerprint: "f1"}, {CaseID: "c", Fingerprint: "f2"}}
	if _, err := ScoreCases(cases, ScoreInput{}, DefaultScoreParams()); err == nil {
		t.Fatal("one case id with two fingerprints was scored")
	}
}

// RunVector is one cross-language ScoreRun fixture; Platform's Python port
// replays testdata/run_vectors.json.
type RunVector struct {
	Name     string      `json:"name"`
	Input    RunInput    `json:"input"`
	Params   ScoreParams `json:"params"`
	Expected RunScore    `json:"expected"`
}

func buildRunVectors(t *testing.T) []RunVector {
	t.Helper()
	params := DefaultScoreParams()
	params.TargetCases = 16
	rng := rand.New(rand.NewSource(77))
	seedA, seedB := ReferencePack(31, testHotkey).Refs(), ReferencePack(32, testHotkey).Refs()
	panelFor := func(cases []CaseRef) []PanelMember {
		return []PanelMember{
			member("champ", "o1", true, randomScores(rng, cases, 0.5)),
			member("p2", "o2", false, randomScores(rng, cases, 0.7)),
			member("p3", "o3", false, randomScores(rng, cases, 0.6)),
			member("p4", "o4", false, randomScores(rng, cases, 0.4)),
		}
	}
	var vs []RunVector
	add := func(name string, in RunInput) {
		got, err := ScoreRun(in, params)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		vs = append(vs, RunVector{Name: name, Input: in, Params: params, Expected: got})
	}
	add("two_fresh_seeds", RunInput{GeneratorOwnerKey: "gen", Seeds: []SeedPanel{
		{Seed: SeedFromInt64(31), Cases: seedA, Panel: panelFor(seedA)},
		{Seed: SeedFromInt64(32), Cases: seedB, Panel: panelFor(seedB)},
	}})
	// A generator that ignores its seed emits the same cases twice: the second
	// seed is all duplicates and the mean halves.
	add("seed_ignored_repeats", RunInput{GeneratorOwnerKey: "gen", Seeds: []SeedPanel{
		{Seed: SeedFromInt64(31), Cases: seedA, Panel: panelFor(seedA)},
		{Seed: SeedFromInt64(32), Cases: seedA, Panel: panelFor(seedA)},
	}})
	add("historical_claims", RunInput{GeneratorOwnerKey: "gen", ClaimedFingerprints: []string{seedA[1].Fingerprint, seedB[2].Fingerprint}, Seeds: []SeedPanel{
		{Seed: SeedFromInt64(31), Cases: seedA, Panel: panelFor(seedA)},
		{Seed: SeedFromInt64(32), Cases: seedB, Panel: panelFor(seedB)},
	}})
	short := panelFor(seedB)[:2]
	add("one_seed_insufficient", RunInput{GeneratorOwnerKey: "gen", Seeds: []SeedPanel{
		{Seed: SeedFromInt64(31), Cases: seedA, Panel: panelFor(seedA)},
		{Seed: SeedFromInt64(32), Cases: seedB, Panel: short},
	}})
	return vs
}

func TestRunVectorsGolden(t *testing.T) {
	vs := buildRunVectors(t)
	path := "testdata/run_vectors.json"
	if *update {
		b, err := json.MarshalIndent(vs, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test ./challenge -run TestRunVectorsGolden -update)", err)
	}
	var golden []RunVector
	if err := json.Unmarshal(raw, &golden); err != nil {
		t.Fatal(err)
	}
	if len(golden) != len(vs) {
		t.Fatalf("golden has %d vectors, built %d", len(golden), len(vs))
	}
	for i := range vs {
		got, _ := json.Marshal(vs[i])
		want, _ := json.Marshal(golden[i])
		if string(got) != string(want) {
			t.Errorf("run vector %q drifted; rerun with -update if intended", vs[i].Name)
		}
	}
}

func TestScoreRunSemantics(t *testing.T) {
	vs := map[string]RunVector{}
	for _, v := range buildRunVectors(t) {
		vs[v.Name] = v
	}
	fresh := vs["two_fresh_seeds"].Expected
	if fresh.Status != PackScored || fresh.Score <= 0 || len(fresh.Seeds) != 2 {
		t.Fatalf("fresh: %+v", fresh)
	}
	if want := (fresh.Seeds[0].Score + fresh.Seeds[1].Score) / 2; fresh.Score != want {
		t.Errorf("fresh mean %v want %v", fresh.Score, want)
	}
	rep := vs["seed_ignored_repeats"].Expected
	if rep.Seeds[1].Duplicate != len(rep.Seeds[1].Cases) || rep.Seeds[1].Score != 0 {
		t.Errorf("repeated seed should be all duplicates: %+v", rep.Seeds[1])
	}
	if rep.Score >= fresh.Score && fresh.Seeds[1].Score > 0 {
		t.Errorf("ignoring the seed should not beat fresh output: %v vs %v", rep.Score, fresh.Score)
	}
	if h := vs["historical_claims"].Expected; h.Seeds[0].Duplicate != 1 || h.Seeds[1].Duplicate != 1 {
		t.Errorf("historical claims: %d %d", h.Seeds[0].Duplicate, h.Seeds[1].Duplicate)
	}
	if ins := vs["one_seed_insufficient"].Expected; ins.Status != PackInsufficientPanel || ins.Score != 0 {
		t.Errorf("insufficient: %+v", ins)
	}
	if _, err := ScoreRun(RunInput{}, DefaultScoreParams()); err == nil {
		t.Error("empty run accepted")
	}
}
