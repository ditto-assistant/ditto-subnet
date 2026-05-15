package chain

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// pylonClient is a small JSON-over-HTTP client against Pylon's REST
// surface. It is kept private; callers go through Client which composes
// it with the substrate-gap helpers.
//
// The endpoint paths below mirror the Python pylon_client SDK as of
// 2026-05. They are stable as long as the SDK's call-shape stays
// stable; integration tests against a live Pylon are the source of truth
// for what actually goes over the wire.
type pylonClient struct {
	baseURL  *url.URL
	identity string // "<name>:<token>" header value
	http     *http.Client
}

const (
	pathRecentNeurons = "/v1/open_access/recent_neurons/%d"
	pathLatestBlock   = "/v1/open_access/block/latest"
	pathExtrinsic     = "/v1/open_access/extrinsic/%d/%d"
	pathPutWeights    = "/v1/identity/put_weights"
)

func newPylonClient(cfg Config, hc *http.Client) (*pylonClient, error) {
	if cfg.PylonURL == "" {
		return nil, fmt.Errorf("%w: PylonURL is required", ErrChainConnection)
	}
	u, err := url.Parse(cfg.PylonURL)
	if err != nil {
		return nil, fmt.Errorf("%w: invalid PylonURL %q: %v", ErrChainConnection, cfg.PylonURL, err)
	}
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	return &pylonClient{
		baseURL:  u,
		identity: cfg.IdentityName + ":" + cfg.IdentityToken,
		http:     hc,
	}, nil
}

// recentNeurons fetches the cached metagraph for “netuid“. The response
// body is a JSON object “{"neurons": {"<hotkey>": {NeuronInfo}, ...}}“
// matching the Python SDK's “GetNeuronsResponse“.
func (c *pylonClient) recentNeurons(ctx context.Context, netuid int) (map[string]NeuronInfo, error) {
	path := fmt.Sprintf(pathRecentNeurons, netuid)
	var payload struct {
		Neurons map[string]NeuronInfo `json:"neurons"`
	}
	if err := c.do(ctx, http.MethodGet, path, nil, &payload); err != nil {
		return nil, err
	}
	out := make(map[string]NeuronInfo, len(payload.Neurons))
	for hk, n := range payload.Neurons {
		// Ensure NeuronInfo.Hotkey is authoritative (the map key wins
		// when the inner field is missing or mismatched; this mirrors
		// the Python ``NeuronInfo.from_pylon`` override).
		n.Hotkey = hk
		out[hk] = n
	}
	return out, nil
}

func (c *pylonClient) latestBlock(ctx context.Context) (BlockInfo, error) {
	var out BlockInfo
	if err := c.do(ctx, http.MethodGet, pathLatestBlock, nil, &out); err != nil {
		return BlockInfo{}, err
	}
	return out, nil
}

func (c *pylonClient) extrinsic(ctx context.Context, blockNum, idx int) (ExtrinsicInfo, error) {
	var out ExtrinsicInfo
	path := fmt.Sprintf(pathExtrinsic, blockNum, idx)
	if err := c.do(ctx, http.MethodGet, path, nil, &out); err != nil {
		return ExtrinsicInfo{}, err
	}
	return out, nil
}

// putWeightsBody is the request body for the put_weights endpoint. Pylon
// normalises the values so the input map need not sum to 1.
type putWeightsBody struct {
	Weights map[string]float64 `json:"weights"`
}

func (c *pylonClient) putWeights(ctx context.Context, weights map[string]float64) error {
	body, err := json.Marshal(putWeightsBody{Weights: weights})
	if err != nil {
		return fmt.Errorf("%w: marshal weights: %v", ErrChainConnection, err)
	}
	return c.do(ctx, http.MethodPost, pathPutWeights, bytes.NewReader(body), nil)
}

// do issues a single Pylon HTTP request. dst, when non-nil, receives the
// JSON-decoded response body. Empty 2xx bodies are tolerated.
func (c *pylonClient) do(ctx context.Context, method, path string, body io.Reader, dst any) error {
	u := *c.baseURL
	u.Path = strings.TrimRight(u.Path, "/") + path

	req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
	if err != nil {
		return fmt.Errorf("%w: build request: %v", ErrChainConnection, err)
	}
	req.Header.Set("Accept", "application/json")
	if c.identity != ":" {
		req.Header.Set("Pylon-Identity", c.identity)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.http.Do(req)
	if err != nil {
		if isTimeout(err) {
			return fmt.Errorf("%w: %s %s: %v", ErrChainTimeout, method, path, err)
		}
		return fmt.Errorf("%w: %s %s: %v", ErrChainConnection, method, path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return fmt.Errorf("%w: %s %s", ErrExtrinsicNotFound, method, path)
	}
	if resp.StatusCode == http.StatusRequestTimeout || resp.StatusCode == http.StatusGatewayTimeout {
		return fmt.Errorf("%w: %s %s returned %d", ErrChainTimeout, method, path, resp.StatusCode)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		buf, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("%w: %s %s returned %d: %s",
			ErrChainConnection, method, path, resp.StatusCode, strings.TrimSpace(string(buf)))
	}
	if dst == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		return nil
	}
	if err := json.NewDecoder(resp.Body).Decode(dst); err != nil {
		return fmt.Errorf("%w: decode %s %s: %v", ErrChainConnection, method, path, err)
	}
	return nil
}

// isTimeout reports whether err is a transport-level timeout. We accept
// both context.DeadlineExceeded and net.Error.Timeout() since net/http
// surfaces both depending on where the deadline expires.
func isTimeout(err error) bool {
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var ne interface{ Timeout() bool }
	if errors.As(err, &ne) {
		return ne.Timeout()
	}
	return false
}

// substrateURL resolves the WebSocket URL for the configured network. It
// is exported indirectly via Config so callers can override with a full
// URL when running against a private node.
func substrateURL(network string) string {
	switch strings.ToLower(network) {
	case "", "finney":
		return "wss://entrypoint-finney.opentensor.ai:443"
	case "test":
		return "wss://test.finney.opentensor.ai:443"
	case "local":
		return "ws://127.0.0.1:9944"
	default:
		return network
	}
}

// formatInt is small helper kept here to avoid pulling strconv into
// downstream files.
func formatInt(i int) string { return strconv.Itoa(i) }
