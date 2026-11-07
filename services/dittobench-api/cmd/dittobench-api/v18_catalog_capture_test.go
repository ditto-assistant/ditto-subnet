package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/catalog"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// A harness that forwards the whole v18 menu must still be captured
// completely, or the catalog gate reads a truncated offer.
func TestCatalogCaptureHoldsTheWholeV18Surface(t *testing.T) {
	if catalog.V18MaxTools > catalogCaptureMaxTools {
		t.Fatalf("a v18 menu may hold %d tools, capture keeps %d", catalog.V18MaxTools, catalogCaptureMaxTools)
	}
	for _, seed := range []int64{1, 123456789} {
		if n := len(catalog.CatalogForSeed(protocol.BenchVersionV18, seed)); n > catalogCaptureMaxTools {
			t.Fatalf("v18 surface has %d tools, capture keeps %d", n, catalogCaptureMaxTools)
		}
	}
}
