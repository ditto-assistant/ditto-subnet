package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/persona"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestV2IsServedFromTheFrozenSnapshot is the guard that makes the v2/v3 split
// hold up over time.
//
// v2 is an immutable contract: on-chain scoring has been running it, and its
// bytes must keep regenerating identically or no already-scored run can be
// audited. v3 is the anti-gaming release, and that hardening perturbs the shared
// rng stream globally, so v2 is served from a frozen snapshot of the generator
// (internal/v2gen) rather than from version conditionals through the live path.
//
// The risk this pins is the quiet one: someone adds hardening to the live
// generator and v2 silently drifts. The golden vectors would catch it, but this
// states the invariant directly -- v2 must not see anything v3-only, no matter
// what the live generator grows next.
func TestV2IsServedFromTheFrozenSnapshot(t *testing.T) {
	prof, _ := ProfileFor("full")
	const seed = int64(123456789)

	v2, err := GenerateDataset(seed, prof, protocol.BenchVersionV2)
	if err != nil {
		t.Fatalf("v2: %v", err)
	}
	v3, err := GenerateDataset(seed, prof, protocol.BenchVersionV3)
	if err != nil {
		t.Fatalf("v3: %v", err)
	}

	// Every v3-only mechanism must be absent from v2 and present in v3.
	countAudit := func(a DatasetArtifact) int {
		n := 0
		for _, c := range a.MemoryCases {
			if len(c.TwinGroup) >= len(persona.AuditTwinPrefix) &&
				c.TwinGroup[:len(persona.AuditTwinPrefix)] == persona.AuditTwinPrefix {
				n++
			}
		}
		return n
	}
	if got := countAudit(v2); got != 0 {
		t.Errorf("v2 carries %d transform-audit cases; v2 is frozen and must carry none", got)
	}
	if got := countAudit(v3); got == 0 {
		t.Error("v3 carries no transform-audit cases; the anti-gaming release lost its audit")
	}

	// The cross-user lifecycle probe (B3) is v3-only too.
	countXUser := func(a DatasetArtifact) int {
		n := 0
		for _, c := range a.MemoryCases {
			if xuNoun(c.Question) != "" {
				n++
			}
		}
		return n
	}
	if got := countXUser(v2); got != 0 {
		t.Errorf("v2 carries %d cross-user lifecycle cases; v2 is frozen and must carry none", got)
	}
	if got := countXUser(v3); got == 0 {
		t.Error("v3 carries no cross-user lifecycle cases; B3 is not reaching v3")
	}

	// And the two contracts must not collide.
	h2, _, err := v2.SHA256Hex()
	if err != nil {
		t.Fatalf("hash v2: %v", err)
	}
	h3, _, err := v3.SHA256Hex()
	if err != nil {
		t.Fatalf("hash v3: %v", err)
	}
	if h2 == h3 {
		t.Fatal("v2 and v3 hash identically; the versions are not distinct contracts")
	}
}
