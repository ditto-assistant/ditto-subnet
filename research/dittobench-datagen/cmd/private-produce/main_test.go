package main

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"testing"
)

func TestReservedSalt(t *testing.T) {
	for _, tc := range []struct {
		name  string
		data  []byte
		mode  os.FileMode
		valid bool
	}{
		{"valid", binary.BigEndian.AppendUint64(nil, 123), 0600, true},
		{"zero", make([]byte, 8), 0600, false},
		{"short", []byte{1}, 0600, false},
		{"long", make([]byte, 9), 0600, false},
		{"public", binary.BigEndian.AppendUint64(nil, 123), 0644, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "salt")
			if err := os.WriteFile(path, tc.data, tc.mode); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(path, tc.mode); err != nil {
				t.Fatal(err)
			}
			got, err := readReservedSalt(path)
			if tc.valid && (err != nil || got != 123) {
				t.Fatalf("got %d, %v", got, err)
			}
			if !tc.valid && err == nil {
				t.Fatal("invalid entropy accepted")
			}
		})
	}
}

func TestReservedSaltRejectsSymlinkAndDirectory(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "salt")
	if err := os.WriteFile(path, binary.BigEndian.AppendUint64(nil, 123), 0600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	for _, invalid := range []string{dir, link, filepath.Join(dir, "missing")} {
		if _, err := readReservedSalt(invalid); err == nil {
			t.Fatal("non-regular entropy accepted")
		}
	}
}
