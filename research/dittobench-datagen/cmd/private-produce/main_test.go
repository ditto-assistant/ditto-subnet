package main

import (
	"encoding/binary"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
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

func TestBudgetCheckpointIsPrivateAndReplaced(t *testing.T) {
	dir := t.TempDir()
	for _, charged := range []float64{0, 1.5, .001} {
		want := privatesurface.BudgetSnapshot{LimitUSD: 10, ChargedUSD: charged}
		if err := writeBudgetCheckpoint(dir, want); err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(dir, "spend.json")
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm() != 0600 {
			t.Fatal("checkpoint not private", err)
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var got privatesurface.BudgetSnapshot
		if err := json.Unmarshal(raw, &got); err != nil || got != want {
			t.Fatalf("bad checkpoint: %+v %v", got, err)
		}
		entries, _ := os.ReadDir(dir)
		if len(entries) != 1 {
			t.Fatal("left temporary checkpoint")
		}
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
