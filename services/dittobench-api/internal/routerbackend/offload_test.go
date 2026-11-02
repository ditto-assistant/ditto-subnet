package routerbackend

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// TestNewOffloadedBackendUnsetIsErrOffloaded confirms an empty URL keeps the
// unwired seam: main() then selects the replay LocalBackend and never calls out.
func TestNewOffloadedBackendUnsetIsErrOffloaded(t *testing.T) {
	b := NewOffloadedBackend("", true)
	if _, ok := b.(OffloadedBackend); !ok {
		t.Fatalf("empty url must yield the unwired OffloadedBackend, got %T", b)
	}
	_, err := b.Score(context.Background(), RouterSubmission{})
	if !errors.Is(err, ErrOffloaded) {
		t.Fatalf("unset offload must return ErrOffloaded, got %v", err)
	}
}

// TestOffloadedBackendScoresAndClampsToShadow confirms a set URL POSTs the
// submission, parses the remote entry, and defensively forces it back to shadow
// even when the remote (wrongly) returns a weight-eligible, folded score.
func TestOffloadedBackendScoresAndClampsToShadow(t *testing.T) {
	first := time.Date(2026, 2, 2, 0, 0, 0, 0, time.UTC)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("want POST, got %s", r.Method)
		}
		var got offloadRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Errorf("decode request: %v", err)
		}
		if got.AgentID != "agent-x" {
			t.Errorf("agent id not forwarded: %q", got.AgentID)
		}
		// A misbehaving remote: claims weight-eligible with a folded score.
		_ = json.NewEncoder(w).Encode(routerscore.LedgerEntry{
			MinerHotkey:    "spoofed",
			AgentID:        "spoofed",
			WeightEligible: true,
			CombinedScore:  0.85,
			ShadowComposite: 0,
		})
	}))
	defer srv.Close()

	b := NewOffloadedBackend(srv.URL, true) // allowPrivate: httptest is loopback
	entry, err := b.Score(context.Background(), RouterSubmission{
		AgentID:   "agent-x",
		FirstSeen: first,
	})
	if err != nil {
		t.Fatalf("offload score errored: %v", err)
	}
	if entry.WeightEligible {
		t.Error("offload entry must be clamped to non-weight-eligible")
	}
	if entry.CombinedScore != 0 {
		t.Errorf("folded combined score must be zeroed, got %v", entry.CombinedScore)
	}
	// The remote's folded number is adopted as the shadow composite before zeroing.
	if entry.ShadowComposite != 0.85 {
		t.Errorf("shadow composite = %v, want 0.85 adopted from remote", entry.ShadowComposite)
	}
	// Identity is re-stamped from the local submission, not the remote's spoof.
	if entry.AgentID != "agent-x" || entry.MinerHotkey != "" {
		t.Errorf("identity not re-stamped from submission: %+v", entry)
	}
	if !first.Equal(entry.FirstSeen) {
		t.Errorf("first_seen not carried: %v", entry.FirstSeen)
	}
}

// TestOffloadedBackendRefusesRedirect confirms the client refuses a redirect a
// remote scorer might use to reach an internal address.
func TestOffloadedBackendRefusesRedirect(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://169.254.169.254/latest/meta-data/", http.StatusFound)
	}))
	defer srv.Close()

	b := NewOffloadedBackend(srv.URL, true)
	if _, err := b.Score(context.Background(), RouterSubmission{AgentID: "a"}); err == nil {
		t.Fatal("expected redirect to be refused")
	}
}
