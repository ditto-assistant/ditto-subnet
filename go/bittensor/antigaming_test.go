package bittensor

import (
	"sort"
	"testing"
)

func TestPartitionFixture_DeterministicAndRotates(t *testing.T) {
	ids := []string{"a", "b", "c", "d", "e", "f", "g", "h", "i", "j"}
	a := PartitionFixture(ids, "secret-1", 0.3, 0.2)
	b := PartitionFixture(ids, "secret-1", 0.3, 0.2)
	if !equalStringSets(a.Private, b.Private) || !equalStringSets(a.Canary, b.Canary) || !equalStringSets(a.Public, b.Public) {
		t.Fatalf("partition not deterministic for same secret")
	}
	rotated := PartitionFixture(ids, "secret-2", 0.3, 0.2)
	if equalStringSets(a.Private, rotated.Private) && equalStringSets(a.Canary, rotated.Canary) {
		t.Fatalf("rotating the secret should change the partition")
	}
	if len(a.Private)+len(a.Canary)+len(a.Public) != len(ids) {
		t.Fatalf("partition lost or duplicated case IDs: %v", a)
	}
}

func TestPartitionFixture_ClampsToPublicMinimum(t *testing.T) {
	ids := []string{"a", "b", "c", "d"}
	part := PartitionFixture(ids, "s", 0.6, 0.6)
	if len(part.Public) == 0 {
		t.Fatalf("expected at least one public case after clamping, got %+v", part)
	}
}

func TestParaphraseSeed_DeterministicAndRotates(t *testing.T) {
	a := ParaphraseSeed("secret-1", "case-x")
	b := ParaphraseSeed("secret-1", "case-x")
	if a != b {
		t.Fatalf("paraphrase seed not deterministic for same inputs: %q vs %q", a, b)
	}
	if c := ParaphraseSeed("secret-2", "case-x"); a == c {
		t.Fatalf("paraphrase seed should change when secret rotates")
	}
	if c := ParaphraseSeed("secret-1", "case-y"); a == c {
		t.Fatalf("paraphrase seed should change when caseID changes")
	}
	if len(a) != 64 {
		t.Fatalf("expected 64-char hex digest, got %d chars", len(a))
	}
}

func TestMemorisationDiscount(t *testing.T) {
	// No samples → no discount.
	if v := MemorisationDiscount(0.9, 0.4, 0, 0.1, 0.5, 0.5); v != 1.0 {
		t.Fatalf("expected 1.0 with no samples, got %v", v)
	}
	// Small gap → no discount.
	if v := MemorisationDiscount(0.9, 0.85, 10, 0.1, 0.5, 0.5); v != 1.0 {
		t.Fatalf("expected 1.0 when gap below threshold, got %v", v)
	}
	// Mid gap.
	v := MemorisationDiscount(0.9, 0.6, 10, 0.1, 0.5, 0.5)
	if v <= 0.74 || v >= 0.76 {
		t.Fatalf("expected ~0.75 mid-range discount, got %v", v)
	}
	// Full gap saturates.
	if v := MemorisationDiscount(0.9, 0.0, 10, 0.1, 0.5, 0.5); v != 0.5 {
		t.Fatalf("expected saturated discount 0.5, got %v", v)
	}
}

func TestDistractorBundle_AvoidsExpectedAndForbidden(t *testing.T) {
	caseID := "ctest"
	expected := []string{"good1"}
	forbidden := []string{"bad1"}
	candidates := []string{"good1", "bad1", "x1", "x2", "x3", "x4", "x5"}
	got := DistractorBundleFor(caseID, expected, forbidden, candidates, "secret", 3)
	if len(got) != 3 {
		t.Fatalf("expected 3 distractors, got %d (%v)", len(got), got)
	}
	for _, id := range got {
		if id == "good1" || id == "bad1" {
			t.Fatalf("distractor included expected/forbidden id %q", id)
		}
	}
	again := DistractorBundleFor(caseID, expected, forbidden, candidates, "secret", 3)
	if !equalStringSets(got, again) {
		t.Fatalf("distractor bundle not deterministic: %v vs %v", got, again)
	}
	other := DistractorBundleFor(caseID, expected, forbidden, candidates, "different-secret", 3)
	if equalStringSets(got, other) {
		t.Fatalf("expected different distractors under a different secret")
	}
}

func TestEnsureParaphraseChanged(t *testing.T) {
	if err := EnsureParaphraseChanged("What did I say?", "What did I say?"); err == nil {
		t.Fatalf("expected error for identical paraphrase")
	}
	if err := EnsureParaphraseChanged("What did I say?", "What did I say???"); err == nil {
		t.Fatalf("punctuation-only changes should be rejected")
	}
	if err := EnsureParaphraseChanged("What did I say?", "What was it I told you?"); err != nil {
		t.Fatalf("expected no error for genuine paraphrase, got %v", err)
	}
}

func TestAggregateWithDiscount_PenalisesMemorisingMiner(t *testing.T) {
	// Two miners with identical public means but very different canary
	// means. The high-gap miner should land at a strictly lower normalised
	// weight than the matched-canary miner.
	scores := []Score{
		// Miner A: matched public/canary (no memorisation signal).
		{ChallengeID: "a1", Visibility: "public", Score: 0.90},
		{ChallengeID: "a2", Visibility: "public", Score: 0.90},
		{ChallengeID: "a3", Visibility: "canary", Score: 0.88},
		{ChallengeID: "a4", Visibility: "canary", Score: 0.89},
		// Miner B: high public but collapsing canary (memorising).
		{ChallengeID: "b1", Visibility: "public", Score: 0.90},
		{ChallengeID: "b2", Visibility: "public", Score: 0.90},
		{ChallengeID: "b3", Visibility: "canary", Score: 0.30},
		{ChallengeID: "b4", Visibility: "canary", Score: 0.30},
	}
	hk := map[string]string{
		"a1": "A", "a2": "A", "a3": "A", "a4": "A",
		"b1": "B", "b2": "B", "b3": "B", "b4": "B",
	}
	agg, details := AggregateWithDiscount(scores, hk, MechanismCore, DiscountOpts{})
	weights := agg.Weights
	if agg.Mechanism != MechanismCore {
		t.Fatalf("expected Mechanism=core, got %q", agg.Mechanism)
	}
	if got := len(weights); got != 2 {
		t.Fatalf("expected weights for two miners, got %d (%v)", got, weights)
	}
	if weights["A"]+weights["B"] < 0.9999 || weights["A"]+weights["B"] > 1.0001 {
		t.Fatalf("weights should sum to 1, got A=%v B=%v", weights["A"], weights["B"])
	}
	if !(weights["A"] > weights["B"]) {
		t.Fatalf("memorising miner B should be penalised: A=%v B=%v", weights["A"], weights["B"])
	}
	for _, d := range details {
		if d.Hotkey == "B" && d.Discount >= 1.0 {
			t.Fatalf("miner B should have discount<1, got %v", d.Discount)
		}
		if d.Hotkey == "A" && d.Discount != 1.0 {
			t.Fatalf("miner A should have discount=1, got %v", d.Discount)
		}
	}
}

func TestAggregateWithDiscount_IgnoresUnmappedChallenges(t *testing.T) {
	scores := []Score{
		{ChallengeID: "x", Visibility: "public", Score: 0.5},
		{ChallengeID: "y", Visibility: "public", Score: 0.7}, // not in map
	}
	hk := map[string]string{"x": "A"}
	agg, _ := AggregateWithDiscount(scores, hk, MechanismCore, DiscountOpts{})
	if len(agg.Weights) != 1 {
		t.Fatalf("expected 1 miner aggregate, got %v", agg.Weights)
	}
}

func equalStringSets(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	x := append([]string(nil), a...)
	y := append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	for i := range x {
		if x[i] != y[i] {
			return false
		}
	}
	return true
}
