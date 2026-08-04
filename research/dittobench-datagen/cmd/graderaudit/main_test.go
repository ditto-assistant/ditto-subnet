package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestAuditBuckets pins the classification: credited answers and disqualifier
// zeroes stay OFF the sheet; only typed-check zeroes (the one place a grader
// false negative can hide) land on it.
func TestAuditBuckets(t *testing.T) {
	mc := protocol.MemoryCase{
		ID:                "c-value",
		QuestionType:      "single-session-recall",
		Question:          "What's my favorite cuisine?",
		ExpectedAnswer:    "Thai",
		DistractorAnswers: []string{"Ethiopian"},
	}
	cases := []gen.ArtifactCase{
		{MemoryCase: mc},
		{MemoryCase: withID(mc, "c-distractor")},
		{MemoryCase: withID(mc, "c-miss")},
		{MemoryCase: withID(mc, "c-absent")},
	}
	transcripts := map[string]protocol.RunResponse{
		"c-value":      {FinalText: "You said Thai food is your favorite."},
		"c-distractor": {FinalText: "It's Ethiopian, I believe."},
		// A synonymous-but-unmatched answer: the false-negative shape the
		// sheet exists to surface.
		"c-miss": {FinalText: "The cuisine of Thailand."},
	}
	sheet, stats, missing := audit(cases, transcripts)
	if missing != 1 {
		t.Fatalf("missing = %d, want 1", missing)
	}
	if len(sheet) != 1 || sheet[0].CaseID != "c-miss" {
		t.Fatalf("sheet = %+v, want exactly c-miss", sheet)
	}
	s := stats[protocol.AnswerValue]
	if s == nil || s.Graded != 3 || s.Credited != 1 || s.Disqualified != 1 || s.TypedZero != 1 {
		t.Fatalf("stats = %+v, want graded=3 credited=1 disqualified=1 typed_zero=1", s)
	}
}

func withID(mc protocol.MemoryCase, id string) protocol.MemoryCase {
	mc.ID = id
	return mc
}
