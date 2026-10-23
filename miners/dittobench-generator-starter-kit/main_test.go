package main

import (
	"encoding/json"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func TestEverySeedValidates(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for _, per := range []int{2, 4, 6} { // 6 is the CLI maximum
			r := challenge.Validate(Generate(seed, testHotkey, per))
			if !r.Valid {
				b, _ := json.MarshalIndent(r.Issues, "", "  ")
				t.Fatalf("seed %d per-family %d invalid:\n%s", seed, per, b)
			}
		}
	}
}

func TestGenerateIsDeterministic(t *testing.T) {
	a, _ := json.Marshal(Generate(9, testHotkey, 4))
	b, _ := json.Marshal(Generate(9, testHotkey, 4))
	c, _ := json.Marshal(Generate(10, testHotkey, 4))
	if string(a) != string(b) || string(a) == string(c) {
		t.Fatal("generation must be a pure function of the seed")
	}
}

// Every case question must be unique within a pack: two cases asking the same
// question about the same memory have no single right answer.
func TestQuestionsAreUnique(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		seen := map[string]string{}
		for _, c := range Generate(seed, testHotkey, 6).Cases {
			if prior, dup := seen[c.Question]; dup {
				t.Fatalf("seed %d: %s and %s both ask %q", seed, prior, c.ID, c.Question)
			}
			seen[c.Question] = c.ID
		}
	}
}
