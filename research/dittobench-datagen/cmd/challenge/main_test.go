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

func TestPackAndInspectRoundTrip(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "gen")
	if err := os.MkdirAll(filepath.Join(src, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{
		"go.mod":    "module example.com/gen\n\ngo 1.23\n",
		"main.go":   "package main\n\nfunc main() {}\n",
		".git/HEAD": "ref: x\n",
		".DS_Store": "junk",
	} {
		if err := os.WriteFile(filepath.Join(src, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	out := filepath.Join(dir, "gen.tar.gz")
	if err := runPack([]string{"-dir", src, "-out", out}); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	info, files, err := challenge.InspectArchive(raw)
	if err != nil || info.Files != 2 || len(files) != 2 {
		t.Fatalf("packed archive: %+v %v", info, err)
	}
}
