package challenge

import (
	"encoding/json"
	"fmt"
	"math/rand"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// widePack builds a valid pack of n update-chain cases, each about its own
// numbered code, so a test can reach any case count the contract allows.
func widePack(t *testing.T, seed int64, n int) Pack {
	t.Helper()
	b := &refBuilder{rng: rand.New(rand.NewSource(seed)), waves: make([]protocol.SeedRequest, 3)}
	for i := range b.waves {
		b.waves[i].Wave = i
	}
	cases := make([]Case, 0, n)
	for i := 0; i < n; i++ {
		cases = append(cases, b.codeUpdate(i, fmt.Sprintf("locker %d code", i), fmt.Sprintf("garage keypad %d", i)))
	}
	return Pack{
		Contract:            ContractV1,
		GradingBenchVersion: GradingBenchVersion,
		MinerHotkey:         testHotkey,
		Generator:           Generator{Name: "wide-test", Version: "1", Seed: SeedFromInt64(seed)},
		MemoryWaves:         b.waves,
		Cases:               cases,
	}
}

// MaxCases is the most cases_per_seed Platform can set (80), so a pack, a
// request, and the score's target must all accept it, and nothing above it.
func TestPackContractAcceptsMaxCasesAndNoMore(t *testing.T) {
	t.Parallel()
	if MaxCases != 80 {
		t.Fatalf("MaxCases = %d; Platform's MAX_CASES_PER_SEED and the starter kit pin 80", MaxCases)
	}
	if err := NewGenerateRequest(SeedFromInt64(1), testHotkey, MaxCases).Validate(); err != nil {
		t.Fatalf("request at MaxCases refused: %v", err)
	}
	full := widePack(t, 7, MaxCases)
	if r := Validate(full); !r.Valid || r.CaseCount != MaxCases {
		b, _ := json.MarshalIndent(r.Issues, "", "  ")
		t.Fatalf("pack at MaxCases invalid (cases %d):\n%s", r.CaseCount, b)
	}
	over := widePack(t, 7, MaxCases+1)
	if r := Validate(over); r.Valid || !hasRule(r, "", "case_count") {
		t.Fatalf("pack over MaxCases must fail case_count: %+v", r.Issues)
	}
	params := DefaultScoreParams()
	params.TargetCases = MaxCases
	if err := params.Validate(); err != nil {
		t.Fatalf("target_cases at MaxCases refused: %v", err)
	}
	params.TargetCases = MaxCases + 1
	if params.Validate() == nil {
		t.Fatal("target_cases over MaxCases accepted")
	}
}

// At the cap the denominator is max(n, target_cases) = n: a full pack of
// discriminating cases scores its mean credit over (1 + champion_bonus).
func TestScoreCasesAtMaxCases(t *testing.T) {
	cases := widePack(t, 3, MaxCases).Refs()
	in := ScoreInput{GeneratorOwnerKey: "gen", Panel: []PanelMember{
		member("a", "o1", true, uniform(cases, 1)),
		member("b", "o2", false, uniform(cases, 1)),
		member("c", "o3", false, uniform(cases, 0)),
	}}
	params := DefaultScoreParams()
	params.TargetCases = MaxCases
	got, err := ScoreCases(cases, in, params)
	if err != nil {
		t.Fatal(err)
	}
	// Every case: solved by two of three owners, mean 2/3, champion solved.
	// credit 1/3 each; score = (80/3) / (1.5 * 80) = 2/9.
	if got.Discriminating != MaxCases || got.Unsolved != 0 {
		t.Fatalf("verdicts: %+v", got)
	}
	if want := 2.0 / 9.0; got.Score < want-1e-12 || got.Score > want+1e-12 {
		t.Fatalf("score %v, want %v", got.Score, want)
	}
	// The same pack scored against a smaller target is not inflated past n.
	params.TargetCases = 16
	small, err := ScoreCases(cases, in, params)
	if err != nil {
		t.Fatal(err)
	}
	if small.Score != got.Score {
		t.Fatalf("a target below n must not change the score: %v vs %v", small.Score, got.Score)
	}
}

// A round at the cap (MaxRoundColumns columns of MaxCases cases) must encode
// within MaxRoundDatasetBytes and decode with every case intact, so a
// validator can run it.
func TestRoundDatasetAtMaxCases(t *testing.T) {
	t.Parallel()
	seed := SeedFromInt64(77)
	packs := map[string][]byte{}
	for i := 0; i < MaxRoundColumns; i++ {
		id := fmt.Sprintf("c%d", i)
		p := widePack(t, int64(100+i), MaxCases)
		p.Generator.Seed = RoundColumnSeed(seed, id)
		raw, err := json.Marshal(p)
		if err != nil {
			t.Fatal(err)
		}
		packs[id] = raw
	}
	raw, sum, err := EncodeRoundDataset(5, seed, packs)
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) > MaxRoundDatasetBytes/4 {
		t.Fatalf("a full round of update chains is %d bytes; expected far below the %d byte cap", len(raw), MaxRoundDatasetBytes)
	}
	_, cols, err := DecodeRoundDataset(raw, sum)
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	for _, c := range cols {
		total += len(c.Pack.Cases)
	}
	if len(cols) != MaxRoundColumns || total != MaxRoundColumns*MaxCases {
		t.Fatalf("decoded %d columns, %d cases", len(cols), total)
	}
}
