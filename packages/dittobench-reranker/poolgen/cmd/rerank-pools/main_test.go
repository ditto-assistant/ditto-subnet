package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeSalt(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "salt")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadSaltFileParsesDecimal(t *testing.T) {
	got, err := readSaltFile(writeSalt(t, " 18446744073709551615\n"))
	if err != nil {
		t.Fatal(err)
	}
	if got != 18446744073709551615 {
		t.Fatalf("salt = %d", got)
	}
}

func TestReadSaltFileRejectsWithoutEchoingContents(t *testing.T) {
	for _, body := range []string{"", "0", "-5", "12x34", "123 456", strings.Repeat("9", 80)} {
		_, err := readSaltFile(writeSalt(t, body))
		if err == nil {
			t.Fatalf("accepted %q", body)
		}
		if body != "" && strings.Contains(err.Error(), body) {
			t.Fatalf("error echoes the salt file contents: %v", err)
		}
	}
	if _, err := readSaltFile(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("accepted a missing file")
	}
}
