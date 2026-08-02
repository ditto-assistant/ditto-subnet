package main

import (
	"bufio"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHostAllowed(t *testing.T) {
	allow := parseList("chutes.ai, Example.COM")
	cases := []struct {
		host string
		want bool
	}{
		{"chutes.ai", true},
		{"api.chutes.ai", true},       // subdomain
		{"Chutes.ai", true},           // case-insensitive
		{"chutes.ai.", true},          // trailing dot
		{"example.com", true},         // allowlist entry was mixed-case
		{"notchutes.ai", false},       // suffix but not a dot-boundary subdomain
		{"chutes.ai.evil.com", false}, // evil suffix trick
		{"evil.com", false},
		{"", false},
	}
	for _, c := range cases {
		if got := hostAllowed(c.host, allow); got != c.want {
			t.Errorf("hostAllowed(%q) = %v, want %v", c.host, got, c.want)
		}
	}
}

func TestEmptyAllowlistDeniesAll(t *testing.T) {
	if hostAllowed("chutes.ai", parseList("")) {
		t.Fatal("empty allowlist must deny every host (fail-closed)")
	}
}

func TestParsePorts(t *testing.T) {
	p := parsePorts("443, 8443 ,")
	if !p["443"] || !p["8443"] || len(p) != 2 {
		t.Fatalf("parsePorts got %v", p)
	}
}

func TestServeHTTP_NonConnectRejected(t *testing.T) {
	p := newProxy(config{allow: parseList("chutes.ai"), ports: parsePorts("443")})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "http://chutes.ai/", nil)
	p.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET got %d, want 405", rec.Code)
	}
}

func TestServeHTTP_DeniedHostForbidden(t *testing.T) {
	p := newProxy(config{allow: parseList("chutes.ai"), ports: parsePorts("443")})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodConnect, "//evil.com:443", nil)
	req.Host = "evil.com:443"
	p.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("CONNECT to denied host got %d, want 403", rec.Code)
	}
}

func TestServeHTTP_DeniedPortForbidden(t *testing.T) {
	// Host is allowlisted but the port is not (only 443 permitted).
	p := newProxy(config{allow: parseList("chutes.ai"), ports: parsePorts("443")})
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodConnect, "//chutes.ai:22", nil)
	req.Host = "chutes.ai:22"
	p.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("CONNECT to denied port got %d, want 403", rec.Code)
	}
}

// TestTunnel_EndToEnd starts the real proxy and a stub upstream, then drives a
// CONNECT through it and checks bytes flow both ways for an allowed host and are
// refused for a denied one.
func TestTunnel_EndToEnd(t *testing.T) {
	// Stub upstream: echoes a banner then whatever it receives.
	upstream, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer upstream.Close()
	go func() {
		for {
			c, err := upstream.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				fmt.Fprint(c, "HELLO\n")
				buf := make([]byte, 64)
				n, _ := c.Read(buf)
				c.Write(buf[:n]) // echo
			}(c)
		}
	}()
	_, upPort, _ := net.SplitHostPort(upstream.Addr().String())

	// Proxy allowlisting 127.0.0.1 on the upstream's ephemeral port.
	p := newProxy(config{
		allow:       parseList("127.0.0.1"),
		ports:       parsePorts(upPort),
		dialTimeout: 5 * time.Second,
	})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	srv := &http.Server{Handler: p, ReadHeaderTimeout: 5 * time.Second}
	go srv.Serve(ln)
	defer srv.Close()

	target := "127.0.0.1:" + upPort

	// Allowed CONNECT: expect 200 then the upstream banner, and echo round-trips.
	conn, err := net.DialTimeout("tcp", ln.Addr().String(), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target)
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatalf("read CONNECT response: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("allowed CONNECT got %d, want 200", resp.StatusCode)
	}
	line, _ := br.ReadString('\n')
	if line != "HELLO\n" {
		t.Fatalf("tunnel banner = %q, want HELLO", line)
	}
	fmt.Fprint(conn, "ping")
	echo := make([]byte, 4)
	if _, err := br.Read(echo); err != nil {
		t.Fatalf("read echo: %v", err)
	}
	if string(echo) != "ping" {
		t.Fatalf("echo = %q, want ping", echo)
	}

	// Denied host on the same proxy: 403, no tunnel.
	conn2, err := net.DialTimeout("tcp", ln.Addr().String(), 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn2.Close()
	fmt.Fprintf(conn2, "CONNECT evil.com:%s HTTP/1.1\r\nHost: evil.com:%s\r\n\r\n", upPort, upPort)
	resp2, err := http.ReadResponse(bufio.NewReader(conn2), &http.Request{Method: http.MethodConnect})
	if err != nil {
		t.Fatalf("read denied response: %v", err)
	}
	if resp2.StatusCode != http.StatusForbidden {
		t.Fatalf("denied CONNECT got %d, want 403", resp2.StatusCode)
	}
}
