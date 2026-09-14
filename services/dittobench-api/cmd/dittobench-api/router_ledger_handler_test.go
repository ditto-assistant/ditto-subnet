package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerledger"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// handleRouterLedger must serve a well-formed, empty ledger when the router track
// was never initialized (nil store), so a polling relay never 500s and cannot
// tell "no track" from "track with no entries".
func TestHandleRouterLedgerNilStoreServesEmptyLedger(t *testing.T) {
	rec := httptest.NewRecorder()
	(&server{}).handleRouterLedger(rec, httptest.NewRequest(http.MethodGet, "/v1/router/ledger", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("nil store: status %d, want %d", rec.Code, http.StatusOK)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store", got)
	}
	var ledger routerscore.Ledger
	if err := json.Unmarshal(rec.Body.Bytes(), &ledger); err != nil {
		t.Fatalf("body is not a routerscore.Ledger: %v", err)
	}
	if ledger.Count != 0 || len(ledger.Entries) != 0 {
		t.Fatalf("nil store must serve an empty ledger, got %+v", ledger)
	}
	// Entries must be a JSON array, not null, so relay clients can iterate.
	if ledger.Entries == nil {
		t.Error("Entries must be a non-nil empty slice so it marshals as [] not null")
	}
}

// A populated store is served as its ordered shadow snapshot, and every served
// entry stays shadow: weight_eligible=false and folded combined_score=0.
func TestHandleRouterLedgerServesShadowSnapshot(t *testing.T) {
	store := routerledger.New()
	base := time.Date(2026, 9, 14, 0, 0, 0, 0, time.UTC)
	store.Record(routerscore.LedgerEntry{
		AgentID:               "agent-lo",
		RouterContractVersion: routerscore.RouterContractVersion,
		ShadowComposite:       0.20,
		FirstSeen:             base,
	})
	store.Record(routerscore.LedgerEntry{
		AgentID:               "agent-hi",
		RouterContractVersion: routerscore.RouterContractVersion,
		ShadowComposite:       0.80,
		FirstSeen:             base.Add(time.Hour),
	})

	rec := httptest.NewRecorder()
	(&server{routerLedger: store}).handleRouterLedger(
		rec, httptest.NewRequest(http.MethodGet, "/v1/router/ledger", nil),
	)

	if rec.Code != http.StatusOK {
		t.Fatalf("status %d, want %d", rec.Code, http.StatusOK)
	}
	var ledger routerscore.Ledger
	if err := json.Unmarshal(rec.Body.Bytes(), &ledger); err != nil {
		t.Fatalf("body is not a routerscore.Ledger: %v", err)
	}
	if ledger.Count != 2 || len(ledger.Entries) != 2 {
		t.Fatalf("count/entries = %d/%d, want 2/2", ledger.Count, len(ledger.Entries))
	}
	if ledger.Entries[0].AgentID != "agent-hi" {
		t.Errorf("entries not ordered by shadow composite desc: %+v", ledger.Entries)
	}
	for _, e := range ledger.Entries {
		if e.WeightEligible {
			t.Errorf("served entry %q is weight-eligible; shadow invariant broken", e.AgentID)
		}
		if e.CombinedScore != 0 {
			t.Errorf("served entry %q has non-zero folded combined_score %v", e.AgentID, e.CombinedScore)
		}
	}
}
