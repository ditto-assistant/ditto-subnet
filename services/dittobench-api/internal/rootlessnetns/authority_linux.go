package rootlessnetns

import (
	"context"
	"net"
	"sync"
	"time"
)

// WithAuthority bounds candidate access to the rootless router by the worker's
// network authority.
//
// In host mode, candidate-to-router traffic crosses host nftables, whose rules
// accept it only before the connectivity profile's expiry and while the timed
// cgroup lease exists. In rootless-netns mode that traffic stays inside
// RootlessKit's network namespace and never reaches those rules. The returned
// listener enforces the window in the worker instead. When expires passes or
// ctx is done (the worker's stop signal, including systemd stopping it because
// the egress guard it is bound to stopped), it closes the listening socket, so
// the kernel refuses new connections, and closes every accepted connection,
// including requests still in flight.
//
// After that, Accept waits for Close and returns net.ErrClosed, so an
// http.Server that is then shut down returns http.ErrServerClosed rather than
// a serve failure. Close before the authority ends stops accepting; accepted
// connections stay bounded by the same expiry until they close.
//
// This is not a kernel guarantee: a stopped or wedged worker cannot enforce
// it. The listener is closed on every refusal.
func WithAuthority(ctx context.Context, listener net.Listener, expires time.Time) (net.Listener, error) {
	if listener == nil {
		return nil, ErrListener
	}
	if ctx == nil || ctx.Err() != nil || expires.IsZero() || !time.Now().Before(expires) {
		_ = listener.Close()
		return nil, ErrListener
	}
	bounded := &authorityListener{Listener: listener, expires: expires, conns: make(map[*authorityConn]struct{}), closed: make(chan struct{})}
	// Hold the lock so an immediate expiry or cancellation cannot observe the
	// listener before both triggers are installed.
	bounded.mu.Lock()
	bounded.timer = time.AfterFunc(time.Until(expires), bounded.end)
	bounded.stopContext = context.AfterFunc(ctx, bounded.end)
	bounded.mu.Unlock()
	return bounded, nil
}

type authorityListener struct {
	net.Listener
	expires time.Time

	mu          sync.Mutex
	conns       map[*authorityConn]struct{}
	ended       bool // authority over: listener and connections closed
	closing     bool // Close was called
	timer       *time.Timer
	stopContext func() bool
	closeOnce   sync.Once
	closed      chan struct{}
}

type authorityConn struct {
	net.Conn
	owner *authorityListener
	once  sync.Once
}

// end is idempotent and never waits for a connection.
func (bounded *authorityListener) end() {
	bounded.mu.Lock()
	if bounded.ended {
		bounded.mu.Unlock()
		return
	}
	bounded.ended = true
	closeListener := !bounded.closing
	conns := make([]net.Conn, 0, len(bounded.conns))
	for conn := range bounded.conns {
		conns = append(conns, conn.Conn)
	}
	clear(bounded.conns)
	bounded.mu.Unlock()
	bounded.stop()
	if closeListener {
		_ = bounded.Listener.Close()
	}
	for _, conn := range conns {
		_ = conn.Close()
	}
}

func (bounded *authorityListener) stop() {
	bounded.mu.Lock()
	timer, stopContext := bounded.timer, bounded.stopContext
	bounded.mu.Unlock()
	timer.Stop()
	stopContext()
}

func (bounded *authorityListener) Accept() (net.Conn, error) {
	conn, err := bounded.Listener.Accept()
	bounded.mu.Lock()
	if bounded.ended || bounded.closing || !time.Now().Before(bounded.expires) {
		// A late timer must not admit a connection after the expiry.
		expired := !bounded.ended && !bounded.closing
		bounded.mu.Unlock()
		if conn != nil {
			_ = conn.Close()
		}
		if expired {
			bounded.end()
		}
		<-bounded.closed
		return nil, net.ErrClosed
	}
	if err != nil {
		bounded.mu.Unlock()
		return nil, err
	}
	tracked := &authorityConn{Conn: conn, owner: bounded}
	bounded.conns[tracked] = struct{}{}
	bounded.mu.Unlock()
	return tracked, nil
}

func (bounded *authorityListener) Close() error {
	var err error
	bounded.closeOnce.Do(func() {
		bounded.mu.Lock()
		bounded.closing = true
		ended, idle := bounded.ended, len(bounded.conns) == 0
		bounded.mu.Unlock()
		if !ended {
			err = bounded.Listener.Close()
		}
		if idle {
			bounded.stop()
		}
		close(bounded.closed)
	})
	return err
}

func (conn *authorityConn) Close() error {
	conn.once.Do(func() {
		owner := conn.owner
		owner.mu.Lock()
		delete(owner.conns, conn)
		done := owner.closing && !owner.ended && len(owner.conns) == 0
		owner.mu.Unlock()
		if done {
			owner.stop()
		}
	})
	return conn.Conn.Close()
}

// CloseWrite keeps net/http's half-close behavior for TCP connections.
func (conn *authorityConn) CloseWrite() error {
	if writer, ok := conn.Conn.(interface{ CloseWrite() error }); ok {
		return writer.CloseWrite()
	}
	return nil
}
