package rootlessnetns

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"syscall"
	"testing"
	"time"
)

func loopbackListener(t *testing.T) net.Listener {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return listener
}

// serveEcho echoes on every accepted connection and reports Accept's
// terminal error.
func serveEcho(listener net.Listener) <-chan error {
	done := make(chan error, 1)
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				done <- err
				return
			}
			go func() { _, _ = io.Copy(conn, conn) }()
		}
	}()
	return done
}

func dialEcho(t *testing.T, address string) net.Conn {
	t.Helper()
	conn, err := net.DialTimeout("tcp4", address, time.Second)
	if err != nil {
		t.Fatalf("dial before authority ended: %v", err)
	}
	if _, err := conn.Write([]byte("x")); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 1)
	if err := conn.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(conn, reply); err != nil {
		t.Fatalf("echo before authority ended: %v", err)
	}
	return conn
}

// waitClosed requires the peer to close conn within bound.
func waitClosed(t *testing.T, conn net.Conn, bound time.Duration) time.Time {
	t.Helper()
	if err := conn.SetReadDeadline(time.Now().Add(bound)); err != nil {
		t.Fatal(err)
	}
	_, err := conn.Read(make([]byte, 1))
	var timeout net.Error
	if err == nil || errors.As(err, &timeout) && timeout.Timeout() {
		t.Fatalf("established connection survived the authority: %v", err)
	}
	return time.Now()
}

func requireRefused(t *testing.T, address string) {
	t.Helper()
	conn, err := net.DialTimeout("tcp4", address, time.Second)
	if err == nil {
		_ = conn.Close()
		t.Fatal("new connection accepted after the authority ended")
	}
	if !errors.Is(err, syscall.ECONNREFUSED) {
		t.Fatalf("new connection not refused by the kernel: %v", err)
	}
}

func TestAuthorityEndsListenerAndConnectionsAtExpiry(t *testing.T) {
	raw := loopbackListener(t)
	address := raw.Addr().String()
	expires := time.Now().Add(400 * time.Millisecond)
	bounded, err := WithAuthority(t.Context(), raw, expires)
	if err != nil {
		t.Fatal(err)
	}
	done := serveEcho(bounded)
	established := dialEcho(t, address)
	defer established.Close()
	closedAt := waitClosed(t, established, 5*time.Second)
	if closedAt.Before(expires) {
		t.Fatalf("connection closed %s before the expiry", expires.Sub(closedAt))
	}
	requireRefused(t, address)
	select {
	case err := <-done:
		t.Fatalf("Accept returned before Close: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	if err := bounded.Close(); err != nil {
		t.Fatalf("Close after the authority ended: %v", err)
	}
	if err := <-done; !errors.Is(err, net.ErrClosed) {
		t.Fatalf("Accept after Close: %v", err)
	}
}

func TestAuthorityEndsOnWorkerCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	raw := loopbackListener(t)
	address := raw.Addr().String()
	bounded, err := WithAuthority(ctx, raw, time.Now().Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	defer bounded.Close()
	serveEcho(bounded)
	established := dialEcho(t, address)
	defer established.Close()
	cancel()
	waitClosed(t, established, 5*time.Second)
	requireRefused(t, address)
}

func TestAuthorityRefusesEndedWindowAndClosesListener(t *testing.T) {
	cancelled, cancel := context.WithCancel(t.Context())
	cancel()
	for name, tc := range map[string]struct {
		ctx     context.Context
		expires time.Time
	}{
		"expired":   {t.Context(), time.Now().Add(-time.Second)},
		"now":       {t.Context(), time.Now()},
		"zero":      {t.Context(), time.Time{}},
		"cancelled": {cancelled, time.Now().Add(time.Hour)},
		"nil_ctx":   {nil, time.Now().Add(time.Hour)},
	} {
		t.Run(name, func(t *testing.T) {
			raw := loopbackListener(t)
			if listener, err := WithAuthority(tc.ctx, raw, tc.expires); err == nil {
				_ = listener.Close()
				t.Fatal("ended authority produced a listener")
			}
			if _, err := raw.Accept(); !errors.Is(err, net.ErrClosed) {
				t.Fatalf("refused listener left open: %v", err)
			}
		})
	}
	if _, err := WithAuthority(t.Context(), nil, time.Now().Add(time.Hour)); err == nil {
		t.Fatal("nil listener accepted")
	}
}

// Close before the expiry stops accepting, but a connection still open keeps
// the same bound and is closed at the expiry.
func TestAuthorityCloseKeepsOpenConnectionsBounded(t *testing.T) {
	raw := loopbackListener(t)
	address := raw.Addr().String()
	expires := time.Now().Add(500 * time.Millisecond)
	bounded, err := WithAuthority(t.Context(), raw, expires)
	if err != nil {
		t.Fatal(err)
	}
	done := serveEcho(bounded)
	established := dialEcho(t, address)
	defer established.Close()
	if err := bounded.Close(); err != nil {
		t.Fatal(err)
	}
	if err := <-done; !errors.Is(err, net.ErrClosed) {
		t.Fatalf("Accept after Close: %v", err)
	}
	requireRefused(t, address)
	if closedAt := waitClosed(t, established, 5*time.Second); closedAt.Before(expires) {
		t.Fatal("connection closed before the expiry")
	}
}

// An http.Server whose authority ended must still shut down cleanly, so the
// worker does not misreport an expired window as unconfirmed cleanup.
func TestAuthorityHTTPServerShutsDownCleanlyAfterExpiry(t *testing.T) {
	raw := loopbackListener(t)
	address := raw.Addr().String()
	started := make(chan struct{})
	bounded, err := WithAuthority(t.Context(), raw, time.Now().Add(500*time.Millisecond))
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/hang" {
			close(started)
			<-request.Context().Done()
			return
		}
		response.WriteHeader(http.StatusNoContent)
	})}
	served := make(chan error, 1)
	go func() { served <- server.Serve(bounded) }()
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	defer client.CloseIdleConnections()
	response, err := client.Get("http://" + address + "/")
	if err != nil || response.StatusCode != http.StatusNoContent {
		t.Fatalf("request before expiry: %v", err)
	}
	_ = response.Body.Close()
	hung := make(chan error, 1)
	go func() {
		response, err := (&http.Client{Transport: &http.Transport{Proxy: nil, DisableKeepAlives: true}}).Get("http://" + address + "/hang")
		if err == nil {
			_ = response.Body.Close()
		}
		hung <- err
	}()
	<-started
	select {
	case err := <-hung:
		if err == nil {
			t.Fatal("in-flight request completed after the authority ended")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("in-flight request survived the authority")
	}
	requireRefused(t, address)
	shutdown, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdown); err != nil {
		t.Fatalf("shutdown after expiry: %v", err)
	}
	if err := <-served; !errors.Is(err, http.ErrServerClosed) {
		t.Fatalf("Serve after expiry and shutdown: %v", err)
	}
}
