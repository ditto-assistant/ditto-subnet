package challenge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"
)

// SeasonVectors pins the dueling season derivations the Python mirror
// (ditto_screening_protocol.dueling, packages/ditto-screening-protocol/tests)
// must reproduce byte for byte.
type SeasonVectors struct {
	Seasons []SeasonVector `json:"seasons"`
}

type SeasonVector struct {
	SeasonIndex     uint64            `json:"season_index"`
	SecretHex       string            `json:"secret_hex"`
	AnchorBlockHash string            `json:"anchor_block_hash"`
	Commitment      string            `json:"commitment"`
	SeasonSeed      string            `json:"season_seed"`
	SliceSeeds      []SliceSeedVector `json:"slice_seeds"`
}

type SliceSeedVector struct {
	RunSeed   int64  `json:"run_seed"`
	ColumnID  string `json:"column_id"`
	SliceSeed string `json:"slice_seed"`
}

func testSeasonSecret(label string) []byte {
	sum := sha256.Sum256([]byte("season-secret:" + label))
	return sum[:]
}

func buildSeasonVectors(t *testing.T) SeasonVectors {
	t.Helper()
	inputs := []struct {
		season uint64
		label  string
		block  string
	}{
		{0, "zero", "0x" + strings.Repeat("00", 32)},
		{26119, "first", "0x" + strings.Repeat("5a", 32)},
		// Upper case and no prefix normalize to the same block bytes.
		{26120, "second", strings.Repeat("AB", 16) + strings.Repeat("cd", 16)},
		{math.MaxUint64, "max", "0X" + strings.Repeat("ff", 32)},
	}
	runs := []int64{0, 123, 987654321012, math.MaxInt64}
	var out SeasonVectors
	for _, in := range inputs {
		secret := testSeasonSecret(in.label)
		commit, err := SeasonCommitment(in.season, secret)
		if err != nil {
			t.Fatal(err)
		}
		seed, err := SeasonSeed(in.season, secret, in.block)
		if err != nil {
			t.Fatal(err)
		}
		v := SeasonVector{
			SeasonIndex: in.season, SecretHex: hex.EncodeToString(secret), AnchorBlockHash: in.block,
			Commitment: commit, SeasonSeed: seed,
		}
		for _, run := range runs {
			for _, col := range []string{"c0", "c4"} {
				s, err := SliceSeed(seed, run, col)
				if err != nil {
					t.Fatal(err)
				}
				v.SliceSeeds = append(v.SliceSeeds, SliceSeedVector{RunSeed: run, ColumnID: col, SliceSeed: s})
			}
		}
		out.Seasons = append(out.Seasons, v)
	}
	return out
}

func TestSeasonVectorsGolden(t *testing.T) {
	got := buildSeasonVectors(t)
	path := "testdata/season_vectors.json"
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
		t.Fatalf("%v (run go test ./challenge -run TestSeasonVectorsGolden -update)", err)
	}
	var want SeasonVectors
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	gotRaw, _ := json.Marshal(got)
	wantRaw, _ := json.Marshal(want)
	if string(gotRaw) != string(wantRaw) {
		t.Fatal("season vectors drifted; re-run with -update and review the diff")
	}
}

func TestSeasonDerivationsBindEveryInput(t *testing.T) {
	secret := testSeasonSecret("bind")
	block := "0x" + strings.Repeat("ab", 32)
	seed, err := SeasonSeed(7, secret, block)
	if err != nil {
		t.Fatal(err)
	}
	if !shaPattern.MatchString(seed) {
		t.Fatalf("season seed %q is not lowercase hex", seed)
	}
	if other, _ := SeasonSeed(8, secret, block); other == seed {
		t.Fatal("season seed must bind the season index")
	}
	if other, _ := SeasonSeed(7, secret, "0x"+strings.Repeat("ac", 32)); other == seed {
		t.Fatal("season seed must bind the anchor block")
	}
	if other, _ := SeasonSeed(7, testSeasonSecret("other"), block); other == seed {
		t.Fatal("season seed must bind the secret")
	}
	a, _ := SeasonCommitment(7, secret)
	b, _ := SeasonCommitment(8, secret)
	if a == b {
		t.Fatal("commitment must bind the season index")
	}
	// Domain separation: a season never reuses a round value.
	roundSeed, _ := RoundSeed(7, secret, block)
	if a == RoundCommitment(7, secret) || seed == roundSeed {
		t.Fatal("season derivations must not collide with round derivations")
	}
	s0, _ := SliceSeed(seed, 1, "c0")
	s1, _ := SliceSeed(seed, 1, "c1")
	s2, _ := SliceSeed(seed, 2, "c0")
	if s0 == s1 || s0 == s2 || s0 == RoundColumnSeed(seed, "c0") {
		t.Fatal("slice seeds must bind the run seed and the column, under their own domain")
	}
}

func TestSeasonDerivationsRefuseBadInputs(t *testing.T) {
	block := "0x" + strings.Repeat("ab", 32)
	for _, secret := range [][]byte{nil, make([]byte, 31), make([]byte, 33)} {
		if _, err := SeasonCommitment(1, secret); err == nil {
			t.Fatalf("a %d-byte secret must be refused by the commitment", len(secret))
		}
		if _, err := SeasonSeed(1, secret, block); err == nil {
			t.Fatalf("a %d-byte secret must be refused by the seed", len(secret))
		}
	}
	secret := testSeasonSecret("bad")
	for _, bad := range []string{"", "0x1234", "0x" + strings.Repeat("zz", 32), strings.Repeat("ab", 33)} {
		if _, err := SeasonSeed(1, secret, bad); err == nil {
			t.Fatalf("anchor block hash %q must be refused", bad)
		}
	}
	seed, _ := SeasonSeed(1, secret, block)
	if _, err := SliceSeed(seed, -1, "c0"); err == nil {
		t.Fatal("a negative run seed must be refused")
	}
	if _, err := SliceSeed(seed, 1, ""); err == nil {
		t.Fatal("an empty column id must be refused")
	}
	for _, bad := range []string{"", strings.ToUpper(seed), seed[:63], "0x" + seed[2:]} {
		if _, err := SliceSeed(bad, 1, "c0"); err == nil {
			t.Fatalf("season seed %q must be refused", bad)
		}
	}
}
