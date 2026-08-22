package netguard

import (
	"context"
	"errors"
	"net"
	"testing"
)

func TestIsDisallowedIP(t *testing.T) {
	cases := map[string]bool{
		"8.8.8.8":              false, // public
		"1.1.1.1":              false,
		"203.0.113.5":          false, // TEST-NET-3 is technically reserved but routable-looking; treated public here
		"127.0.0.1":            true,  // loopback
		"10.0.0.1":             true,  // RFC1918
		"192.168.1.1":          true,
		"172.16.5.4":           true,
		"169.254.169.254":      true,  // cloud metadata (link-local)
		"100.64.1.1":           true,  // CGNAT
		"0.0.0.0":              true,  // unspecified
		"224.0.0.1":            true,  // multicast
		"::1":                  true,  // ipv6 loopback
		"fd00::1":              true,  // ipv6 ULA (private)
		"fe80::1":              true,  // ipv6 link-local
		"2606:4700:4700::1111": false, // public ipv6
	}
	for ipStr, want := range cases {
		ip := net.ParseIP(ipStr)
		if got := isDisallowedIP(ip); got != want {
			t.Errorf("isDisallowedIP(%s) = %v, want %v", ipStr, got, want)
		}
	}
	if !isDisallowedIP(nil) {
		t.Error("nil IP must be disallowed")
	}
}

func TestValidateURLContextHonorsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := ValidateURLContext(ctx, "https://example.com/artifact", false); !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v", err)
	}
	if err := ValidateURLContext(ctx, "http://127.0.0.1/artifact", true); !errors.Is(err, context.Canceled) {
		t.Fatalf("allow-private cancellation error = %v", err)
	}
	if err := ValidateURLContext(nil, "https://example.com/artifact", false); err == nil {
		t.Fatal("nil context was accepted")
	}
}

func TestValidateURL(t *testing.T) {
	// allowPrivate bypass: anything goes.
	if err := ValidateURL("http://localhost:9000", true); err != nil {
		t.Errorf("allowPrivate should bypass: %v", err)
	}

	// Bad schemes / hosts.
	for _, bad := range []string{
		"ftp://example.com",
		"file:///etc/passwd",
		"gopher://x",
		"http://",
		"://nope",
	} {
		if err := ValidateURL(bad, false); err == nil {
			t.Errorf("expected error for %q", bad)
		}
	}

	// IP literals: private/loopback/metadata rejected.
	for _, bad := range []string{
		"http://127.0.0.1:9000",
		"http://10.1.2.3/run",
		"http://169.254.169.254/computeMetadata/v1/",
		"https://192.168.0.5",
		"http://[::1]:8080",
		"http://100.64.0.1",
	} {
		if err := ValidateURL(bad, false); err == nil {
			t.Errorf("expected SSRF rejection for %q", bad)
		}
	}

	// Public IP literal allowed.
	if err := ValidateURL("https://8.8.8.8/health", false); err != nil {
		t.Errorf("public IP should pass: %v", err)
	}
}

func TestClientBlocksPrivateDial(t *testing.T) {
	// With guarding on, a direct dial to a loopback address must fail in Control.
	c := Client(false)
	_, err := c.Get("http://127.0.0.1:0/")
	if err == nil {
		t.Fatal("expected guarded client to block loopback dial")
	}

	// With allowPrivate, the Control hook is absent (dial may still fail for
	// other reasons, but not because of the guard). Just ensure no panic.
	_ = Client(true)
}
