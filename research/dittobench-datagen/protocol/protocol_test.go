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

func TestScoreReportZeroCompositeStderrUsesHistoricalOmitEmptyShape(t *testing.T) {
	reportJSON, err := json.Marshal(ScoreReport{
		RunID: "v9-zero", GeneratedAt: "2026-08-11T00:00:00Z",
		Composite: 0, CompositeStderr: 0, PerCase: []CaseScore{},
	})
	if err != nil {
		t.Fatal(err)
	}
	var reportObject map[string]any
	if err := json.Unmarshal(reportJSON, &reportObject); err != nil {
		t.Fatal(err)
	}
	if _, present := reportObject["composite_stderr"]; present {
		t.Fatal("zero composite_stderr must preserve the historical omitempty shape")
	}
}

// TestRunDetailsMemoryOverCallIsAdditive pins the issue #533 field: a report
// without the factor keeps its historical bytes, and a neutral 1.0 is still
// published (a pointer, so an applied factor is never confused with absence).
func TestRunDetailsMemoryOverCallIsAdditive(t *testing.T) {
	legacy, err := json.Marshal(RunDetails{BenchVersion: BenchVersionV14})
	if err != nil {
		t.Fatal(err)
	}
	if string(legacy) != `{"bench_version":14,"tool_mean":0,"memory_mean":0}` {
		t.Fatalf("details without memory_over_call changed shape: %s", legacy)
	}
	for _, factor := range []float64{1.0, 0.875} {
		value := factor
		body, err := json.Marshal(RunDetails{BenchVersion: BenchVersionV14, MemoryOverCall: &value})
		if err != nil {
			t.Fatal(err)
		}
		var decoded RunDetails
		if err := json.Unmarshal(body, &decoded); err != nil {
			t.Fatal(err)
		}
		if decoded.MemoryOverCall == nil || *decoded.MemoryOverCall != factor {
			t.Fatalf("memory_over_call %v did not round-trip: %s", factor, body)
		}
	}
}
