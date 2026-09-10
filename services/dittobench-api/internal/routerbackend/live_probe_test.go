package routerbackend

// Throwaway live demonstration of the opt-in ("yes-and") router-project gate
// against the real reference router. Gated on DITTOBENCH_LIVE_ROUTER_URL so it
// never runs in CI; drive it by hand:
//
//	DITTOBENCH_LIVE_ROUTER_URL=http://127.0.0.1:8080 \
//	  go test ./internal/routerbackend/ -run TestLive -v
//
// It proves, end to end against the live server: present -> supported and the
// Dispatcher folds a SHADOW (weight_eligible=false, combined_score 0) entry; and
// an unreachable base -> benign ErrNotIncluded skip.

import (
	"context"
	"errors"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
)

func TestLiveReferenceRouterInclusion(t *testing.T) {
	base := os.Getenv("DITTOBENCH_LIVE_ROUTER_URL")
	if base == "" {
		t.Skip("set DITTOBENCH_LIVE_ROUTER_URL to run the live router demonstration")
	}
	ctx := context.Background()
	get := (&http.Client{Timeout: 5 * time.Second}).Get

	// present -> supported
	incl := routerharness.ProbeInclusion(ctx, base, get)
	if !incl.Included || incl.Status != routerharness.StatusSupported {
		t.Fatalf("present: got %+v, want Included=true supported", incl)
	}
	if incl.Contract != routerharness.RouterContractVersion || len(incl.Wires) == 0 {
		t.Fatalf("present: bad advertisement %+v", incl)
	}
	t.Logf("PRESENT -> supported: contract=%d wires=%v reason=%q",
		incl.Contract, incl.Wires, incl.Reason)

	// present -> Dispatcher folds a shadow entry
	disp := Dispatcher{Backend: NewLocalBackend(), Get: get}
	sub := RouterSubmission{
		MinerHotkey:   "5LiveDemoHotkey",
		AgentID:       "agent-live",
		RouterBaseURL: base,
		FirstSeen:     time.Now(),
	}
	res, entry, err := disp.Run(ctx, sub)
	if err != nil {
		t.Fatalf("included dispatch: unexpected err %v", err)
	}
	if !res.Included {
		t.Fatalf("included dispatch: result not included %+v", res)
	}
	if entry.WeightEligible {
		t.Fatalf("SHADOW invariant violated: entry is weight-eligible")
	}
	if entry.CombinedScore != 0 {
		t.Fatalf("v1 shadow: combined score must be 0, got %v", entry.CombinedScore)
	}
	t.Logf("PRESENT -> dispatched SHADOW entry: hotkey=%s weight_eligible=%v combined_score=%v harnesses=%d",
		entry.MinerHotkey, entry.WeightEligible, entry.CombinedScore, len(entry.Harnesses))

	// absent -> benign skip (point at a dead port)
	inclAbsent := routerharness.ProbeInclusion(ctx, "http://127.0.0.1:1", get)
	if inclAbsent.Included || inclAbsent.Status != routerharness.StatusUnsupported {
		t.Fatalf("absent: got %+v, want unsupported skip", inclAbsent)
	}
	_, _, errAbsent := disp.Run(ctx, RouterSubmission{RouterBaseURL: "http://127.0.0.1:1"})
	if !errors.Is(errAbsent, ErrNotIncluded) {
		t.Fatalf("absent dispatch: want ErrNotIncluded, got %v", errAbsent)
	}
	t.Logf("ABSENT -> benign skip: status=%q reason=%q err=ErrNotIncluded",
		inclAbsent.Status, inclAbsent.Reason)
}
