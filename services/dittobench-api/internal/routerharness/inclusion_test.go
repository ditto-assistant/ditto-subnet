package routerharness

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
)

// fakeGet builds an httpGetFn returning a canned response for base+HealthPath.
func fakeGet(status int, body string) func(string) (*http.Response, error) {
	return func(url string) (*http.Response, error) {
		if !strings.HasSuffix(url, HealthPath) {
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

func TestProbeInclusionSupported(t *testing.T) {
	got := ProbeInclusion(context.Background(), "http://127.0.0.1:8080", fakeGet(200, validHealth))
	if !got.Included || got.Status != StatusSupported {
		t.Fatalf("want included/supported, got %+v", got)
	}
	if got.Contract != RouterContractVersion {
		t.Errorf("contract = %d, want %d", got.Contract, RouterContractVersion)
	}
	if len(got.Wires) != 3 {
		t.Errorf("wires = %v, want 3", got.Wires)
	}
}

func TestProbeInclusionTrailingSlashNormalized(t *testing.T) {
	// A base URL with a trailing slash must still hit /router/health once.
	got := ProbeInclusion(context.Background(), "http://127.0.0.1:8080/", fakeGet(200, validHealth))
	if !got.Included {
		t.Fatalf("trailing slash should still probe cleanly, got %+v", got)
	}
}

func TestProbeInclusion404IsBenignSkip(t *testing.T) {
	got := ProbeInclusion(context.Background(), "http://x", fakeGet(404, "not found"))
	if got.Included || got.Status != StatusUnsupported {
		t.Fatalf("404 must be a benign unsupported skip, got %+v", got)
	}
}

func TestProbeInclusionNon200IsUnsupported(t *testing.T) {
	for _, code := range []int{500, 502, 400, 204} {
		got := ProbeInclusion(context.Background(), "http://x", fakeGet(code, "{}"))
		if got.Included || got.Status != StatusUnsupported {
			t.Errorf("status %d: want unsupported, got %+v", code, got)
		}
	}
}

func TestProbeInclusionMalformedBodyIsUnsupported(t *testing.T) {
	got := ProbeInclusion(context.Background(), "http://x", fakeGet(200, "not json{{"))
	if got.Included || got.Status != StatusUnsupported {
		t.Fatalf("malformed body must be unsupported, got %+v", got)
	}
}

func TestProbeInclusionWrongContractIsUnsupported(t *testing.T) {
	body := `{"status":"ok","supported_router_contract_versions":[2,3],"wires":["openai_chat"],"count_tokens":true}`
	got := ProbeInclusion(context.Background(), "http://x", fakeGet(200, body))
	if got.Included || got.Status != StatusUnsupported {
		t.Fatalf("unsupported contract must skip, got %+v", got)
	}
}

func TestProbeInclusionEmptyContractIsUnsupported(t *testing.T) {
	body := `{"status":"ok","supported_router_contract_versions":[],"wires":["openai_chat"],"count_tokens":true}`
	got := ProbeInclusion(context.Background(), "http://x", fakeGet(200, body))
	if got.Included {
		t.Fatalf("empty contract list must skip, got %+v", got)
	}
}

func TestProbeInclusionNoWiresIsUnsupported(t *testing.T) {
	body := `{"status":"ok","supported_router_contract_versions":[1],"wires":[],"count_tokens":true}`
	got := ProbeInclusion(context.Background(), "http://x", fakeGet(200, body))
	if got.Included || got.Status != StatusUnsupported {
		t.Fatalf("no wires must skip, got %+v", got)
	}
}

func TestProbeInclusionTransportErrorIsUnsupported(t *testing.T) {
	get := func(string) (*http.Response, error) { return nil, fmt.Errorf("connection refused") }
	got := ProbeInclusion(context.Background(), "http://127.0.0.1:9", get)
	if got.Included || got.Status != StatusUnsupported {
		t.Fatalf("transport error (server absent) must be a benign skip, got %+v", got)
	}
}

func TestProbeInclusionEmptyBaseIsUnsupported(t *testing.T) {
	got := ProbeInclusion(context.Background(), "  ", fakeGet(200, validHealth))
	if got.Included {
		t.Fatalf("empty base url must skip, got %+v", got)
	}
}
