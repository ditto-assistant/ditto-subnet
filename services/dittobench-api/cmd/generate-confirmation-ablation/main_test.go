package main

import (
	"bytes"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestFrozenProductionDatasetMatchesGenerator(t *testing.T) {
	raw, err := render()
	if err != nil {
		t.Fatal(err)
	}
	_, source, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("resolve test source")
	}
	path := filepath.Clean(filepath.Join(filepath.Dir(source), "..", "..", "..", "..", "packages", "ditto-screening-protocol", "ditto_screening_protocol", "data", "confirmation_ablation_v9_shadow.json"))
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(raw, want) {
		t.Fatal("checked-in confirmation ablation dataset does not match the canonical generator")
	}
}
