package runner

import (
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/sandbox"
)

// The published runtime envelope (docs/RUNTIME-ENVELOPE.md) is what miners
// build against; the scorer's deadlines must be the published ones.
func TestDeadlinesMatchPublishedEnvelope(t *testing.T) {
	timing := sandbox.PublishedEnvelope.Timing
	if got, want := perCaseTimeoutFor(14), time.Duration(timing.CaseCallSeconds)*time.Second; got != want {
		t.Errorf("per-case deadline %s, envelope publishes %s", got, want)
	}
	if got, want := v7SeedTimeout, time.Duration(timing.SeedCallSeconds)*time.Second; got != want {
		t.Errorf("/seed deadline %s, envelope publishes %s", got, want)
	}
}
