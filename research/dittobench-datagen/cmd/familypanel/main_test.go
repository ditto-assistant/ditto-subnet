package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestPlanPrintsTheCanaryDatasetAndItsFamilies(t *testing.T) {
	var out bytes.Buffer
	if err := run([]string{"-plan", "-seeds", "4081349795126678977"}, &out); err != nil {
		t.Fatal(err)
	}
	line := out.String()
	for _, want := range []string{"4134b18afccc98691480ec750321ed728e9434a478f77080da463b922283d843", "owner-after-correction", "standing-status"} {
		if !strings.Contains(line, want) {
			t.Errorf("plan output %q lacks %q", line, want)
		}
	}
}

func TestRunNeedsInput(t *testing.T) {
	if err := run(nil, &bytes.Buffer{}); err == nil {
		t.Fatal("no -runs or -plan accepted")
	}
}
