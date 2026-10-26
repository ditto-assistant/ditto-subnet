package challenge

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// RoundVectors pins the round derivations Platform's Python port must
// reproduce (apps/platform/ditto/generator_rounds.py replays
// testdata/round_vectors.json) and the matrix scoring the scorer applies.
type RoundVectors struct {
	Derivations []RoundDerivationVector `json:"derivations"`
	Scores      []RoundScoreVector      `json:"scores"`
}

type RoundDerivationVector struct {
	Round         uint64            `json:"round"`
	SecretHex     string            `json:"secret_hex"`
	OpenBlockHash string            `json:"open_block_hash"`
	Commitment    string            `json:"commitment"`
	Seed          string            `json:"seed"`
	ColumnSeeds   map[string]string `json:"column_seeds"`
}

type RoundScoreVector struct {
	Name     string      `json:"name"`
	Input    RoundInput  `json:"input"`
	Params   ScoreParams `json:"params"`
	Expected RoundScore  `json:"expected"`
}

func roundPack(t *testing.T, n int64, roundSeed, columnID string) []byte {
	t.Helper()
	p := ReferencePack(n, testHotkey)
	p.Generator.Seed = RoundColumnSeed(roundSeed, columnID)
	raw, err := json.MarshalIndent(p, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestRoundSeedNeedsCommittedSecretAndOpenBlock(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	block := "0x" + strings.Repeat("ab", 32)
	seed, err := RoundSeed(7, secret, block)
	if err != nil {
		t.Fatal(err)
	}
	if !shaPattern.MatchString(seed) {
		t.Fatalf("seed %q is not lowercase hex", seed)
	}
	other, _ := RoundSeed(7, secret, "0x"+strings.Repeat("ac", 32))
	if other == seed {
		t.Fatal("seed must depend on the open block hash")
	}
	other, _ = RoundSeed(8, secret, block)
	if other == seed {
		t.Fatal("seed must depend on the round index")
	}
	if RoundCommitment(7, secret) == RoundCommitment(8, secret) {
		t.Fatal("commitment must bind the round index")
	}
	if _, err := RoundSeed(7, secret, "0x1234"); err == nil {
		t.Fatal("a short block hash must be refused")
	}
	if RoundColumnSeed(seed, "c0") == RoundColumnSeed(seed, "c1") {
		t.Fatal("columns must run on distinct seeds")
	}
}

func TestRoundDatasetRoundTrip(t *testing.T) {
	seed := SeedFromInt64(41)
	packs := map[string][]byte{
		"c1": roundPack(t, 2, seed, "c1"),
		"c0": roundPack(t, 1, seed, "c0"),
	}
	raw, sum, err := EncodeRoundDataset(3, seed, packs)
	if err != nil {
		t.Fatal(err)
	}
	again, sum2, err := EncodeRoundDataset(3, seed, packs)
	if err != nil || sum2 != sum || string(again) != string(raw) {
		t.Fatal("encoding must be canonical")
	}
	ds, cols, err := DecodeRoundDataset(raw, sum)
	if err != nil {
		t.Fatal(err)
	}
	if ds.RoundIndex != 3 || len(cols) != 2 || cols[0].ColumnID != "c0" || cols[1].ColumnID != "c1" {
		t.Fatalf("unexpected decode: %+v", ds)
	}
	refs := RoundCaseRefs("c1", cols[1].Pack)
	if !strings.HasPrefix(refs[0].CaseID, "c1/") {
		t.Fatalf("round case ids must carry the column: %q", refs[0].CaseID)
	}

	if _, _, err := DecodeRoundDataset(raw, strings.Repeat("0", 64)); err == nil {
		t.Fatal("digest mismatch must be refused")
	}
	trailing := append(append([]byte(nil), raw...), []byte(" {}")...)
	if _, _, err := DecodeRoundDataset(trailing, Digest(trailing)); err == nil {
		t.Fatal("trailing data after the dataset must be refused")
	}
	if _, _, err := EncodeRoundDataset(3, seed, map[string][]byte{"c0": roundPack(t, 1, seed, "c1")}); err == nil {
		t.Fatal("a pack generated for another column seed must be refused")
	}
	if _, _, err := EncodeRoundDataset(3, seed, map[string][]byte{"x0": packs["c0"]}); err == nil {
		t.Fatal("an invalid column id must be refused")
	}

	// A dataset rewritten to swap the two columns' packs keeps valid packs but
	// breaks the per-column seed binding.
	var tampered RoundDataset
	if err := json.Unmarshal(raw, &tampered); err != nil {
		t.Fatal(err)
	}
	tampered.Columns[0].Pack, tampered.Columns[1].Pack = tampered.Columns[1].Pack, tampered.Columns[0].Pack
	tampered.Columns[0].PackSHA256, tampered.Columns[1].PackSHA256 = tampered.Columns[1].PackSHA256, tampered.Columns[0].PackSHA256
	bad, _ := json.Marshal(tampered)
	if _, _, err := DecodeRoundDataset(bad, Digest(bad)); err == nil || !strings.Contains(err.Error(), "seed") {
		t.Fatalf("swapped columns must fail the seed binding, got %v", err)
	}
}

func roundCases(column string, n int) []CaseRef {
	out := make([]CaseRef, n)
	for i := range out {
		id := RoundCaseID(column, "k"+string(rune('a'+i)))
		out[i] = CaseRef{CaseID: id, Fingerprint: hex.EncodeToString([]byte(id))}
	}
	return out
}

func agentResults(scores map[string]float64, faults ...string) []PanelCaseResult {
	faulted := map[string]bool{}
	for _, f := range faults {
		faulted[f] = true
	}
	out := make([]PanelCaseResult, 0, len(scores))
	for _, id := range sortedKeys(scores) {
		out = append(out, PanelCaseResult{CaseID: id, Score: scores[id], Fault: faulted[id]})
	}
	return out
}

func sortedKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0 && keys[j] < keys[j-1]; j-- {
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
	return keys
}

func fill(dst map[string]float64, cases []CaseRef, s float64) map[string]float64 {
	for _, c := range cases {
		dst[c.CaseID] = s
	}
	return dst
}

func standing(t *testing.T, rs RoundScore, id string) AgentStanding {
	t.Helper()
	for _, s := range rs.Agents {
		if s.AgentID == id {
			return s
		}
	}
	t.Fatalf("agent %s missing", id)
	return AgentStanding{}
}

// handshakeRound: owner "alice" mines both sides. Her generator (c1) emits a
// family only her agent solves, and on the neutral column (c0) her agent is
// exactly as good as bob's. Pairwise-neutral ranking must not let c1 lift her
// agent above bob's, and c1 must not score as discriminating, because only
// one owner (alice, who never counts on her own column) can solve it.
func handshakeRound() RoundInput {
	c0 := roundCases("c0", 4)
	c1 := roundCases("c1", 4)
	c2 := roundCases("c2", 4)
	return RoundInput{
		Columns: []RoundColumn{
			{ColumnID: "c0", OwnerKey: "", Counted: true, Cases: c0},
			{ColumnID: "c1", OwnerKey: "alice", Counted: true, Cases: c1},
			{ColumnID: "c2", OwnerKey: "dave", Counted: false, Cases: c2},
		},
		Agents: []RoundAgent{
			{AgentID: "a-alice", OwnerKey: "alice", Champion: true,
				Results: agentResults(fill(fill(fill(map[string]float64{}, c0, 0.75), c1, 1), c2, 0))},
			{AgentID: "a-bob", OwnerKey: "bob",
				Results: agentResults(fill(fill(fill(map[string]float64{}, c0, 0.75), c1, 0), c2, 1))},
			{AgentID: "a-carol", OwnerKey: "carol",
				Results: agentResults(fill(fill(fill(map[string]float64{}, c0, 0.25), c1, 0), c2, 1))},
			{AgentID: "ref-1", OwnerKey: "subnet", Reference: true,
				Results: agentResults(fill(fill(fill(map[string]float64{}, c0, 0.5), c1, 0), c2, 0.25))},
		},
	}
}

func TestScoreRoundPairwiseNeutral(t *testing.T) {
	params := DefaultScoreParams()
	rs, err := ScoreRound(handshakeRound(), params)
	if err != nil {
		t.Fatal(err)
	}
	alice, bob := standing(t, rs, "a-alice"), standing(t, rs, "a-bob")
	if alice.PairwiseScore != bob.PairwiseScore {
		t.Fatalf("alice's own column moved her against the field: alice=%+v bob=%+v", alice, bob)
	}
	if alice.CommonCases != 4 || bob.CommonCases != 4 || alice.CommonMean != bob.CommonMean {
		t.Fatalf("the tie-break must use only columns no panel owner owns: alice=%+v bob=%+v", alice, bob)
	}
	if standing(t, rs, "a-carol").Rank != 4 {
		t.Fatalf("carol is worst on every neutral case: %+v", rs.Agents)
	}
	for _, c := range rs.Columns {
		switch c.ColumnID {
		case "c1":
			if c.Pack.Discriminating != 0 || c.Pack.Unsolved != 4 || c.Pack.Score != 0 {
				t.Fatalf("handshake column must earn nothing: %+v", c.Pack)
			}
		case "c2":
			// The challenger column is scored as a generator even though no
			// agent comparison counts it: bob and carol solve, the reference
			// and alice do not.
			if c.Pack.Discriminating != 4 || c.Pack.Score <= 0 {
				t.Fatalf("challenger column must score: %+v", c.Pack)
			}
		}
	}
}

func TestScoreRoundReferenceAgentsAreIndependentSolvers(t *testing.T) {
	c0 := roundCases("c0", 2)
	in := RoundInput{
		Columns: []RoundColumn{{ColumnID: "c0", Counted: true, Cases: c0}},
		Agents: []RoundAgent{
			{AgentID: "a1", OwnerKey: "x", Results: agentResults(fill(map[string]float64{}, c0, 1))},
			{AgentID: "a2", OwnerKey: "x", Results: agentResults(fill(map[string]float64{}, c0, 0))},
			{AgentID: "a3", OwnerKey: "y", Results: agentResults(fill(map[string]float64{}, c0, 0))},
			{AgentID: "r1", OwnerKey: "subnet", Reference: true, Results: agentResults(fill(map[string]float64{}, c0, 1))},
			{AgentID: "r2", OwnerKey: "subnet", Reference: true, Results: agentResults(fill(map[string]float64{}, c0, 0))},
		},
	}
	params := DefaultScoreParams()
	rs, err := ScoreRound(in, params)
	if err != nil {
		t.Fatal(err)
	}
	if got := rs.Columns[0].Pack.Discriminating; got != 2 {
		t.Fatalf("owner x plus reference r1 are two solver owners; got %d discriminating", got)
	}
	in.Agents[3].Reference = false // r1 now shares owner "subnet" with r2 only
	in.Agents[3].OwnerKey = "x"
	rs, _ = ScoreRound(in, params)
	if got := rs.Columns[0].Pack.Unsolved; got != 2 {
		t.Fatalf("without the reference flag only owner x solves; got %+v", rs.Columns[0].Pack)
	}
}

func TestScoreRoundFaultsAndClaims(t *testing.T) {
	c0 := roundCases("c0", 3)
	dup := []CaseRef{c0[0], {CaseID: "c1/kz", Fingerprint: "fresh"}}
	in := RoundInput{
		Columns: []RoundColumn{
			{ColumnID: "c0", Counted: true, Cases: c0},
			{ColumnID: "c1", OwnerKey: "o9", Counted: true, Cases: dup},
		},
		Agents: []RoundAgent{
			{AgentID: "a", OwnerKey: "o1", Results: agentResults(map[string]float64{c0[0].CaseID: 1, c0[1].CaseID: 1, c0[2].CaseID: 0, "c1/kz": 1}, c0[2].CaseID)},
			{AgentID: "b", OwnerKey: "o2", Results: agentResults(map[string]float64{c0[0].CaseID: 0, c0[1].CaseID: 1, c0[2].CaseID: 1, "c1/kz": 0})},
		},
	}
	params := DefaultScoreParams()
	params.MinPanel = 2
	rs, err := ScoreRound(in, params)
	if err != nil {
		t.Fatal(err)
	}
	if rs.Columns[1].Pack.Duplicate != 1 {
		t.Fatalf("a later column repeating an earlier fingerprint is a duplicate: %+v", rs.Columns[1].Pack)
	}
	a := standing(t, rs, "a")
	// c0[2] is faulted for a and excluded; c1's owner has no agent on the
	// panel, so both its cases are common: 2 + 2.
	if a.CommonCases != 4 {
		t.Fatalf("faulted cases are excluded: %+v", a)
	}
	if _, err := ScoreRound(RoundInput{Agents: []RoundAgent{{AgentID: "x"}, {AgentID: "x"}}}, params); err == nil {
		t.Fatal("duplicate agents must be refused")
	}
	dupResults := RoundInput{Agents: []RoundAgent{{AgentID: "x", OwnerKey: "o", Results: []PanelCaseResult{
		{CaseID: "c0/a", Score: 1}, {CaseID: "c0/a", Score: 0},
	}}}}
	if _, err := ScoreRound(dupResults, params); err == nil {
		t.Fatal("duplicate case results must be refused")
	}
}

func buildRoundVectors(t *testing.T) RoundVectors {
	t.Helper()
	var v RoundVectors
	for i, block := range []string{"0x" + strings.Repeat("11", 32), strings.Repeat("ab", 32)} {
		secret := []byte(strings.Repeat(string(rune('a'+i)), 32))
		round := uint64(1000 + i)
		seed, err := RoundSeed(round, secret, block)
		if err != nil {
			t.Fatal(err)
		}
		cols := map[string]string{}
		for _, id := range []string{"c0", "c1", "c5"} {
			cols[id] = RoundColumnSeed(seed, id)
		}
		v.Derivations = append(v.Derivations, RoundDerivationVector{
			Round: round, SecretHex: hex.EncodeToString(secret), OpenBlockHash: block,
			Commitment: RoundCommitment(round, secret), Seed: seed, ColumnSeeds: cols,
		})
	}
	params := DefaultScoreParams()
	add := func(name string, in RoundInput, p ScoreParams) {
		got, err := ScoreRound(in, p)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		v.Scores = append(v.Scores, RoundScoreVector{Name: name, Input: in, Params: p, Expected: got})
	}
	add("handshake", handshakeRound(), params)
	return v
}

func TestRoundVectorsGolden(t *testing.T) {
	got := buildRoundVectors(t)
	path := "testdata/round_vectors.json"
	if *update {
		raw, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(raw, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test -run TestRoundVectorsGolden -update)", err)
	}
	var want RoundVectors
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	gotRaw, _ := json.Marshal(got)
	wantRaw, _ := json.Marshal(want)
	if string(gotRaw) != string(wantRaw) {
		t.Fatal("round vectors drifted; re-run with -update and review the diff")
	}
}

func TestCommonMeanUsesOneCaseSetForEveryAgent(t *testing.T) {
	params := DefaultScoreParams()
	params.MinPanel = 2
	params.MinSolverOwners = 1
	in := RoundInput{
		Columns: []RoundColumn{{ColumnID: "c0", Counted: true, Cases: []CaseRef{
			{CaseID: "c0/a", Fingerprint: "fa"}, {CaseID: "c0/b", Fingerprint: "fb"},
		}}},
		Agents: []RoundAgent{
			// x faults on b, so b is not common: both means use only a.
			{AgentID: "x", OwnerKey: "o1", Results: []PanelCaseResult{{CaseID: "c0/a", Score: 1}, {CaseID: "c0/b", Fault: true}}},
			{AgentID: "y", OwnerKey: "o2", Results: []PanelCaseResult{{CaseID: "c0/a", Score: 1}, {CaseID: "c0/b", Score: 0}}},
		},
	}
	got, err := ScoreRound(in, params)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range got.Agents {
		if a.CommonCases != 1 || a.CommonMean != 1 {
			t.Fatalf("agent %s: common %d mean %v", a.AgentID, a.CommonCases, a.CommonMean)
		}
	}
}
