// Command egress-proxy is a minimal, fail-closed HTTPS CONNECT forward proxy
// with a hostname allowlist. It fronts the untrusted miner sandbox network so a
// harness's only permitted egress is to explicitly allowlisted hosts (e.g. the
// locked model relay's upstream, llm.chutes.ai:443). Anything not on the
// allowlist is refused.
//
// It is one of the two enforcement layers of the sandbox egress lockdown (see
// the Sandbox egress section in docs/model-lock.md): this proxy allowlists by
// hostname at CONNECT time (solving the CDN rotating-IP problem), and a host
// firewall
// separately DROPs any direct egress that bypasses the proxy — so a harness that
// ignores HTTPS_PROXY and dials the internet fails closed rather than leaking.
//
// Deliberately CONNECT-only: the only legitimate outbound is HTTPS to an
// allowlisted host (the host model gateway and loopback bypass the proxy via
// NO_PROXY). Plain-HTTP proxying and every non-CONNECT method are rejected, so
// there is no request-forwarding surface to abuse.
package main

import (
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	cfg := configFromEnv()
	p := newProxy(cfg)
	if len(p.allow) == 0 {
		log.Printf("egress-proxy: WARNING empty allowlist (EGRESS_PROXY_ALLOW) — every CONNECT will be denied (fail-closed)")
	} else {
		log.Printf("egress-proxy: listening on %s; allow hosts=%v ports=%v", cfg.addr, p.allow, sortedPorts(p.ports))
	}
	srv := &http.Server{
		Addr:    cfg.addr,
		Handler: p,
		// Bound the time to read the CONNECT request line + headers. The tunnel
		// itself is hijacked, so this does not cap an established connection.
		ReadHeaderTimeout: 15 * time.Second,
	}
	log.Fatal(srv.ListenAndServe())
}

type config struct {
	addr        string
	allow       []string
	ports       map[string]bool
	dialTimeout time.Duration
}

func configFromEnv() config {
	return config{
		addr:        envOr("EGRESS_PROXY_ADDR", ":3128"),
		allow:       parseList(os.Getenv("EGRESS_PROXY_ALLOW")),
		ports:       parsePorts(envOr("EGRESS_PROXY_ALLOW_PORTS", "443")),
		dialTimeout: envDuration("EGRESS_PROXY_DIAL_TIMEOUT", 10*time.Second),
	}
}

// proxy is the CONNECT handler. It is safe for concurrent use.
type proxy struct {
	allow       []string        // lower-cased allowlisted hostnames
	ports       map[string]bool // allowed CONNECT ports (string form)
	dialTimeout time.Duration
}

func newProxy(c config) *proxy {
	return &proxy{allow: c.allow, ports: c.ports, dialTimeout: c.dialTimeout}
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodConnect {
		// No plain-HTTP forwarding: the only legitimate egress is HTTPS CONNECT.
		http.Error(w, "only CONNECT is supported", http.StatusMethodNotAllowed)
		return
	}
	host, port, err := net.SplitHostPort(r.Host)
	if err != nil {
		http.Error(w, "malformed CONNECT target (want host:port)", http.StatusBadRequest)
		return
	}
	if !p.ports[port] || !hostAllowed(host, p.allow) {
		log.Printf("egress-proxy: DENY CONNECT %s:%s", host, port)
		http.Error(w, "egress to this host is not allowed", http.StatusForbidden)
		return
	}

	upstream, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), p.dialTimeout)
	if err != nil {
		log.Printf("egress-proxy: upstream dial %s:%s failed: %v", host, port, err)
		http.Error(w, "upstream dial failed", http.StatusBadGateway)
		return
	}
	defer upstream.Close()

	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijacking unsupported", http.StatusInternalServerError)
		return
	}
	client, _, err := hj.Hijack()
	if err != nil {
		http.Error(w, "hijack failed", http.StatusInternalServerError)
		return
	}
	defer client.Close()

	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}
	log.Printf("egress-proxy: ALLOW CONNECT %s:%s", host, port)
	tunnel(client, upstream)
}

// tunnel splices two connections until either side closes, then returns. Both
// conns are closed by the caller's defers, which unblocks the surviving copy.
func tunnel(a, b net.Conn) {
	done := make(chan struct{}, 2)
	cp := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		// Half-close the write side so the peer sees EOF promptly.
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		}
		done <- struct{}{}
	}
	go cp(a, b)
	go cp(b, a)
	<-done
	<-done
}

// hostAllowed reports whether host matches an allowlist entry exactly or as a
// subdomain of it (entry "chutes.ai" allows "chutes.ai" and
// "llm.chutes.ai", never "notchutes.ai" or "chutes.ai.evil.com").
func hostAllowed(host string, allow []string) bool {
	h := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
	for _, a := range allow {
		if h == a || strings.HasSuffix(h, "."+a) {
			return true
		}
	}
	return false
}

func parseList(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if v := strings.ToLower(strings.TrimSpace(part)); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func parsePorts(s string) map[string]bool {
	out := make(map[string]bool)
	for _, part := range strings.Split(s, ",") {
		if v := strings.TrimSpace(part); v != "" {
			out[v] = true
		}
	}
	return out
}

func sortedPorts(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func envOr(name, def string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return def
}

func envDuration(name string, def time.Duration) time.Duration {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return def
}
