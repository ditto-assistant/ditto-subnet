package routerbackend

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerharness"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// fakeGet builds an httpGetFn returning a canned response for base+HealthPath.
func fakeGet(status int, body string) func(string) (*http.Response, error) {
	return func(url string) (*http.Response, error) {
		if !strings.HasSuffix(url, routerharness.HealthPath) {
			return nil, fmt.Errorf("unexpected url %q", url)
		}
		return &http.Response{
			StatusCode: status,
			Body:       io.NopCloser(bytes.NewBufferString(body)),
		}, nil
	}
}

const validHealth = `{"status":"ok","supported_router_contract_versions":[1],` +
	`"wires":["anthropic_messages","openai_chat","openai_responses"],"count_tokens":true}`

func TestDispatcherSkipsWhenNotIncluded(t *testing.T) {
	d := Dispatcher{Backend: OffloadedBackend{}, Get: fakeGet(404, "nope")}
	incl, entry, err := d.Run(context.Background(), RouterSubmission{
		MinerHotkey:   "hk",
		RouterBaseURL: "http://x",
	})
	if !errors.Is(err, ErrNotIncluded) {
		t.Fatalf("no router project must yield ErrNotIncluded, got %v", err)
	}
	if incl.Included || incl.Status != routerharness.StatusUnsupported {
		t.Errorf("inclusion result should be a benign skip, got %+v", incl)
	}
	// The backend was never dispatched, so no offloaded error leaked through.
	if entry.MinerHotkey != "" {
		t.Errorf("skip must return a zero entry, got %+v", entry)
	}
}

func TestDispatcherDispatchesWhenIncluded(t *testing.T) {
	d := Dispatcher{Backend: NewLocalBackend(), Get: fakeGet(200, validHealth)}
	first := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	incl, entry, err := d.Run(context.Background(), RouterSubmission{
		MinerHotkey:   "hk",
		AgentID:       "11111111-1111-1111-1111-111111111111",
		RouterBaseURL: "http://127.0.0.1:8080",
		FirstSeen:     first,
	})
	if err != nil {
		t.Fatalf("included submission should score without error, got %v", err)
	}
	if !incl.Included {
		t.Fatalf("expected included, got %+v", incl)
	}
	// v1 shadow: the LocalBackend replays the embedded offline corpus, so the
	// harnesses ARE operational and produce a real, non-zero shadow composite —
	// but the entry stays never-weight-eligible and its folded CombinedScore is 0.
	if entry.MinerHotkey != "hk" || entry.AgentID != "11111111-1111-1111-1111-111111111111" {
		t.Errorf("entry identity not stamped: %+v", entry)
	}
	if entry.RouterContractVersion != routerscore.RouterContractVersion {
		t.Errorf("contract = %d, want %d", entry.RouterContractVersion, routerscore.RouterContractVersion)
	}
	if entry.WeightEligible {
		t.Error("shadow entry must never be weight-eligible")
	}
	if entry.CombinedScore != 0 {
		t.Errorf("shadow entry must fold to 0 (no emission impact), got %v", entry.CombinedScore)
	}
	// The measured aggregate rides ShadowComposite and must be a real, non-zero
	// number the replay produced (the dashboard's router_shadow_composite).
	if entry.ShadowComposite <= 0 || entry.ShadowComposite > 1 {
		t.Errorf("shadow composite = %v, want a real value in (0,1]", entry.ShadowComposite)
	}
	if len(entry.Harnesses) != len(routerharness.Harnesses()) {
		t.Errorf("want one slice per harness, got %d", len(entry.Harnesses))
	}
	operational := 0
	for _, h := range entry.Harnesses {
		if h.Operational {
			operational++
			if h.Efficiency <= 0 || h.Efficiency > 1 {
				t.Errorf("operational harness %q efficiency = %v, want (0,1]", h.Harness, h.Efficiency)
			}
		}
	}
	if operational != len(routerharness.Harnesses()) {
		t.Errorf("replay corpus should make every harness operational, got %d/%d", operational, len(routerharness.Harnesses()))
	}
	if !first.Equal(entry.FirstSeen) {
		t.Errorf("first_seen not carried: %v", entry.FirstSeen)
	}

	// Determinism: replaying the same corpus yields an identical composite.
	_, entry2, err := d.Run(context.Background(), RouterSubmission{
		MinerHotkey:   "hk",
		AgentID:       "11111111-1111-1111-1111-111111111111",
		RouterBaseURL: "http://127.0.0.1:8080",
		FirstSeen:     first,
	})
	if err != nil {
		t.Fatalf("second replay errored: %v", err)
	}
	if entry2.ShadowComposite != entry.ShadowComposite {
		t.Errorf("replay not deterministic: %v vs %v", entry2.ShadowComposite, entry.ShadowComposite)
	}
}

func TestOffloadedBackendReportsOffloaded(t *testing.T) {
	_, err := OffloadedBackend{}.Score(context.Background(), RouterSubmission{})
	if !errors.Is(err, ErrOffloaded) {
		t.Fatalf("offloaded backend must return ErrOffloaded, got %v", err)
	}
}

func TestDispatcherNilGetUsesDefaultClient(t *testing.T) {
	// A nil getter must not panic; it falls back to the default client, which
	// (with no server) yields a benign unsupported skip rather than a crash.
	d := Dispatcher{Backend: OffloadedBackend{}}
	_, _, err := d.Run(context.Background(), RouterSubmission{
		RouterBaseURL: "http://127.0.0.1:1", // nothing listening
	})
	if !errors.Is(err, ErrNotIncluded) {
		t.Fatalf("unreachable server must be a benign skip, got %v", err)
	}
}

// A harness that accepts the probe and then stalls must not hold the run: the
// probe runs before the finished memory run is recorded, and the job context has
// no deadline of its own.
func TestProbeClientBoundsAStalledHarness(t *testing.T) {
	for _, tc := range []struct {
		name    string
		handler http.HandlerFunc
	}{
		{"no response headers", func(_ http.ResponseWriter, r *http.Request) {
			<-r.Context().Done()
		}},
		{"stalled advertisement body", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ok",`))
			w.(http.Flusher).Flush()
			<-r.Context().Done()
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(tc.handler)
			defer srv.Close()
			d := Dispatcher{
				Backend: NewLocalBackend(),
				Get:     newProbeClient(true, 200*time.Millisecond).Get,
			}

			done := make(chan error, 1)
			go func() {
				_, _, err := d.Run(context.Background(), RouterSubmission{
					MinerHotkey:   "hk",
					RouterBaseURL: srv.URL,
				})
				done <- err
			}()
			select {
			case err := <-done:
				if !errors.Is(err, ErrNotIncluded) {
					t.Fatalf("a stalled probe must be a benign skip, got %v", err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("inclusion probe did not return for a stalled harness")
			}
		})
	}
}

func TestNewProbeClientIsBounded(t *testing.T) {
	if got := NewProbeClient(false).Timeout; got != probeTimeout || got <= 0 {
		t.Fatalf("probe client timeout = %v, want %v", got, probeTimeout)
	}
}
