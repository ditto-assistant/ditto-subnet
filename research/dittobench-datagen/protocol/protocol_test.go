package protocol

import (
	"encoding/json"
	"testing"
)

func TestRunRequestBenchVersionIsAdditiveForV7Only(t *testing.T) {
	legacy, err := json.Marshal(RunRequest{CaseID: "v6", BenchVersion: 0})
	if err != nil {
		t.Fatal(err)
	}
	var legacyObject map[string]any
	if err := json.Unmarshal(legacy, &legacyObject); err != nil {
		t.Fatal(err)
	}
	if _, present := legacyObject["bench_version"]; present {
		t.Fatal("legacy v2-v6 request must omit bench_version")
	}

	v7, err := json.Marshal(RunRequest{CaseID: "v7", BenchVersion: BenchVersionV7})
	if err != nil {
		t.Fatal(err)
	}
	var v7Object map[string]any
	if err := json.Unmarshal(v7, &v7Object); err != nil {
		t.Fatal(err)
	}
	if got := v7Object["bench_version"]; got != float64(BenchVersionV7) {
		t.Fatalf("v7 bench_version = %v, want %d", got, BenchVersionV7)
	}
}
