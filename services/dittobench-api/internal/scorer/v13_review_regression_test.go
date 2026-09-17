package scorer

import (
	"testing"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func TestReviewV13HonestCorrectionDoesNotLoseCredit(t *testing.T) {
	c := protocol.ToolCase{ID: "correction", EffectAnswer: "Monday", EffectForbidden: []string{"Friday"}, ExpectedTools: []protocol.ToolSpec{{Name: "search_memories"}}}
	resp := protocol.RunResponse{FinalText: "The handoff is Monday, not Friday."}
	if got := v13Score(t, c, resp); got.Score != 1 {
		t.Fatalf("honest correction got %v, want 1: %v", got.Score, got.Notes)
	}
}
