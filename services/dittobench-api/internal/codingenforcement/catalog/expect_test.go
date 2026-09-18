package catalog

import (
	"encoding/json"
	"errors"
	"os"
	"testing"
)

// vectorsFile is shared with the Python evidence tool's tests.
const vectorsFile = "testdata/expectation-vectors-v1.json"

func TestExpectationVectorsAgreeWithPython(t *testing.T) {
	raw, err := os.ReadFile(vectorsFile)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := Decode(raw)
	if err != nil {
		t.Fatal(err)
	}
	document := decoded.(map[string]any)
	if document["schema"] != "dittobench-coding-native-enforcement-expectation-vectors-v1" {
		t.Fatalf("schema = %v", document["schema"])
	}
	ids := document["subordinate_ids"].(map[string]any)
	uidStart, _ := nonNegative(ids["uid_start"])
	uidCount, _ := nonNegative(ids["uid_count"])
	gidStart, _ := nonNegative(ids["gid_start"])
	gidCount, _ := nonNegative(ids["gid_count"])
	subordinate := SubordinateIDs{UIDStart: uidStart, UIDCount: uidCount, GIDStart: gidStart, GIDCount: gidCount}
	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, item := range document["vectors"].([]any) {
		vector := item.(map[string]any)
		encoded, err := Canonical(vector["expect"])
		if err != nil {
			t.Fatal(err)
		}
		var expect Expectation
		if err := json.Unmarshal(encoded, &expect); err != nil {
			t.Fatalf("%s: %v", vector["name"], err)
		}
		if err := expect.validate(loaded.OutcomeSet()); err != nil {
			t.Fatalf("%s: %v", vector["name"], err)
		}
		matched, err := Evaluate(expect, vector["observed"], subordinate, loaded.OutcomeSet(), loaded.Tolerances)
		result := "unmatched"
		switch {
		case err != nil:
			if !errors.Is(err, ErrObservedShape) {
				t.Fatalf("%s: unexpected error %v", vector["name"], err)
			}
			result = "invalid"
		case matched:
			result = "matched"
		}
		if result != vector["result"] {
			t.Errorf("%s: result = %s, want %v", vector["name"], result, vector["result"])
		}
		seen[result] = true
	}
	if len(seen) != 3 {
		t.Fatalf("vectors cover %v", seen)
	}
}
