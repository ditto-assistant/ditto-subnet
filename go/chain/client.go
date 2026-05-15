package chain

import (
	"context"
	"fmt"
	"net/http"
	"time"
)

// Client is the Go counterpart of ditto.chain.client.ChainClient. It
// wraps a small Pylon HTTP client and (eventually) a substrate
// gap-filler for the ExtrinsicSuccess/Failed event lookup that Pylon
// does not surface.
//
// Construction is synchronous (Pylon connections are made per-call), so
// the validator binary can hold a *Client for the lifetime of a tempo
// and only pay the dial cost on actual chain reads. Close() is safe to
// call on a zero-value Client.
type Client struct {
	cfg    Config
	pylon  *pylonClient
	closed bool
}

// New constructs a Client. Pass an explicit *http.Client when callers
// want to control TLS, proxies, or timeouts; otherwise a 30s default
// is used.
func New(cfg Config, hc *http.Client) (*Client, error) {
	if cfg.SubtensorNetwork == "" {
		cfg.SubtensorNetwork = "finney"
	}
	if cfg.ArchiveBlocksCutoff == 0 {
		cfg.ArchiveBlocksCutoff = 300
	}
	pc, err := newPylonClient(cfg, hc)
	if err != nil {
		return nil, err
	}
	return &Client{cfg: cfg, pylon: pc}, nil
}

// Close releases any resources held by the underlying transport. The
// net/http transport pools connections globally so this is a no-op
// today; defined for parity with the Python context manager and so a
// future transport (e.g. a long-lived substrate WS connection) has a
// place to shut down.
func (c *Client) Close() error {
	if c == nil {
		return nil
	}
	c.closed = true
	return nil
}

// Netuid returns the subnet id this client was configured for.
func (c *Client) Netuid() int { return c.cfg.Netuid }

// RecentNeurons fetches the cached metagraph for a netuid. Mirrors
// ChainClient.get_recent_neurons in Python; the map is keyed by hotkey
// so callers can resolve "scored a miner's challenge" -> hotkey without
// a second pass.
func (c *Client) RecentNeurons(ctx context.Context, netuid int) (map[string]NeuronInfo, error) {
	if err := c.guard(); err != nil {
		return nil, err
	}
	return c.pylon.recentNeurons(ctx, netuid)
}

// LatestBlock returns Pylon's view of the chain head. Mirrors
// ChainClient.get_latest_block in Python.
func (c *Client) LatestBlock(ctx context.Context) (BlockInfo, error) {
	if err := c.guard(); err != nil {
		return BlockInfo{}, err
	}
	return c.pylon.latestBlock(ctx)
}

// Extrinsic fetches a single extrinsic by (block, index). Succeeded is
// nil on the returned value because Pylon's response omits the block
// hash; resolve success separately with CheckExtrinsicSuccess when the
// hash is available.
func (c *Client) Extrinsic(ctx context.Context, blockNum, idx int) (ExtrinsicInfo, error) {
	if err := c.guard(); err != nil {
		return ExtrinsicInfo{}, err
	}
	return c.pylon.extrinsic(ctx, blockNum, idx)
}

// PutWeights submits a per-miner weight vector. Pylon normalises the
// weights and handles the commit-reveal / direct-emission decision from
// subnet hyperparameters; this call returns once Pylon accepts the
// submission, not once the extrinsic is finalised.
func (c *Client) PutWeights(ctx context.Context, weights map[string]float64) error {
	if err := c.guard(); err != nil {
		return err
	}
	return c.pylon.putWeights(ctx, weights)
}

// guard returns an error when the client was closed.
func (c *Client) guard() error {
	if c == nil {
		return fmt.Errorf("%w: nil Client", ErrChainConnection)
	}
	if c.closed {
		return fmt.Errorf("%w: Client is closed", ErrChainConnection)
	}
	return nil
}

// DefaultHTTPClient returns an http.Client suitable for Pylon calls. It
// is exported so the validator binary can share one across multiple
// Clients (one per netuid, say) without re-deriving transport settings.
func DefaultHTTPClient() *http.Client {
	return &http.Client{Timeout: 30 * time.Second}
}
