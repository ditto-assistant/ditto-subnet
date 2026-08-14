package chain

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/ditto-assistant/model-relay/internal/config"
)

func TestProbeLatestBlockUsesTheChainGlobalEndpoint(t *testing.T) {
	tests := []struct {
		name              string
		config            config.ChainConfig
		wantAuthorization string
	}{
		{
			name: "open access",
			config: config.ChainConfig{
				Netuid:          118,
				OpenAccessToken: "open-token",
			},
			wantAuthorization: "Bearer open-token",
		},
		{
			name: "identity",
			config: config.ChainConfig{
				Netuid:        118,
				IdentityName:  "validator",
				IdentityToken: "identity-token",
			},
			wantAuthorization: "Bearer identity-token",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var gotPath string
			var gotAuthorization string
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				gotPath = r.URL.Path
				gotAuthorization = r.Header.Get("Authorization")
				w.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()

			test.config.PylonURL = server.URL
			client := NewPylonClient(test.config)
			if err := client.ProbeLatestBlock(context.Background()); err != nil {
				t.Fatalf("ProbeLatestBlock() error = %v", err)
			}
			if gotPath != "/api/v1/block/latest" {
				t.Fatalf("request path = %q, want chain-global latest-block endpoint", gotPath)
			}
			if gotAuthorization != test.wantAuthorization {
				t.Fatalf("Authorization = %q, want %q", gotAuthorization, test.wantAuthorization)
			}
		})
	}
}

func TestRegisteredColdkeyUsesRecentNeuronOwner(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"neurons":{"5Hotkey":{"coldkey":"5Coldkey","validator_permit":false}}}`))
	}))
	defer server.Close()
	client := NewPylonClient(config.ChainConfig{PylonURL: server.URL, Netuid: 118, OpenAccessToken: "token"})
	owner, err := client.RegisteredColdkey(context.Background(), "5Hotkey")
	if err != nil || owner != "5Coldkey" {
		t.Fatalf("RegisteredColdkey() owner=%q err=%v", owner, err)
	}
	missing, err := client.RegisteredColdkey(context.Background(), "5Missing")
	if err != nil || missing != "" {
		t.Fatalf("missing owner=%q err=%v", missing, err)
	}
}
