package main

import (
	"fmt"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/ratelimit"
)

func TestClientIPTrustsOnlyProxyAppendedForwardedFor(t *testing.T) {
	cases := []struct {
		name      string
		hops      int
		forwarded []string
		want      string
	}{
		{"cloud run appends the real client after a spoofed value", 1, []string{"198.51.100.9, 203.0.113.7"}, "203.0.113.7"},
		{"a single proxy-written entry", 1, []string{"203.0.113.7"}, "203.0.113.7"},
		{"load balancer appends client then its own address", 2, []string{"198.51.100.9, 203.0.113.7, 34.117.0.1"}, "203.0.113.7"},
		{"repeated header lines are one list", 1, []string{"198.51.100.9", "203.0.113.7"}, "203.0.113.7"},
		{"no header falls back to the transport peer", 1, nil, "192.0.2.10"},
		{"header shorter than the trusted hops is not trusted", 2, []string{"198.51.100.9"}, "192.0.2.10"},
		{"a non-IP entry is not trusted", 1, []string{"198.51.100.9, not-an-ip"}, "192.0.2.10"},
		{"zero hops ignores the header entirely", 0, []string{"198.51.100.9, 203.0.113.7"}, "192.0.2.10"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("POST", "/v1/submit", nil)
			r.RemoteAddr = "192.0.2.10:44321"
			for _, value := range tc.forwarded {
				r.Header.Add("X-Forwarded-For", value)
			}
			if got := clientIPFromHops(r, tc.hops); got != tc.want {
				t.Fatalf("clientIPFromHops(%v, hops=%d) = %q, want %q", tc.forwarded, tc.hops, got, tc.want)
			}
		})
	}
}

// Rotating the client-written part of X-Forwarded-For must not mint a fresh
// rate-limit bucket: the key has to come from the proxy-appended entry.
func TestRotatingSpoofedForwardedForStaysInOneLimiterBucket(t *testing.T) {
	limiter := ratelimit.New(3, time.Hour)
	allowed := 0
	for i := 0; i < 20; i++ {
		r := httptest.NewRequest("POST", "/v1/submit", nil)
		r.RemoteAddr = "192.0.2.10:44321"
		r.Header.Set("X-Forwarded-For", fmt.Sprintf("198.51.100.%d, 203.0.113.7", i))
		if limiter.Allow(clientIPFromHops(r, 1)) {
			allowed++
		}
	}
	if allowed != 3 {
		t.Fatalf("20 requests from one client rotating a spoofed prefix: %d allowed, want the limit of 3", allowed)
	}
}

func TestParseTrustedProxyHops(t *testing.T) {
	cases := map[string]int{
		"":    1,
		" ":   1,
		"0":   0,
		"1":   1,
		"2":   2,
		"8":   8,
		"9":   1,
		"-1":  1,
		"two": 1,
	}
	for raw, want := range cases {
		if got := parseTrustedProxyHops(raw); got != want {
			t.Fatalf("parseTrustedProxyHops(%q) = %d, want %d", raw, got, want)
		}
	}
}
