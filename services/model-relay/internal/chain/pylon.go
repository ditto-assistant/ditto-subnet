// Package chain holds the minimal Pylon client surface the relay needs: the
// /health liveness probe (latest block) and the validator-permit check used
// by POST /api/v1/inference/exchange.
package chain

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
)

// Prober is the health-probe seam. Implementations return nil when the chain
// dependency is reachable.
type Prober interface {
	ProbeLatestBlock(ctx context.Context) error
}

// PermitChecker resolves whether a hotkey holds a validator permit on the
// configured netuid. A non-nil error means the chain was unreachable (the
// endpoint answers 503); (false, nil) means registered-without-permit or
// not registered at all (401).
type PermitChecker interface {
	ValidatorPermit(ctx context.Context, hotkey string) (bool, error)
}

// PylonClient talks to the Pylon HTTP API. It mirrors the Python
// pylon_client URL construction. Latest-block reads are chain-global, while
// recent-neuron reads carry subnet and, in identity mode, identity context:
//
//	latest block:     GET {base}/api/v1/block/latest
//	recent neurons:   GET {base}/api/v1/subnet/{netuid}/block/recent/neurons
//	identity neurons: GET {base}/api/v1/identity/{name}/subnet/{netuid}/block/recent/neurons
//
// with the corresponding token as a Bearer Authorization header.
type PylonClient struct {
	baseURL string
	netuid  int
	token   string
	// identityName is empty in open-access mode.
	identityName string
	httpClient   *http.Client
}

// NewPylonClient builds the client from chain config. Open-access mode is
// preferred when both auth forms are configured, matching the Python client.
func NewPylonClient(cfg config.ChainConfig) *PylonClient {
	c := &PylonClient{
		baseURL: strings.TrimRight(cfg.PylonURL, "/"),
		netuid:  cfg.Netuid,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
	if cfg.OpenAccessToken != "" {
		c.token = cfg.OpenAccessToken
	} else {
		c.identityName = cfg.IdentityName
		c.token = cfg.IdentityToken
	}
	return c
}

func (c *PylonClient) latestBlockURL() string {
	return c.baseURL + "/api/v1/block/latest"
}

func (c *PylonClient) recentNeuronsURL() string {
	if c.identityName != "" {
		return fmt.Sprintf("%s/api/v1/identity/%s/subnet/%d/block/recent/neurons", c.baseURL, c.identityName, c.netuid)
	}
	return fmt.Sprintf("%s/api/v1/subnet/%d/block/recent/neurons", c.baseURL, c.netuid)
}

// ValidatorPermit fetches Pylon's cached recent-neurons snapshot (the same
// endpoint pylon_client's get_recent_neurons hits) and reports whether the
// hotkey is registered with validator_permit. Transport failures and non-2xx
// statuses are errors (chain unavailable); a registered hotkey without a
// permit and an unregistered hotkey both return (false, nil), matching
// _assert_validator_permitted's ValidatorAuthError mapping.
func (c *PylonClient) ValidatorPermit(ctx context.Context, hotkey string) (bool, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.recentNeuronsURL(), nil)
	if err != nil {
		return false, fmt.Errorf("build pylon request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return false, fmt.Errorf("pylon unreachable: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		_, _ = io.CopyN(io.Discard, resp.Body, 1<<16)
		return false, fmt.Errorf("pylon recent-neurons returned status %d", resp.StatusCode)
	}
	// GetNeuronsResponse: {"neurons": {"<hotkey>": {..., "validator_permit": bool}}}
	var payload struct {
		Neurons map[string]struct {
			ValidatorPermit bool `json:"validator_permit"`
		} `json:"neurons"`
	}
	// The metagraph payload for a large subnet runs to a few MB; bound it.
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(&payload); err != nil {
		return false, fmt.Errorf("pylon recent-neurons decode: %w", err)
	}
	neuron, registered := payload.Neurons[hotkey]
	if !registered {
		return false, nil
	}
	return neuron.ValidatorPermit, nil
}

// ProbeLatestBlock performs the same dependency check the Python /health
// endpoint performs via chain.get_latest_block(): any transport error or
// non-2xx status marks the chain down.
func (c *PylonClient) ProbeLatestBlock(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.latestBlockURL(), nil)
	if err != nil {
		return fmt.Errorf("build pylon request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("pylon unreachable: %w", err)
	}
	defer resp.Body.Close()
	// Drain a bounded amount so the connection can be reused.
	_, _ = io.CopyN(io.Discard, resp.Body, 1<<16)
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("pylon latest-block probe returned status %d", resp.StatusCode)
	}
	return nil
}
