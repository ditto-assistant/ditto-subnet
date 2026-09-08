package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestControlInputPathsAndDigestsAreClosed(t *testing.T) {
	for _, value := range []string{"../secret", "/absolute", "a/../b", "a\\b", "a\x00b"} {
		if relative(value) {
			t.Fatal("unsafe path accepted")
		}
	}
	if !relative("groups/example/src/lib.rs") || digest("ABC") {
		t.Fatal("path or hash contract changed")
	}
}
func TestControlReadRejectsLeafAndDirectorySymlinks(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "src"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "src/source"), []byte("public"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, pair := range [][2]string{{"src/source", "leaf"}, {"src", "directory"}} {
		if err := os.Symlink(pair[0], filepath.Join(root, pair[1])); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"leaf", "directory/source"} {
		if _, err := read(root, name, 100); err == nil {
			t.Fatal("source link accepted")
		}
	}
	if body, err := read(root, "src/source", 100); err != nil || string(body) != "public" {
		t.Fatal("regular input rejected")
	}
}
func TestControlSourceReadIsByteBounded(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "source"), []byte("public"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := read(root, "source", 2); err == nil {
		t.Fatal("oversize input accepted")
	}
}
