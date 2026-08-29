// Package netguard hardens the validator against SSRF via caller-supplied
// harness URLs. The hosted practice API is public + unauthenticated and makes
// outbound requests to whatever harness_url a submitter provides, so an attacker
// could otherwise point it at internal/cloud-metadata addresses.
//
// Two layers, both gated by allowPrivate (false in production, true for local
// dev + the Docker sandbox whose containers live on loopback):
//
//   - ValidateURL rejects non-http(s) schemes and hosts that resolve to a
//     non-public address, up front in the submit handler.
//   - Client returns an *http.Client whose dialer re-checks the *connected* IP,
//     defeating DNS-rebinding (a host that passes ValidateURL but resolves to a
//     private IP at connect time).
package netguard

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"syscall"
	"time"
)

// isDisallowedIP reports whether ip is anything other than a public unicast
// address: loopback, RFC1918 private, link-local (incl. the 169.254.169.254
// cloud metadata IP), unspecified, multicast, or CGNAT (100.64.0.0/10).
func isDisallowedIP(ip net.IP) bool {
	if ip == nil {
		return true
	}
	if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsInterfaceLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified() {
		return true
	}
	// CGNAT 100.64.0.0/10 (RFC 6598) — not covered by IsPrivate.
	if ip4 := ip.To4(); ip4 != nil && ip4[0] == 100 && ip4[1]&0xc0 == 64 {
		return true
	}
	return false
}

// ValidateURL checks that raw is an http(s) URL whose host resolves only to
// public addresses. When allowPrivate is true the check is skipped (local dev /
// sandbox). It is the up-front guard; Client provides the dial-time backstop.
func ValidateURL(raw string, allowPrivate bool) error {
	return ValidateURLContext(context.Background(), raw, allowPrivate)
}

// ValidateURLContext is ValidateURL with caller-bounded DNS resolution.
func ValidateURLContext(ctx context.Context, raw string, allowPrivate bool) error {
	if ctx == nil {
		return fmt.Errorf("harness_url validation context is required")
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("harness_url validation interrupted: %w", err)
	}
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("invalid harness_url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("harness_url scheme must be http or https")
	}
	host := u.Hostname()
	if host == "" {
		return fmt.Errorf("harness_url has no host")
	}
	if allowPrivate {
		return nil
	}
	if ip := net.ParseIP(host); ip != nil {
		if isDisallowedIP(ip) {
			return fmt.Errorf("harness_url host %s is not a public address", host)
		}
		return nil
	}
	addresses, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return fmt.Errorf("resolve harness_url host %s: %w", host, err)
	}
	if len(addresses) == 0 {
		return fmt.Errorf("harness_url host %s has no addresses", host)
	}
	for _, address := range addresses {
		if isDisallowedIP(address.IP) {
			return fmt.Errorf("harness_url host %s resolves to a non-public address", host)
		}
	}
	return nil
}

// Client returns an *http.Client for outbound harness calls. When allowPrivate
// is false the dialer rejects connections to non-public IPs at dial time, so a
// host that passes ValidateURL but rebinds to a private IP still can't be hit.
// No client-level timeout — callers bound each request with a context.
func Client(allowPrivate bool) *http.Client {
	d := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	if !allowPrivate {
		d.Control = func(_, address string, _ syscall.RawConn) error {
			host, _, err := net.SplitHostPort(address)
			if err != nil {
				return err
			}
			if isDisallowedIP(net.ParseIP(host)) {
				return fmt.Errorf("blocked connection to non-public address %s", address)
			}
			return nil
		}
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		return d.DialContext(ctx, network, addr)
	}
	return &http.Client{Transport: tr}
}
