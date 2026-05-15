package chain

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

// newMockPylon spins up an httptest server that answers a fixed set of
// Pylon endpoints. The handler is parametrised so tests can override
// individual responses without rebuilding the whole map.
func newMockPylon(t *testing.T, routes map[string]http.HandlerFunc) (*Client, *httptest.Server) {
	t.Helper()
	mux := http.NewServeMux()
	for path, h := range routes {
		mux.HandleFunc(path, h)
	}
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	c, err := New(Config{
		PylonURL:      srv.URL,
		IdentityName:  "test",
		IdentityToken: "token",
		Netuid:        118,
	}, srv.Client())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c, srv
}

func TestRecentNeurons_DecodesAndKeysByHotkey(t *testing.T) {
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{
		"/v1/open_access/recent_neurons/118": func(w http.ResponseWriter, r *http.Request) {
			if got := r.Header.Get("Pylon-Identity"); got != "test:token" {
				t.Errorf("missing Pylon-Identity, got %q", got)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"neurons": map[string]any{
					"5HpA": map[string]any{
						"coldkey":          "5Cold",
						"uid":              7,
						"stake":            12.5,
						"active":           true,
						"validator_permit": true,
					},
				},
			})
		},
	})
	got, err := c.RecentNeurons(context.Background(), 118)
	if err != nil {
		t.Fatalf("RecentNeurons: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 neuron, got %d", len(got))
	}
	n := got["5HpA"]
	if n.Hotkey != "5HpA" || n.UID != 7 || !n.ValidatorPermit || n.Stake != 12.5 {
		t.Fatalf("decoded shape wrong: %+v", n)
	}
}

func TestLatestBlock_HappyPath(t *testing.T) {
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{
		"/v1/open_access/block/latest": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"number": 42, "hash": "0xabc", "timestamp": 1715802000}`))
		},
	})
	b, err := c.LatestBlock(context.Background())
	if err != nil {
		t.Fatalf("LatestBlock: %v", err)
	}
	if b.Number != 42 || b.Hash != "0xabc" || b.Timestamp != 1715802000 {
		t.Fatalf("wrong block: %+v", b)
	}
}

func TestExtrinsic_NotFoundMapsToErrExtrinsicNotFound(t *testing.T) {
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{
		"/v1/open_access/extrinsic/100/0": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
		},
	})
	_, err := c.Extrinsic(context.Background(), 100, 0)
	if !errors.Is(err, ErrExtrinsicNotFound) {
		t.Fatalf("expected ErrExtrinsicNotFound, got %v", err)
	}
}

func TestPutWeights_PostsBody(t *testing.T) {
	var received map[string]any
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{
		"/v1/identity/put_weights": func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST, got %s", r.Method)
			}
			if got := r.Header.Get("Content-Type"); got != "application/json" {
				t.Errorf("missing JSON content-type, got %q", got)
			}
			_ = json.NewDecoder(r.Body).Decode(&received)
			w.WriteHeader(http.StatusOK)
		},
	})
	weights := map[string]float64{"5HpA": 0.7, "5Bob": 0.3}
	if err := c.PutWeights(context.Background(), weights); err != nil {
		t.Fatalf("PutWeights: %v", err)
	}
	wmap, ok := received["weights"].(map[string]any)
	if !ok || len(wmap) != 2 {
		t.Fatalf("expected weights map, got %v", received)
	}
}

func TestPutWeights_ServerErrorMapsToConnectionError(t *testing.T) {
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{
		"/v1/identity/put_weights": func(w http.ResponseWriter, _ *http.Request) {
			http.Error(w, "validator permit missing", http.StatusForbidden)
		},
	})
	err := c.PutWeights(context.Background(), map[string]float64{"5HpA": 1.0})
	if !errors.Is(err, ErrChainConnection) {
		t.Fatalf("expected ErrChainConnection, got %v", err)
	}
}

func TestCheckExtrinsicSuccess_NotImplemented(t *testing.T) {
	c, _ := newMockPylon(t, map[string]http.HandlerFunc{})
	_, err := c.CheckExtrinsicSuccess("0xabc", 0)
	if !errors.Is(err, ErrNotImplemented) {
		t.Fatalf("expected ErrNotImplemented, got %v", err)
	}
}

func TestNew_RequiresPylonURL(t *testing.T) {
	if _, err := New(Config{}, nil); !errors.Is(err, ErrChainConnection) {
		t.Fatalf("expected ErrChainConnection for empty PylonURL, got %v", err)
	}
}

func TestSubstrateURL_KnownNetworks(t *testing.T) {
	cases := map[string]string{
		"":         "wss://entrypoint-finney.opentensor.ai:443",
		"finney":   "wss://entrypoint-finney.opentensor.ai:443",
		"test":     "wss://test.finney.opentensor.ai:443",
		"local":    "ws://127.0.0.1:9944",
		"ws://x:1": "ws://x:1",
	}
	for input, want := range cases {
		if got := substrateURL(input); got != want {
			t.Errorf("substrateURL(%q) = %q, want %q", input, got, want)
		}
	}
}

// Touch formatInt so the helper isn't reported as dead code while it is
// kept around for the substrate gap follow-up.
func TestFormatInt(t *testing.T) {
	if got := formatInt(42); got != "42" {
		t.Fatalf("formatInt(42) = %q", got)
	}
}
