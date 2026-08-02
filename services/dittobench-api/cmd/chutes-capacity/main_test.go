package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAnalyzeUsesRepeatedConservativeSnapshots(t *testing.T) {
	t.Parallel()
	directory := t.TempDir()
	first := filepath.Join(directory, "first.json")
	second := filepath.Join(directory, "second.json")
	writeFixture(t, first, `[
  {"name":"roomy","timestamp":"t1","active_instance_count":20,"total_instance_count":20,"scalable":false,"scale_allowance":0,"utilization_current":0.1,"utilization_5m":0.2,"utilization_1h":0.3,"total_requests_1h":1000,"completed_requests_1h":1000,"rate_limited_requests_1h":0},
  {"name":"spiky","timestamp":"t1","active_instance_count":25,"total_instance_count":25,"scalable":true,"scale_allowance":5,"utilization_current":0.95,"utilization_5m":0.8,"utilization_1h":0.5,"total_requests_1h":2000,"completed_requests_1h":1500,"rate_limited_requests_1h":500}
]`)
	writeFixture(t, second, `[
  {"name":"roomy","timestamp":"t2","active_instance_count":20,"total_instance_count":20,"scalable":false,"scale_allowance":0,"utilization_current":0.2,"utilization_5m":0.2,"utilization_1h":0.3,"total_requests_1h":1100,"completed_requests_1h":1100,"rate_limited_requests_1h":0},
  {"name":"spiky","timestamp":"t2","active_instance_count":25,"total_instance_count":25,"scalable":true,"scale_allowance":5,"utilization_current":0.9,"utilization_5m":0.85,"utilization_1h":0.6,"total_requests_1h":2000,"completed_requests_1h":1600,"rate_limited_requests_1h":400}
]`)
	rep, err := analyze([]string{first, second}, []string{"roomy", "spiky"})
	if err != nil {
		t.Fatal(err)
	}
	if rep.Candidates[0].Model != "roomy" {
		t.Fatalf("ranked first = %q", rep.Candidates[0].Model)
	}
	if rep.Candidates[0].ConservativeUtilization != .3 {
		t.Fatalf("roomy utilization = %f", rep.Candidates[0].ConservativeUtilization)
	}
}

func TestAnalyzeRequiresEveryModelInEverySnapshot(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "snapshot.json")
	writeFixture(t, path, `[{"name":"present"}]`)
	if _, err := analyze([]string{path}, []string{"missing"}); err == nil {
		t.Fatal("expected missing model error")
	}
}

func TestAnalyzeRejectsDuplicateRowsAndSelectedModels(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "snapshot.json")
	writeFixture(t, path, `[{"name":"duplicate"},{"name":"duplicate"}]`)
	if _, err := analyze([]string{path}, []string{"duplicate"}); err == nil {
		t.Fatal("expected duplicate row error")
	}
	if _, err := analyze([]string{path}, []string{"duplicate", "duplicate"}); err == nil {
		t.Fatal("expected duplicate model error")
	}
}

func TestSummarizeDoesNotAssumeIdleTrafficCompleted(t *testing.T) {
	t.Parallel()
	candidate := summarize("idle", []snapshotRow{{Name: "idle", Scalable: true}})
	if candidate.MedianCompletionRatio1H != 0 || !candidate.Scalable {
		t.Fatalf("candidate = %#v", candidate)
	}
}

func writeFixture(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}
