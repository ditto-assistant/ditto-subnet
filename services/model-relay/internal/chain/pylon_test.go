package chain

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

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
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
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
	if got := calls.Load(); got != 1 {
		t.Fatalf("recent-neurons calls = %d, want one cached snapshot", got)
	}
}

func TestRecentNeuronsCollapsesConcurrentRefreshes(t *testing.T) {
	var calls atomic.Int32
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"neurons":{"5Hotkey":{"coldkey":"5Coldkey","validator_permit":true}}}`))
	}))
	defer server.Close()
	client := NewPylonClient(config.ChainConfig{PylonURL: server.URL, Netuid: 118, OpenAccessToken: "token"})

	const contenders = 32
	var wg sync.WaitGroup
	errors := make(chan error, contenders)
	wg.Add(contenders)
	for range contenders {
		go func() {
			defer wg.Done()
			permitted, err := client.ValidatorPermit(context.Background(), "5Hotkey")
			if err != nil {
				errors <- err
			} else if !permitted {
				errors <- fmt.Errorf("permit unexpectedly false")
			}
		}()
	}
	deadline := time.Now().Add(2 * time.Second)
	for calls.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	close(release)
	wg.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("recent-neurons calls = %d, want one single-flight refresh", got)
	}
}

func TestRecentNeuronsRefreshesAfterTTL(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{"neurons":{"5Hotkey":{"validator_permit":true}}}`))
	}))
	defer server.Close()
	client := NewPylonClient(config.ChainConfig{PylonURL: server.URL, Netuid: 118, OpenAccessToken: "token"})
	now := time.Unix(1_700_000_000, 0)
	client.now = func() time.Time { return now }

	if _, err := client.ValidatorPermit(context.Background(), "5Hotkey"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(recentNeuronsCacheTTL + time.Nanosecond)
	if _, err := client.ValidatorPermit(context.Background(), "5Hotkey"); err != nil {
		t.Fatal(err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("recent-neurons calls = %d, want refresh after TTL", got)
	}
}

func TestRecentNeuronsDoesNotCacheFailure(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) == 1 {
			http.Error(w, "temporary", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(`{"neurons":{"5Hotkey":{"validator_permit":true}}}`))
	}))
	defer server.Close()
	client := NewPylonClient(config.ChainConfig{PylonURL: server.URL, Netuid: 118, OpenAccessToken: "token"})

	if _, err := client.ValidatorPermit(context.Background(), "5Hotkey"); err == nil {
		t.Fatal("first failed refresh returned nil error")
	}
	if permitted, err := client.ValidatorPermit(context.Background(), "5Hotkey"); err != nil || !permitted {
		t.Fatalf("second refresh permit=%v err=%v", permitted, err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("recent-neurons calls = %d, want failed fetch plus retry", got)
	}
}

func TestRecentNeuronsCallerCancellationDoesNotPoisonFlight(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
		_, _ = w.Write([]byte(`{"neurons":{"5Hotkey":{"validator_permit":true}}}`))
	}))
	defer server.Close()
	client := NewPylonClient(config.ChainConfig{PylonURL: server.URL, Netuid: 118, OpenAccessToken: "token"})
	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() {
		_, err := client.ValidatorPermit(ctx, "5Hotkey")
		result <- err
	}()
	<-started
	cancel()
	if err := <-result; err != context.Canceled {
		t.Fatalf("cancelled caller error = %v, want context.Canceled", err)
	}

	close(release)
	if permitted, err := client.ValidatorPermit(context.Background(), "5Hotkey"); err != nil || !permitted {
		t.Fatalf("waiting caller permit=%v err=%v", permitted, err)
	}
}
