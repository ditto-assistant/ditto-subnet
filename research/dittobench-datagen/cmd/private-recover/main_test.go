package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadPrivateBoundaries(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "checkpoint")
	if err := os.WriteFile(path, []byte("checkpoint"), 0600); err != nil {
		t.Fatal(err)
	}
	if got, err := readPrivate(path, 10); err != nil || string(got) != "checkpoint" {
		t.Fatal("valid checkpoint unreadable")
	}
	if _, err := readPrivate(path, 9); err == nil {
		t.Fatal("oversized input accepted")
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readPrivate(link, 10); err == nil {
		t.Fatal("symlink accepted")
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := readPrivate(path, 10); err == nil {
		t.Fatal("shared checkpoint accepted")
	}
}
