package pprofserver

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"
)

func freePort(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return listener.Addr().(*net.TCPAddr).Port
}

func waitForListener(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 50*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("pprof listener never came up on %s", addr)
}

func TestStartServesStandardProfilesOnLoopback(t *testing.T) {
	port := freePort(t)
	t.Setenv("PPROF_PORT", strconv.Itoa(port))
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	Start(ctx, slog.New(slog.NewTextHandler(io.Discard, nil)), "test", 8000)

	addr := fmt.Sprintf("127.0.0.1:%d", port)
	waitForListener(t, addr)
	response, err := http.Get("http://" + addr + "/debug/pprof/")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	for _, profile := range []string{"heap", "goroutine", "allocs"} {
		if !strings.Contains(string(body), profile) {
			t.Errorf("pprof index missing %q", profile)
		}
	}
}

func TestResolvePort(t *testing.T) {
	if got, err := resolvePort(8010, ""); err != nil || got != 11010 {
		t.Fatalf("derived port = %d, %v; want 11010", got, err)
	}
	if got, err := resolvePort(8010, "19090"); err != nil || got != 19090 {
		t.Fatalf("override port = %d, %v; want 19090", got, err)
	}
	for _, override := range []string{"nope", "0", "65536"} {
		if _, err := resolvePort(8010, override); err == nil {
			t.Errorf("resolvePort accepted %q", override)
		}
	}
}
