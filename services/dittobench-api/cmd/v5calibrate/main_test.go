package main

import "testing"

// TestV5ShipGateMultiSeed is the hermetic, cross-seed realization of the v5 plan's
// 4.11 ship gate (offline half) and its counter-guard. It runs the harness
// archetypes against REAL generated v5 datasets over 24 rotating seeds through the
// real scorer pipeline and asserts:
//
//   - The Unicorn-style leak-router and the no-model cleartext parser drop below
//     the 0.85 champion line on EVERY seed (the exploit is closed).
//   - A conversationally-grounded harness with the SAME strong retrieval stack as
//     the router stays far above them (the drop is specific to the conversational
//     margin, not a blanket difficulty hike -- the counter-guard).
//   - The conversational-sanity metric separates them cleanly (~high vs 0).
//
// The on-model winnability precheck against the frozen-Qwen reference still needs
// the model relay; this pins the half that is measurable hermetically.
func TestV5ShipGateMultiSeed(t *testing.T) {
	seeds := make([]int64, 24)
	for i := range seeds {
		seeds[i] = 20260720 + int64(i)
	}
	results, err := Calibrate(seeds, "full")
	if err != nil {
		t.Fatalf("calibrate: %v", err)
	}
	byName := map[string]Result{}
	for _, r := range results {
		byName[r.Archetype] = r
		t.Logf("%-16s comp=%.3f±%.3f mem=%.3f conv=%.3f below0.85=%.0f%%",
			r.Archetype, r.CompositeMean, r.CompositeStdErr, r.MemoryMean, r.ConvSanityMean, r.FracBelow085*100)
	}

	router := byName["unicorn-router"]
	parser := byName["cleartext-parser"]
	honest := byName["honest-strong"]

	// The exploiters drop below the champion line on every seed.
	if router.FracBelow085 != 1.0 {
		t.Errorf("unicorn-router must be <0.85 on every seed, got %.0f%%", router.FracBelow085*100)
	}
	if parser.FracBelow085 != 1.0 {
		t.Errorf("cleartext-parser must be <0.85 on every seed, got %.0f%%", parser.FracBelow085*100)
	}

	// Conversational sanity separates honest from the exploiters.
	if honest.ConvSanityMean < 0.7 {
		t.Errorf("honest harness must pass conversational sanity, got %.3f", honest.ConvSanityMean)
	}
	if router.ConvSanityMean != 0 || parser.ConvSanityMean != 0 {
		t.Errorf("exploiters must fail conversational sanity, got router=%.3f parser=%.3f", router.ConvSanityMean, parser.ConvSanityMean)
	}
	// Per-seed conversational-sanity variance must stay LOW: a grounded harness must
	// not be randomly gated seed-to-seed. The geometric-mean metric over the scaled
	// single-turn slices holds this well under the old weakest-link minimum's ~0.17-0.20
	// (measured), so a regression to the brittle min or thin slices trips this.
	if honest.ConvSanitySD > 0.13 {
		t.Errorf("honest conversational-sanity per-seed sd too high (%.3f > 0.13): grounded harness is being randomly gated", honest.ConvSanitySD)
	}
	if h := byName["honest-strong"]; h.ConvSanitySD > 0.13 {
		t.Errorf("honest-strong conversational-sanity per-seed sd too high (%.3f > 0.13)", h.ConvSanitySD)
	}

	// Counter-guard: same retrieval stack, but grounding conversation keeps the
	// honest harness far above the router -- the drop is specific to the
	// conversational margin, not a blanket difficulty hike.
	if honest.CompositeMean-router.CompositeMean < 0.25 {
		t.Errorf("v5 must separate honest (%.3f) from the same-retrieval router (%.3f) by >0.25", honest.CompositeMean, router.CompositeMean)
	}
	// And the honest harness is not dragged down by the conversational gate: its
	// composite retains most of its memory mean (conv factor ~1.0), while the
	// router loses roughly half of its (equal) memory mean to the gate.
	honestRetained := honest.CompositeMean / honest.MemoryMean
	routerPenalized := router.CompositeMean / router.MemoryMean
	if honestRetained < 0.8 {
		t.Errorf("honest harness must retain most of its memory mean (no conversational penalty), retained %.2f", honestRetained)
	}
	if routerPenalized > 0.65 {
		t.Errorf("router must be penalized by the conversational gate, retained %.2f", routerPenalized)
	}
	// A conversationally-grounded strong harness sits in a healthy, winnable band
	// (v5 did not make the ceiling degenerate). We assert a band rather than a
	// specific max: with the scaled case counts the between-seed variance is low by
	// design, so no single seed flukes high — the SYNTHETIC archetype's ceiling is
	// its memMean, not 0.85. Real-model winnability (a strong harness reaching high
	// scores) is established on-model in docs/BASELINES-v5-phaseA.md; here we only
	// require the honest band to be non-degenerate and clear of the exploiters.
	if honest.CompositeMean < 0.6 {
		t.Errorf("honest harness band is degenerate under v5, mean %.3f", honest.CompositeMean)
	}
}
