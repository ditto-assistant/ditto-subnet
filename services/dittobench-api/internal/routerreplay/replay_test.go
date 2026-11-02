package routerreplay

import (
	"math"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// TestDefaultCorpusLoadsAndValidates confirms the embedded reference corpus
// parses, validates, and covers every big-four harness exactly once.
func TestDefaultCorpusLoadsAndValidates(t *testing.T) {
	c, err := Default()
	if err != nil {
		t.Fatalf("default corpus failed to load: %v", err)
	}
	if c.CorpusID == "" {
		t.Error("corpus id missing")
	}
	want := routerharness.Harnesses()
	if len(c.Harnesses) != len(want) {
		t.Fatalf("corpus has %d harnesses, want %d", len(c.Harnesses), len(want))
	}
	seen := make(map[routerharness.Harness]bool)
	for _, r := range c.Harnesses {
		if !r.Harness.Valid() {
			t.Errorf("invalid harness %q", r.Harness)
		}
		seen[r.Harness] = true
	}
	for _, h := range want {
		if !seen[h] {
			t.Errorf("corpus missing harness %q", h)
		}
	}
}

// TestReplayComposeYieldsNonZeroShadowComposite is the core Part 2 gate: a replay
// fixture folds into a real, non-zero shadow composite while the folded
// CombinedScore stays 0 and the entry is never weight-eligible.
func TestReplayComposeYieldsNonZeroShadowComposite(t *testing.T) {
	c, err := Default()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	axis := routerscore.DefaultEffAxisWeights
	gate := routerscore.ShadowFloorGate()
	outcomes := c.Outcomes(axis, gate)
	if len(outcomes) != len(c.Harnesses) {
		t.Fatalf("got %d outcomes, want %d", len(outcomes), len(c.Harnesses))
	}
	for _, o := range outcomes {
		if !o.Operational {
			t.Errorf("harness %q not operational in replay", o.Harness)
		}
		if o.Efficiency <= 0 || o.Efficiency > 1 {
			t.Errorf("harness %q efficiency %v out of (0,1]", o.Harness, o.Efficiency)
		}
	}
	entry := routerscore.BuildEntry("", "agent-1", routerscore.DefaultHarnessWeights, false, time.Time{}, outcomes)
	if entry.WeightEligible {
		t.Error("replay entry must never be weight-eligible")
	}
	if entry.CombinedScore != 0 {
		t.Errorf("folded combined score = %v, want 0", entry.CombinedScore)
	}
	if entry.ShadowComposite <= 0 || entry.ShadowComposite > 1 {
		t.Fatalf("shadow composite = %v, want a real value in (0,1]", entry.ShadowComposite)
	}
}

// TestReplayIsDeterministic confirms the same corpus yields an identical
// composite across repeated folds (offline, no provider variance).
func TestReplayIsDeterministic(t *testing.T) {
	c, err := Default()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	axis := routerscore.DefaultEffAxisWeights
	gate := routerscore.ShadowFloorGate()
	a := routerscore.Combine(routerscore.DefaultHarnessWeights, c.Outcomes(axis, gate))
	b := routerscore.Combine(routerscore.DefaultHarnessWeights, c.Outcomes(axis, gate))
	if math.Abs(a-b) > 0 {
		t.Fatalf("replay not deterministic: %v vs %v", a, b)
	}
}

// TestNonOperationalRecordForfeitsOnlyItsSlice confirms a forfeited harness drops
// only its own weight, matching the soft combine.
func TestNonOperationalRecordForfeitsOnlyItsSlice(t *testing.T) {
	c, err := Default()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	axis := routerscore.DefaultEffAxisWeights
	gate := routerscore.ShadowFloorGate()
	full := routerscore.Combine(routerscore.DefaultHarnessWeights, c.Outcomes(axis, gate))

	// Force the first harness non-operational and re-fold.
	degraded := c
	degraded.Harnesses = append([]Record(nil), c.Harnesses...)
	dropped := degraded.Harnesses[0]
	degraded.Harnesses[0].Operational = false
	partial := routerscore.Combine(routerscore.DefaultHarnessWeights, degraded.Outcomes(axis, gate))

	if partial >= full {
		t.Fatalf("forfeit did not reduce composite: full=%v partial=%v", full, partial)
	}
	// The drop is bounded by the harness weight (efficiency <= 1).
	lost := full - partial
	if lost > routerscore.DefaultHarnessWeights[dropped.Harness]+1e-9 {
		t.Fatalf("forfeit lost %v, more than harness weight %v", lost, routerscore.DefaultHarnessWeights[dropped.Harness])
	}
}
