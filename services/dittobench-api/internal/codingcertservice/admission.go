package codingcertservice

import (
	"context"
	"errors"
	"net"
	"net/http"
	"sync"
	"time"
)

// Admission window defaults and bounds. The service is long-lived, unlike the
// one-shot hosted worker, so candidate admission to its router listener is not
// left to source binding and route tokens alone: every service run admits work
// only for a bounded lifetime and only until it has been idle for the idle
// timeout. When either ends, the router listener and its connections are
// closed, the control socket is removed and Run returns ErrAdmissionEnded. An
// operator must start the service again, which re-proves the whole placement.
const (
	DefaultMaxLifetime = 2 * time.Hour
	MinMaxLifetime     = time.Hour
	MaxMaxLifetime     = 12 * time.Hour

	DefaultIdleTimeout = 15 * time.Minute
	MinIdleTimeout     = 10 * time.Minute

	// certifyAdmissionWindow is the lifetime that must remain for readiness to
	// report the listener ready. A lease deadline is at most 30 minutes after
	// issue and readiness is proven before issue, so a claimed certification
	// never outlives the admission window.
	certifyAdmissionWindow = 35 * time.Minute
	// claimIdleGrace is the idle time that must remain for readiness to report
	// ready while no certification is in flight. It covers issue, the second
	// readiness proof, claim, harness launch and grant exchange before the
	// certify request arrives and marks the service active.
	claimIdleGrace = 5 * time.Minute
)

// ErrAdmissionEnded reports that the service stopped because its admission
// window reached its lifetime or idle bound. It is not a placement failure.
var ErrAdmissionEnded = errors.New("coding certification service admission window ended")

// AdmissionLimits bounds one service run.
type AdmissionLimits struct {
	MaxLifetime time.Duration
	IdleTimeout time.Duration
}

// Valid reports whether the limits are within the reviewed bounds: a lifetime
// in [1h, 12h] and an idle timeout in [10m, lifetime], so readiness always has
// room for at least one full certification.
func (limits AdmissionLimits) Valid() bool {
	return limits.MaxLifetime >= MinMaxLifetime && limits.MaxLifetime <= MaxMaxLifetime &&
		limits.IdleTimeout >= MinIdleTimeout && limits.IdleTimeout <= limits.MaxLifetime
}

type certifyingKey struct{}

// admission is one service run's admission window. Activity is an open router
// connection or an in-flight certify request; readiness probes are not
// activity, so polling cannot keep an idle service admitting work.
type admission struct {
	now     func() time.Time
	expires time.Time
	idle    time.Duration
	cancel  context.CancelFunc

	mu         sync.Mutex
	active     int
	lastActive time.Time
	ended      bool
	timer      *time.Timer
}

// newAdmission starts the window at now. The returned context ends when the
// parent ends, the lifetime passes, or the service has been idle for the idle
// timeout, whichever is first.
func newAdmission(parent context.Context, limits AdmissionLimits, now func() time.Time) (*admission, context.Context) {
	if now == nil {
		now = time.Now
	}
	ctx, cancel := context.WithCancel(parent)
	started := now()
	window := &admission{
		now: now, expires: started.Add(limits.MaxLifetime), idle: limits.IdleTimeout,
		cancel: cancel, lastActive: started,
	}
	if !limits.Valid() {
		window.ended = true
		cancel()
		return window, ctx
	}
	context.AfterFunc(ctx, window.end)
	window.mu.Lock()
	window.scheduleLocked(started)
	window.mu.Unlock()
	return window, ctx
}

// Expires is the absolute end of the lifetime.
func (window *admission) Expires() time.Time { return window.expires }

// scheduleLocked arms the next evaluation at the earliest bound that can end
// the window from the current state.
func (window *admission) scheduleLocked(now time.Time) {
	if window.ended {
		return
	}
	next := window.expires
	if window.active == 0 {
		if idleEnd := window.lastActive.Add(window.idle); idleEnd.Before(next) {
			next = idleEnd
		}
	}
	delay := next.Sub(now)
	if delay < 0 {
		delay = 0
	}
	if window.timer != nil {
		window.timer.Stop()
	}
	window.timer = time.AfterFunc(delay, window.evaluate)
}

// evaluate ends the window if a bound has passed and otherwise re-arms it.
func (window *admission) evaluate() {
	window.mu.Lock()
	if window.ended {
		window.mu.Unlock()
		return
	}
	now := window.now()
	if window.expiredLocked(now) {
		window.mu.Unlock()
		window.end()
		return
	}
	window.scheduleLocked(now)
	window.mu.Unlock()
}

func (window *admission) expiredLocked(now time.Time) bool {
	return !now.Before(window.expires) ||
		(window.active == 0 && !now.Before(window.lastActive.Add(window.idle)))
}

// end is idempotent.
func (window *admission) end() {
	window.mu.Lock()
	if window.ended {
		window.mu.Unlock()
		return
	}
	window.ended = true
	if window.timer != nil {
		window.timer.Stop()
	}
	window.mu.Unlock()
	window.cancel()
}

// begin marks one unit of activity. It refuses once the window has ended or a
// bound has passed, even if the timer has not fired yet.
func (window *admission) begin() (func(), bool) {
	window.mu.Lock()
	if window.ended || window.expiredLocked(window.now()) {
		window.mu.Unlock()
		window.evaluate()
		return nil, false
	}
	window.active++
	window.scheduleLocked(window.now())
	window.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			window.mu.Lock()
			window.active--
			now := window.now()
			window.lastActive = now
			window.scheduleLocked(now)
			window.mu.Unlock()
		})
	}, true
}

// permits is the listener admission proof used by readiness and the certify
// re-check. A certify request already in flight needs only an open window.
// Readiness additionally needs room for a whole certification before the
// lifetime ends and, when nothing is active, before the idle timeout ends.
func (window *admission) permits(ctx context.Context) bool {
	if window == nil || ctx == nil {
		return false
	}
	window.mu.Lock()
	defer window.mu.Unlock()
	now := window.now()
	if window.ended || window.expiredLocked(now) {
		return false
	}
	if certifying, _ := ctx.Value(certifyingKey{}).(bool); certifying {
		return true
	}
	if window.expires.Sub(now) < certifyAdmissionWindow {
		return false
	}
	return window.active > 0 || window.lastActive.Add(window.idle).Sub(now) >= claimIdleGrace
}

// Certify marks a POST certify request as activity for its whole duration and
// tells the placement re-check it is in flight. Other methods and requests
// after the window has ended pass through unmarked, so the handler's own
// method and placement checks refuse them.
func (window *admission) Certify(next http.Handler) http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost {
			next.ServeHTTP(response, request)
			return
		}
		release, ok := window.begin()
		if !ok {
			next.ServeHTTP(response, request)
			return
		}
		defer release()
		next.ServeHTTP(response, request.WithContext(context.WithValue(request.Context(), certifyingKey{}, true)))
	})
}

// Listener counts every accepted router connection as activity until it
// closes. A connection accepted after the window has ended is closed at once.
func (window *admission) Listener(listener net.Listener) net.Listener {
	return &admissionListener{Listener: listener, window: window}
}

type admissionListener struct {
	net.Listener
	window *admission
}

func (listener *admissionListener) Accept() (net.Conn, error) {
	for {
		conn, err := listener.Listener.Accept()
		if err != nil {
			return nil, err
		}
		release, ok := listener.window.begin()
		if !ok {
			_ = conn.Close()
			continue
		}
		return &admissionConn{Conn: conn, release: release}, nil
	}
}

type admissionConn struct {
	net.Conn
	release func()
	once    sync.Once
}

func (conn *admissionConn) Close() error {
	conn.once.Do(conn.release)
	return conn.Conn.Close()
}

// CloseWrite keeps net/http's half-close behavior for TCP connections.
func (conn *admissionConn) CloseWrite() error {
	if writer, ok := conn.Conn.(interface{ CloseWrite() error }); ok {
		return writer.CloseWrite()
	}
	return nil
}
