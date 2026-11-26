package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/datagen"
)

// The scorer keeps the category literal so it need not import datagen; this
// pins the two spellings together.
func TestV18UnlinkedReplyCategoryMatchesDatagen(t *testing.T) {
	if scorer.V18UnlinkedProbeCategory != datagen.V16CategoryUnlinkedProbe {
		t.Fatalf("scorer reads %q, datagen emits %q", scorer.V18UnlinkedProbeCategory, datagen.V16CategoryUnlinkedProbe)
	}
	if v18UnlinkedReplyPosture != scorer.UnlinkedReplyReview {
		t.Fatalf("default posture %q, want review", v18UnlinkedReplyPosture)
	}
}
