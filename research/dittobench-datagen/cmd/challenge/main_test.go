package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func TestValidateFileReportsDecodeErrorsAsIssues(t *testing.T) {
	dir := t.TempDir()
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte(`{"contract": 7}`), 0o644); err != nil {
		t.Fatal(err)
	}
	r, err := validateFile(bad)
	if err != nil || r.Valid || len(r.Issues) != 1 || r.Issues[0].Rule != "decode" {
		t.Fatalf("decode failure not reported as an issue: %+v %v", r, err)
	}

	good := filepath.Join(dir, "good.json")
	if err := writeJSON(good, challenge.ReferencePack(9, testHotkey)); err != nil {
		t.Fatal(err)
	}
	r, err = validateFile(good)
	if err != nil || !r.Valid {
		t.Fatalf("reference pack rejected: %+v %v", r, err)
	}
}
