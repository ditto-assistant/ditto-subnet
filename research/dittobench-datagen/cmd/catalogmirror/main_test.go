package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestRustMirrorIsByteIdentical pins the regeneration contract: the Rust
// rendering of CatalogForVersion(13) is byte-for-byte the committed starter-kit
// catalog.rs, so `go run ./cmd/catalogmirror -format rust > catalog.rs` needs no
// cargo fmt pass. Skips when the mirror is absent (standalone module use).
func TestRustMirrorIsByteIdentical(t *testing.T) {
	rel := filepath.Join("..", "..", "..", "..", "miners", "dittobench-starter-kit", "src", "catalog.rs")
	want, err := os.ReadFile(rel)
	if err != nil {
		t.Skipf("mirror %s not present: %v", rel, err)
	}
	got, err := renderRust(protocol.BenchVersionV13, catalog.CatalogForVersion(protocol.BenchVersionV13))
	if err != nil {
		t.Fatal(err)
	}
	if got != string(want) {
		t.Fatalf("starter-kit catalog.rs is not the byte-identical catalogmirror rendering; regenerate with: go run ./cmd/catalogmirror -bench-version 13 -format rust > miners/dittobench-starter-kit/src/catalog.rs")
	}
}
