package main

import (
	"bytes"
	"strings"
	"testing"
)

const evidence = "../../internal/familygate/testdata/sn351_evidence.json"

func TestRunPrintsTheTable(t *testing.T) {
	var out bytes.Buffer
	if err := run([]string{"-in", evidence}, nil, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "| revert-to-original | 3 |") {
		t.Fatalf("table:\n%s", out.String())
	}
}

func TestRunRequireFailsOnAHeldFamily(t *testing.T) {
	var out bytes.Buffer
	if err := run([]string{"-in", evidence, "-require", "revert-to-original"}, nil, &out); err != nil {
		t.Fatalf("admitted family refused: %v", err)
	}
	err := run([]string{"-in", evidence, "-require", "revert-to-original,conditional,missing"}, nil, &out)
	if err == nil || !strings.Contains(err.Error(), "conditional (hold-unsolved)") || !strings.Contains(err.Error(), "missing (no rows)") {
		t.Fatalf("require: %v", err)
	}
}

func TestRunReadsStdinAsJSON(t *testing.T) {
	var out bytes.Buffer
	in := strings.NewReader(`[{"round":1,"family":"x","status":"trivial"}]`)
	if err := run([]string{"-json"}, in, &out); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), `"verdict": "hold-trivial"`) {
		t.Fatalf("json:\n%s", out.String())
	}
	if err := run(nil, strings.NewReader(`[{"round":1,"family":"x","bogus":1}]`), &out); err == nil {
		t.Fatal("accepted an unknown field")
	}
}
