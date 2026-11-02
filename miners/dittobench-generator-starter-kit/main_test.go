package main

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func request(seed int64, cases int) challenge.GenerateRequest {
	return challenge.NewGenerateRequest(challenge.SeedFromInt64(seed), testHotkey, cases)
}

// Validation grades a full-memory dump per case, so its cost grows with the
// square of the pack size (about 5 s for an 80-case pack on one core). The
// sweep covers every small size on one seed and the sizes near each pool
// boundary and the cap on several.
func TestEverySeedValidates(t *testing.T) {
	type sweep struct {
		seeds []int64
		sizes []int
	}
	var small []int
	for cases := challenge.MinCases; cases <= 32; cases++ {
		small = append(small, cases)
	}
	sweeps := []sweep{
		{seeds: []int64{1}, sizes: small},
		{seeds: []int64{1, 2, 3}, sizes: []int{33, 48, 64, 70, MaxCases - 1, MaxCases}},
	}
	for _, s := range sweeps {
		for _, seed := range s.seeds {
			for _, cases := range s.sizes {
				req := request(seed, cases)
				pack, err := Generate(req)
				if err != nil {
					t.Fatal(err)
				}
				raw, _ := json.Marshal(pack)
				if r := challenge.ValidateGenerated(raw, req); !r.Valid {
					b, _ := json.MarshalIndent(r.Issues, "", "  ")
					t.Fatalf("seed %d cases %d invalid:\n%s", seed, cases, b)
				}
			}
		}
	}
}

// Generation itself is cheap; every seed must fill every size without
// exhausting a pool (Builder.Pick panics when one runs dry).
func TestEverySizeFills(t *testing.T) {
	for seed := int64(1); seed <= 16; seed++ {
		for cases := challenge.MinCases; cases <= MaxCases; cases++ {
			pack, err := Generate(request(seed, cases))
			if err != nil {
				t.Fatal(err)
			}
			if len(pack.Cases) != cases {
				t.Fatalf("seed %d: asked for %d cases, got %d", seed, cases, len(pack.Cases))
			}
		}
	}
}

// The kit must fill every pack size the contract allows, so the operator can
// raise cases_per_seed to the cap without the public default generator
// dropping its columns.
func TestMaxCasesMatchesTheContract(t *testing.T) {
	if MaxCases != challenge.MaxCases {
		t.Fatalf("starter kit fills %d cases; the contract allows %d", MaxCases, challenge.MaxCases)
	}
}

func TestTimestampsAdvanceWaveByWave(t *testing.T) {
	pack, err := Generate(request(3, MaxCases))
	if err != nil {
		t.Fatal(err)
	}
	last := ""
	for _, wave := range pack.MemoryWaves {
		for _, pair := range wave.Pairs {
			// RFC 3339 UTC timestamps order lexically.
			if pair.Timestamp <= last {
				t.Fatalf("wave %d pair %s at %s is not after %s", wave.Wave, pair.PairID, pair.Timestamp, last)
			}
			last = pair.Timestamp
		}
	}
}

func TestRunIsAPureFunctionOfTheRequest(t *testing.T) {
	emit := func(req challenge.GenerateRequest) []byte {
		var out bytes.Buffer
		if err := run(bytes.NewReader(req.Bytes()), &out); err != nil {
			t.Fatal(err)
		}
		return out.Bytes()
	}
	a, b, c := emit(request(9, 16)), emit(request(9, 16)), emit(request(10, 16))
	if !bytes.Equal(a, b) || bytes.Equal(a, c) {
		t.Fatal("output must be a pure function of the request")
	}
}

func TestRejectsUnfillableRequests(t *testing.T) {
	if _, err := Generate(request(1, MaxCases+1)); err == nil {
		t.Fatal("oversized request accepted")
	}
	bad := request(1, 16)
	bad.Contract = "nope"
	if _, err := Generate(bad); err == nil {
		t.Fatal("bad contract accepted")
	}
}

// Every case question must be unique within a pack: two cases asking the same
// question about the same memory have no single right answer.
func TestQuestionsAreUnique(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		pack, err := Generate(request(seed, MaxCases))
		if err != nil {
			t.Fatal(err)
		}
		seen := map[string]string{}
		for _, c := range pack.Cases {
			if prior, dup := seen[c.Question]; dup {
				t.Fatalf("seed %d: %s and %s both ask %q", seed, prior, c.ID, c.Question)
			}
			seen[c.Question] = c.ID
		}
	}
}

// A v13 request yields exactly the version 3 pack: the same cases, haystack,
// and grading policy. Only the provenance version string moved. The digests
// were recorded from version 3 of the kit.
func TestV13OutputIsUnchanged(t *testing.T) {
	for _, c := range []struct {
		seed   int64
		cases  int
		digest string
	}{
		{1, 80, "8c60ecb0dd50e6bf7b367a90a820beeafd91def266c58e1eebc2cb0de9353bea"},
		{2, 16, "9b4b71cb06c1a3fdb07c5988650d1b11515d063bd5d09fee5988ab660990f6f0"},
		{3, 47, "702858fcfc38d6b20fd7378df68b69116b4521682f0991da2d4f4debf75f4336"},
	} {
		var out bytes.Buffer
		if err := run(bytes.NewReader(request(c.seed, c.cases).Bytes()), &out); err != nil {
			t.Fatal(err)
		}
		v3 := bytes.Replace(out.Bytes(), []byte(`"version":"4"`), []byte(`"version":"3"`), 1)
		if got := challenge.Digest(v3); got != c.digest {
			t.Fatalf("seed %d cases %d: v13 pack digest %s, want the version 3 pack %s", c.seed, c.cases, got, c.digest)
		}
	}
}

// A v14 round's request gets a v14 pack that passes the v14 rules, with the
// same cases as the v13 pack for the same seed.
func TestV14RequestDeclaresV14(t *testing.T) {
	for _, seed := range []int64{1, 2, 3} {
		for _, cases := range []int{challenge.MinCases, 33, MaxCases} {
			req := challenge.NewGenerateRequestFor(challenge.SeedFromInt64(seed), testHotkey, cases, challenge.GradingBenchVersionV14)
			pack, err := Generate(req)
			if err != nil {
				t.Fatal(err)
			}
			if pack.GradingBenchVersion != challenge.GradingBenchVersionV14 {
				t.Fatalf("seed %d: declared v%d", seed, pack.GradingBenchVersion)
			}
			raw, _ := json.Marshal(pack)
			if r := challenge.ValidateGenerated(raw, req); !r.Valid {
				b, _ := json.MarshalIndent(r.Issues, "", "  ")
				t.Fatalf("seed %d cases %d invalid at v14:\n%s", seed, cases, b)
			}
			legacy, err := Generate(request(seed, cases))
			if err != nil {
				t.Fatal(err)
			}
			legacy.GradingBenchVersion = pack.GradingBenchVersion
			again, _ := json.Marshal(legacy)
			if !bytes.Equal(raw, again) {
				t.Fatalf("seed %d cases %d: v14 pack differs from the v13 pack beyond its policy", seed, cases)
			}
		}
	}
}
