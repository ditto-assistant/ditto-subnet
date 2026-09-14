package routerledger

import (
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

func shadowEntry(agentID string, composite float64, firstSeen time.Time) routerscore.LedgerEntry {
	return routerscore.LedgerEntry{
		AgentID:               agentID,
		RouterContractVersion: routerscore.RouterContractVersion,
		WeightEligible:        false,
		CombinedScore:         0,
		ShadowComposite:       composite,
		FirstSeen:             firstSeen,
	}
}

func TestRecordAndSnapshotOrdersByShadowComposite(t *testing.T) {
	s := New()
	base := time.Date(2026, 9, 14, 0, 0, 0, 0, time.UTC)
	s.Record(shadowEntry("low", 0.20, base))
	s.Record(shadowEntry("high", 0.80, base.Add(time.Hour)))
	s.Record(shadowEntry("mid", 0.50, base.Add(2*time.Hour)))

	if s.Len() != 3 {
		t.Fatalf("len = %d, want 3", s.Len())
	}
	snap := s.Snapshot()
	if snap.Count != 3 || len(snap.Entries) != 3 {
		t.Fatalf("snapshot count/entries = %d/%d, want 3/3", snap.Count, len(snap.Entries))
	}
	want := []string{"high", "mid", "low"}
	for i, id := range want {
		if snap.Entries[i].AgentID != id {
			t.Errorf("position %d = %q, want %q", i, snap.Entries[i].AgentID, id)
		}
	}
	if snap.RouterContractVersion == nil || *snap.RouterContractVersion != routerscore.RouterContractVersion {
		t.Errorf("snapshot contract version wrong: %v", snap.RouterContractVersion)
	}
	if snap.GeneratedAt == nil {
		t.Error("snapshot should stamp generated_at after a record")
	}
}

func TestRecordUpsertsAndPreservesFirstSeen(t *testing.T) {
	s := New()
	early := time.Date(2026, 9, 14, 0, 0, 0, 0, time.UTC)
	late := early.Add(24 * time.Hour)
	s.Record(shadowEntry("agent", 0.30, early))
	s.Record(shadowEntry("agent", 0.60, late)) // re-score keeps the earliest first-seen

	if s.Len() != 1 {
		t.Fatalf("upsert should keep one entry, got %d", s.Len())
	}
	snap := s.Snapshot()
	got := snap.Entries[0]
	if got.ShadowComposite != 0.60 {
		t.Errorf("composite not updated: %v", got.ShadowComposite)
	}
	if !got.FirstSeen.Equal(early) {
		t.Errorf("first_seen not preserved: got %v want %v", got.FirstSeen, early)
	}
}

func TestRecordRefusesWeightEligibleAndEmptyIdentity(t *testing.T) {
	s := New()
	weighted := shadowEntry("agent", 0.9, time.Now())
	weighted.WeightEligible = true
	s.Record(weighted) // must be refused (shadow invariant)
	s.Record(shadowEntry("", 0.5, time.Now())) // no identity -> dropped

	if s.Len() != 0 {
		t.Fatalf("store must reject weight-eligible and identity-less entries, len = %d", s.Len())
	}
}

func TestEmptySnapshotIsWellFormed(t *testing.T) {
	s := New()
	snap := s.Snapshot()
	if snap.Count != 0 || len(snap.Entries) != 0 {
		t.Fatalf("empty snapshot not empty: %+v", snap)
	}
	if snap.GeneratedAt != nil {
		t.Error("empty store should not stamp generated_at")
	}
}
