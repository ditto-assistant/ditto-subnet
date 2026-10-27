package universe

import (
	"strings"
	"testing"
)

// TestV14FamilyMixCoversEveryProgramFamily keeps the SN-351 ledger complete:
// every business family and personal domain the v14 program generators can
// emit has exactly one entry, and the ledger names nothing they cannot emit.
func TestV14FamilyMixCoversEveryProgramFamily(t *testing.T) {
	emitted := map[string]bool{}
	for _, f := range V13Families {
		emitted[string(f)] = true
	}
	for _, d := range V13PersonalDomains {
		emitted[string(d)] = true
	}
	seen := map[string]bool{}
	for _, e := range V14FamilyMix {
		if seen[e.Family] {
			t.Fatalf("%s has two ledger entries", e.Family)
		}
		seen[e.Family] = true
		if !emitted[e.Family] {
			t.Fatalf("ledger entry %s names a family the v14 generators do not emit", e.Family)
		}
	}
	for f := range emitted {
		if !seen[f] {
			t.Fatalf("v14 emits %s with no SN-351 ledger entry", f)
		}
	}
}

// TestV14FamilyMixPortsNoExcludedShape is the SN-351 guardrail. Nothing new to
// v14 may use a shape the generator evidence rules out, a generator-derived
// mechanic must be admitted on at least three rounds of evidence before it is
// generated, and a carried v13 entry that still uses an excluded shape must
// name the issue replacing it.
func TestV14FamilyMixPortsNoExcludedShape(t *testing.T) {
	for _, e := range V14FamilyMix {
		if e.Shape == "" {
			t.Fatalf("%s has no shape", e.Family)
		}
		excluded, isExcluded := V14ExcludedShapeFor(e.Shape)
		switch e.Origin {
		case V14OriginCarried:
			if e.Status != V14StatusCarried {
				t.Fatalf("%s: carried entry has status %s", e.Family, e.Status)
			}
			if isExcluded && e.Tracking == "" {
				t.Fatalf("%s carries the excluded %s shape (%s) with no replacing issue", e.Family, e.Shape, excluded.Reason)
			}
		case V14OriginStructural:
			if isExcluded {
				t.Fatalf("%s: v14-structural entry uses the excluded %s shape (%s)", e.Family, e.Shape, excluded.Evidence)
			}
			if e.Status != V14StatusPendingPanel && e.Status != V14StatusAdmitted {
				t.Fatalf("%s: v14-structural entry has status %s", e.Family, e.Status)
			}
		case V14OriginGenerator:
			if isExcluded {
				t.Fatalf("%s: generator-derived entry uses the excluded %s shape (%s)", e.Family, e.Shape, excluded.Evidence)
			}
			if e.Status != V14StatusAdmitted {
				t.Fatalf("%s: generator-derived mechanic is generated before it was admitted (status %s)", e.Family, e.Status)
			}
			if strings.Count(e.Evidence, "round") < 3 {
				t.Fatalf("%s: generator-derived admission cites fewer than three rounds: %q", e.Family, e.Evidence)
			}
		default:
			t.Fatalf("%s: unknown origin %q", e.Family, e.Origin)
		}
		if e.Status == V14StatusAdmitted && e.Evidence == "" {
			t.Fatalf("%s: admitted with no evidence", e.Family)
		}
	}
	for _, s := range V14ExcludedShapes {
		if s.Reason != "trivial" && s.Reason != "unsolved" {
			t.Fatalf("excluded shape %s has reason %q", s.Shape, s.Reason)
		}
		if s.Evidence == "" {
			t.Fatalf("excluded shape %s cites no evidence", s.Shape)
		}
	}
}

func TestV14FamiliesAwaitingPanelSkipsCarriedEntries(t *testing.T) {
	for _, f := range V14FamiliesAwaitingPanel() {
		e, ok := V14FamilyMixFor(f)
		if !ok || e.Origin == V14OriginCarried || e.Status == V14StatusAdmitted {
			t.Fatalf("%s is not awaiting the panel: %+v", f, e)
		}
	}
}
