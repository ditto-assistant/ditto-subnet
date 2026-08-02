package main

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestWithObservedTrajectory verifies the memory-case grading seam: the
// validator's observed tool trajectory replaces the harness self-report when
// anything was observed, and is left untouched otherwise. This is the seam that
// makes injection-bait compliance unscrubable (the grader's BaitTool check runs
// over these substituted calls).
func TestWithObservedTrajectory(t *testing.T) {
	selfReport := protocol.RunResponse{
		FinalText: "You live in Lisbon.",
		ToolCalls: []protocol.ObservedToolCall{{Name: "search_memories"}},
	}

	// Observed calls win over the self-report: a laundering harness that hid its
	// gmail_send call from tool_calls cannot hide it from the observed trajectory.
	observed := []protocol.ObservedToolCall{{Name: "gmail_send"}}
	got := withObservedTrajectory(selfReport, observed)
	if len(got.ToolCalls) != 1 || got.ToolCalls[0].Name != "gmail_send" {
		t.Fatalf("observed trajectory must replace self-report: got %+v", got.ToolCalls)
	}

	// Nothing observed (local stubbing or a pure recall answer): self-report kept.
	kept := withObservedTrajectory(selfReport, nil)
	if len(kept.ToolCalls) != 1 || kept.ToolCalls[0].Name != "search_memories" {
		t.Fatalf("with no observed calls the self-report must be preserved: got %+v", kept.ToolCalls)
	}
}
