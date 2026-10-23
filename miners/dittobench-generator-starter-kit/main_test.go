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

func TestEverySeedValidates(t *testing.T) {
	for seed := int64(1); seed <= 4; seed++ {
		for cases := challenge.MinCases; cases <= MaxCases; cases++ {
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
