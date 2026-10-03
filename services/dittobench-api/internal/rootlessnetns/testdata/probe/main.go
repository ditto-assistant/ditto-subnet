// Command probe is a synthetic candidate for the rootless router integration
// test. It carries no credentials. By default it requests one route through
// host.docker.internal (the in-namespace router) and one through a
// host-namespace address with the same Host header, then prints the two HTTP
// status codes as JSON. With --expiry it holds one keep-alive connection to a
// route whose authority window is about to end and reports how the router
// ended candidate access.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"syscall"
	"time"
)

func status(target, dial string, deadline time.Duration, want func(int) bool) int {
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true}
	if dial != "" {
		transport.DialContext = func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 2 * time.Second}).DialContext(ctx, network, dial)
		}
	}
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}
	last := 0
	for stop := time.Now().Add(deadline); time.Now().Before(stop); time.Sleep(250 * time.Millisecond) {
		response, err := client.Post(target, "application/json", nil)
		if err != nil {
			continue
		}
		_ = response.Body.Close()
		last = response.StatusCode
		if want(last) {
			break
		}
	}
	return last
}

// expiry keeps one admitted keep-alive connection open until the router ends
// it, then tries a new connection.
func expiry(target string) map[string]any {
	result := map[string]any{"before_expiry": 0, "established_closed": false, "closed_after_ms": -1, "after_expiry_dial": "not_tried"}
	parsed, err := url.Parse(target)
	if err != nil {
		return result
	}
	request := fmt.Sprintf("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Length: 0\r\n\r\n", parsed.EscapedPath(), parsed.Host)
	var conn net.Conn
	for stop := time.Now().Add(60 * time.Second); time.Now().Before(stop); time.Sleep(250 * time.Millisecond) {
		candidate, err := net.DialTimeout("tcp4", parsed.Host, 2*time.Second)
		if err != nil {
			continue
		}
		_ = candidate.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := candidate.Write([]byte(request)); err != nil {
			_ = candidate.Close()
			continue
		}
		response, err := http.ReadResponse(bufio.NewReader(candidate), nil)
		if err != nil {
			_ = candidate.Close()
			continue
		}
		_ = response.Body.Close()
		result["before_expiry"] = response.StatusCode
		if response.StatusCode == http.StatusOK {
			conn = candidate
			break
		}
		_ = candidate.Close()
	}
	if conn == nil {
		return result
	}
	admitted := time.Now()
	_ = conn.SetDeadline(time.Now().Add(90 * time.Second))
	_, err = conn.Read(make([]byte, 1))
	var timeout net.Error
	if err != nil && !(errors.As(err, &timeout) && timeout.Timeout()) {
		result["established_closed"] = true
		result["closed_after_ms"] = time.Since(admitted).Milliseconds()
	}
	_ = conn.Close()
	after, err := net.DialTimeout("tcp4", parsed.Host, 2*time.Second)
	switch {
	case err == nil:
		_ = after.Close()
		result["after_expiry_dial"] = "accepted"
	case errors.Is(err, syscall.ECONNREFUSED):
		result["after_expiry_dial"] = "refused"
	default:
		result["after_expiry_dial"] = "error"
	}
	return result
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "--expiry" {
		if json.NewEncoder(os.Stdout).Encode(expiry(os.Args[2])) != nil {
			os.Exit(1)
		}
		return
	}
	if len(os.Args) != 4 {
		os.Exit(2)
	}
	result := map[string]int{
		// Retry until the test has registered this container's source address.
		"in_namespace":   status(os.Args[1], "", 90*time.Second, func(code int) bool { return code == http.StatusOK }),
		"host_namespace": status(os.Args[2], os.Args[3], 20*time.Second, func(code int) bool { return code != 0 }),
	}
	if json.NewEncoder(os.Stdout).Encode(result) != nil {
		os.Exit(1)
	}
}
