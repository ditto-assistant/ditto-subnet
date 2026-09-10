package routerbackend

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
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
	// v1 shadow: adapters are stubs, so every slice forfeits -> combined 0, and
	// the entry is never weight-eligible.
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
		t.Errorf("v1 shadow stub must fold to 0, got %v", entry.CombinedScore)
	}
	if len(entry.Harnesses) != len(routerharness.Harnesses()) {
		t.Errorf("want one slice per harness, got %d", len(entry.Harnesses))
	}
	for _, h := range entry.Harnesses {
		if h.Operational {
			t.Errorf("harness %q should be non-operational in the v1 stub", h.Harness)
		}
	}
	if !first.Equal(entry.FirstSeen) {
		t.Errorf("first_seen not carried: %v", entry.FirstSeen)
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
