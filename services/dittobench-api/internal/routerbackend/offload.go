// This file implements Part 2b of the router shadow track: OffloadedBackend as a
// real, configurable HTTP client to a remote scorer. When DITTOBENCH_ROUTER_OFFLOAD_URL
// is unset the backend keeps returning ErrOffloaded (so main() selects the offline
// replay LocalBackend and nothing calls out). When set, Score POSTs the submission
// to the remote scorer and parses a routerscore.LedgerEntry.
//
// The client is SSRF-guarded (netguard dial-time non-public-IP block + up-front
// URL validation), refuses redirects, bounds every request with a timeout, and
// logs no ticket, URL query, or secret. The result is defensively forced back to
// shadow: WeightEligible is cleared and CombinedScore zeroed regardless of what a
// remote returns, so a compromised or buggy scorer can never leak a weight-bearing
// number through the shadow path.
package routerbackend

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/netguard"
	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// offloadRequestTimeout bounds a single remote scoring call. The heavy path may
// be slow, but the shadow run must never hang a memory job.
const offloadRequestTimeout = 45 * time.Second

// maxOffloadResponseBytes caps the remote scorer's response so a hostile or buggy
// endpoint cannot exhaust memory.
const maxOffloadResponseBytes = 1 << 20 // 1 MiB

// httpOffloadClient is the offloaded backend's real HTTP implementation. Zero
// value is not usable; construct with NewOffloadedBackend.
type httpOffloadClient struct {
	url          string
	allowPrivate bool
	client       *http.Client
}

// NewOffloadedBackend selects the offloaded implementation from config. An empty
// url yields the unwired OffloadedBackend whose Score returns ErrOffloaded, so
// main() falls back to the replay LocalBackend and makes no outbound call. A set
// url yields an SSRF-guarded, redirect-refusing HTTP client to the remote scorer.
// allowPrivate mirrors the harness path (true only for local dev / sandbox).
func NewOffloadedBackend(url string, allowPrivate bool) RouterBackend {
	if url == "" {
		return OffloadedBackend{}
	}
	c := netguard.Client(allowPrivate)
	// Refuse redirects: a remote scorer must not be able to bounce us to an
	// internal address that would slip past the up-front URL validation.
	c.CheckRedirect = func(_ *http.Request, _ []*http.Request) error {
		return errors.New("routerbackend: offload scorer must not redirect")
	}
	return &httpOffloadClient{url: url, allowPrivate: allowPrivate, client: c}
}

// Score POSTs the submission to the remote scorer and parses a shadow ledger
// entry. Any error is returned so the caller swallows it (shadow never fails a
// memory run). The returned entry is defensively forced to shadow.
func (h *httpOffloadClient) Score(
	ctx context.Context, sub RouterSubmission,
) (routerscore.LedgerEntry, error) {
	if err := netguard.ValidateURLContext(ctx, h.url, h.allowPrivate); err != nil {
		// Do not echo the URL — it may carry a token in the path/query.
		return routerscore.LedgerEntry{}, fmt.Errorf("routerbackend: offload url rejected: %w", err)
	}

	body, err := json.Marshal(offloadRequest{
		MinerHotkey:   sub.MinerHotkey,
		AgentID:       sub.AgentID,
		RouterBaseURL: sub.RouterBaseURL,
		FirstSeen:     sub.FirstSeen,
	})
	if err != nil {
		return routerscore.LedgerEntry{}, fmt.Errorf("routerbackend: marshal offload request: %w", err)
	}

	reqCtx, cancel := context.WithTimeout(ctx, offloadRequestTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, h.url, bytes.NewReader(body))
	if err != nil {
		return routerscore.LedgerEntry{}, fmt.Errorf("routerbackend: build offload request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := h.client.Do(req)
	if err != nil {
		// net/url wraps the request URL (with any secret) into the error; strip it.
		return routerscore.LedgerEntry{}, errors.New("routerbackend: offload scorer request failed")
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return routerscore.LedgerEntry{}, fmt.Errorf("routerbackend: offload scorer returned status %d", resp.StatusCode)
	}

	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxOffloadResponseBytes))
	if err != nil {
		return routerscore.LedgerEntry{}, errors.New("routerbackend: read offload response failed")
	}
	var entry routerscore.LedgerEntry
	if err := json.Unmarshal(raw, &entry); err != nil {
		return routerscore.LedgerEntry{}, fmt.Errorf("routerbackend: parse offload response: %w", err)
	}

	// Defensive shadow clamp: never trust a remote to keep the invariant. The
	// entry identity is re-stamped from the local submission so a remote cannot
	// spoof a different agent, and any weight-bearing number is discarded — the
	// real measured aggregate rides ShadowComposite only.
	entry.MinerHotkey = sub.MinerHotkey
	entry.AgentID = sub.AgentID
	entry.RouterContractVersion = routerscore.RouterContractVersion
	entry.WeightEligible = false
	if entry.ShadowComposite == 0 && entry.CombinedScore != 0 {
		// A remote that only populated CombinedScore still yields a usable shadow
		// number; adopt it as the shadow composite before zeroing the folded field.
		entry.ShadowComposite = entry.CombinedScore
	}
	entry.CombinedScore = 0
	if entry.FirstSeen.IsZero() {
		entry.FirstSeen = sub.FirstSeen
	}
	return entry, nil
}

// offloadRequest is the JSON body POSTed to the remote scorer. It carries only
// public submission identity, never a secret.
type offloadRequest struct {
	MinerHotkey   string    `json:"miner_hotkey"`
	AgentID       string    `json:"agent_id"`
	RouterBaseURL string    `json:"router_base_url"`
	FirstSeen     time.Time `json:"first_seen"`
}

var _ RouterBackend = (*httpOffloadClient)(nil)
