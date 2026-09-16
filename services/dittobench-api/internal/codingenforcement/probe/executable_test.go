package probe

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestRunningExecutableSHA256MeasuresTheRunningBinary(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("/proc/self/exe is Linux only")
	}
	got, err := RunningExecutableSHA256()
	if err != nil {
		t.Fatal(err)
	}
	path, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if got != hex.EncodeToString(sum[:]) {
		t.Fatalf("running executable digest = %s, file digest = %x", got, sum)
	}
}

func TestFileSHA256RefusesNonRegularAndEmptyFiles(t *testing.T) {
	directory := t.TempDir()
	if _, err := fileSHA256(directory); err == nil {
		t.Fatal("a directory was measured")
	}
	empty := filepath.Join(directory, "empty")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := fileSHA256(empty); err == nil {
		t.Fatal("an empty file was measured")
	}
}
