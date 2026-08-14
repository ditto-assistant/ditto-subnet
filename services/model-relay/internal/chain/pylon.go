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
	"sync"
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

// RegistrationChecker resolves the current coldkey owner for a registered
// hotkey. An empty coldkey means the hotkey is not registered.
type RegistrationChecker interface {
	RegisteredColdkey(ctx context.Context, hotkey string) (string, error)
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
	mu           sync.Mutex
	neurons      map[string]recentNeuron
	fetchedAt    time.Time
	flight       *neuronFlight
	now          func() time.Time
	cacheTTL     time.Duration
}

// Pylon's recent-neurons response is already a block-cached snapshot. Holding
// it for one block locally avoids downloading and JSON-decoding the same
// multi-megabyte metagraph independently for every concurrent request while
// bounding the additional permit/owner staleness to one block.
const recentNeuronsCacheTTL = 12 * time.Second

type neuronFlight struct {
	done    chan struct{}
	neurons map[string]recentNeuron
	err     error
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
		now:      time.Now,
		cacheTTL: recentNeuronsCacheTTL,
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
	neuron, registered, err := c.recentNeuron(ctx, hotkey)
	if err != nil || !registered {
		return false, err
	}
	return neuron.ValidatorPermit, nil
}

type recentNeuron struct {
	ValidatorPermit bool   `json:"validator_permit"`
	Coldkey         string `json:"coldkey"`
}

func (c *PylonClient) recentNeuron(ctx context.Context, hotkey string) (recentNeuron, bool, error) {
	neurons, err := c.recentNeurons(ctx)
	if err != nil {
		return recentNeuron{}, false, err
	}
	neuron, registered := neurons[hotkey]
	return neuron, registered, nil
}

func (c *PylonClient) recentNeurons(ctx context.Context) (map[string]recentNeuron, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	now := c.now()
	c.mu.Lock()
	if c.neurons != nil && now.Sub(c.fetchedAt) < c.cacheTTL {
		neurons := c.neurons
		c.mu.Unlock()
		return neurons, nil
	}
	flight := c.flight
	if flight == nil {
		flight = &neuronFlight{done: make(chan struct{})}
		c.flight = flight
		go c.refreshRecentNeurons(flight)
	}
	c.mu.Unlock()

	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-flight.done:
		return flight.neurons, flight.err
	}
}

func (c *PylonClient) refreshRecentNeurons(flight *neuronFlight) {
	// A caller disconnect must not cancel the shared upstream fetch for every
	// waiter. The HTTP client's 10-second timeout remains the hard bound.
	neurons, err := c.fetchRecentNeurons(context.Background())
	c.mu.Lock()
	flight.neurons = neurons
	flight.err = err
	if err == nil {
		c.neurons = neurons
		c.fetchedAt = c.now()
	}
	c.flight = nil
	close(flight.done)
	c.mu.Unlock()
}

func (c *PylonClient) fetchRecentNeurons(ctx context.Context) (map[string]recentNeuron, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.recentNeuronsURL(), nil)
	if err != nil {
		return nil, fmt.Errorf("build pylon request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("pylon unreachable: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		_, _ = io.CopyN(io.Discard, resp.Body, 1<<16)
		return nil, fmt.Errorf("pylon recent-neurons returned status %d", resp.StatusCode)
	}
	// GetNeuronsResponse: {"neurons": {"<hotkey>": {..., "validator_permit": bool}}}
	var payload struct {
		Neurons map[string]recentNeuron `json:"neurons"`
	}
	// The metagraph payload for a large subnet runs to a few MB; bound it.
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<20)).Decode(&payload); err != nil {
		return nil, fmt.Errorf("pylon recent-neurons decode: %w", err)
	}
	return payload.Neurons, nil
}

// RegisteredColdkey mirrors ChainClient.get_registered_coldkey against the
// same recent-neurons snapshot used for validator permits.
func (c *PylonClient) RegisteredColdkey(ctx context.Context, hotkey string) (string, error) {
	neuron, registered, err := c.recentNeuron(ctx, hotkey)
	if err != nil || !registered {
		return "", err
	}
	return neuron.Coldkey, nil
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
