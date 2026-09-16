package codingcertservice

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (clock *fakeClock) Now() time.Time {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	return clock.now
}

func (clock *fakeClock) Advance(duration time.Duration) {
	clock.mu.Lock()
	defer clock.mu.Unlock()
	clock.now = clock.now.Add(duration)
}

var testLimits = AdmissionLimits{MaxLifetime: time.Hour, IdleTimeout: 10 * time.Minute}

func newTestAdmission(t *testing.T, limits AdmissionLimits) (*admission, context.Context, *fakeClock) {
	t.Helper()
	clock := &fakeClock{now: time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)}
	window, ctx := newAdmission(t.Context(), limits, clock.Now)
	t.Cleanup(window.end)
	return window, ctx, clock
}

func ended(ctx context.Context) bool {
	select {
	case <-ctx.Done():
		return true
	default:
		return false
	}
}

func TestAdmissionLimitsAreValidatedAndDefaultsAreConservative(t *testing.T) {
	if !(AdmissionLimits{MaxLifetime: DefaultMaxLifetime, IdleTimeout: DefaultIdleTimeout}).Valid() {
		t.Fatal("defaults are invalid")
	}
	if DefaultMaxLifetime > 2*time.Hour || DefaultIdleTimeout > 15*time.Minute {
		t.Fatalf("defaults are not conservative: %v %v", DefaultMaxLifetime, DefaultIdleTimeout)
	}
	// Every valid lifetime leaves room for at least one certification.
	if MinMaxLifetime <= certifyAdmissionWindow || MinIdleTimeout <= claimIdleGrace {
		t.Fatal("minimum bounds leave no admission room")
	}
	for _, limits := range []AdmissionLimits{
		{},
		{MaxLifetime: MinMaxLifetime - time.Second, IdleTimeout: MinIdleTimeout},
		{MaxLifetime: MaxMaxLifetime + time.Second, IdleTimeout: MinIdleTimeout},
		{MaxLifetime: time.Hour, IdleTimeout: MinIdleTimeout - time.Second},
		{MaxLifetime: time.Hour, IdleTimeout: time.Hour + time.Second},
	} {
		if limits.Valid() {
			t.Errorf("limits %+v accepted", limits)
		}
		window, ctx := newAdmission(t.Context(), limits, nil)
		if !ended(ctx) || window.permits(t.Context()) {
			t.Errorf("invalid limits %+v opened a window", limits)
		}
		if _, ok := window.begin(); ok {
			t.Errorf("invalid limits %+v admitted activity", limits)
		}
	}
}

func TestAdmissionEndsAtTheIdleTimeoutWithoutActivity(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	clock.Advance(10*time.Minute - time.Second)
	window.evaluate()
	if ended(ctx) {
		t.Fatal("ended before the idle timeout")
	}
	clock.Advance(time.Second)
	window.evaluate()
	if !ended(ctx) {
		t.Fatal("still admitting after the idle timeout")
	}
	if _, ok := window.begin(); ok || window.permits(t.Context()) {
		t.Fatal("an ended window admitted activity")
	}
}

func TestAdmissionActivityHoldsIdleButNeverTheLifetime(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	release, ok := window.begin()
	if !ok {
		t.Fatal("begin refused")
	}
	clock.Advance(30 * time.Minute)
	window.evaluate()
	if ended(ctx) {
		t.Fatal("idle timeout fired during activity")
	}
	release()
	release() // idempotent
	clock.Advance(10*time.Minute - time.Second)
	window.evaluate()
	if ended(ctx) {
		t.Fatal("idle timeout did not restart at the end of activity")
	}
	if _, ok := window.begin(); !ok {
		t.Fatal("begin refused inside the window")
	}
	// Active until past the lifetime: the lifetime still ends the window.
	clock.Advance(21 * time.Minute)
	window.evaluate()
	if !ended(ctx) {
		t.Fatal("lifetime did not end an active window")
	}
}

func TestAdmissionRefusesActivityOnceABoundHasPassedEvenBeforeTheTimer(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	clock.Advance(time.Hour)
	if window.permits(t.Context()) {
		t.Fatal("permits after the lifetime")
	}
	if _, ok := window.begin(); ok {
		t.Fatal("begin admitted after the lifetime")
	}
	if !ended(ctx) {
		t.Fatal("a refused begin did not end the window")
	}
}

func TestAdmissionTimerEndsTheWindowWithoutAnyCall(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	clock.Advance(11 * time.Minute)
	// Re-arm from the current state: the idle bound is already past, so the
	// real timer fires at once and ends the window on its own.
	window.mu.Lock()
	window.scheduleLocked(clock.Now())
	window.mu.Unlock()
	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("the admission timer did not end the window")
	}
}

func TestAdmissionEndsWithItsParent(t *testing.T) {
	parent, cancel := context.WithCancel(t.Context())
	window, ctx := newAdmission(parent, testLimits, nil)
	cancel()
	select {
	case <-ctx.Done():
	case <-time.After(5 * time.Second):
		t.Fatal("parent cancellation did not end the window")
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, ok := window.begin(); !ok {
			return
		}
	}
	t.Fatal("a cancelled window still admits activity")
}

func TestReadinessNeedsRoomForAWholeCertification(t *testing.T) {
	certifying := context.WithValue(t.Context(), certifyingKey{}, true)

	// Lifetime: readiness needs 35 minutes left; an in-flight certify needs
	// only an open window.
	window, _, clock := newTestAdmission(t, AdmissionLimits{MaxLifetime: time.Hour, IdleTimeout: time.Hour})
	clock.Advance(25 * time.Minute)
	if !window.permits(t.Context()) {
		t.Fatal("readiness refused with 35 minutes left")
	}
	clock.Advance(time.Second)
	if window.permits(t.Context()) {
		t.Fatal("readiness admitted a lease that could outlive the lifetime")
	}
	if !window.permits(certifying) {
		t.Fatal("an in-flight certify was refused inside the window")
	}

	// Idle: with nothing active, readiness needs 5 minutes of idle time left.
	window, _, clock = newTestAdmission(t, testLimits)
	clock.Advance(5 * time.Minute)
	if !window.permits(t.Context()) {
		t.Fatal("readiness refused with 5 idle minutes left")
	}
	clock.Advance(time.Second)
	if window.permits(t.Context()) {
		t.Fatal("readiness admitted a claim the idle timeout could cut off")
	}
	if !window.permits(certifying) {
		t.Fatal("an in-flight certify was refused inside the idle window")
	}
	release, ok := window.begin()
	if !ok || !window.permits(t.Context()) {
		t.Fatal("readiness refused while a certification is active")
	}
	release()
	if !window.permits(t.Context()) {
		t.Fatal("readiness refused right after activity restarted the idle window")
	}
	var missing *admission
	if missing.permits(t.Context()) {
		t.Fatal("a nil window permits")
	}
}

func TestCertifyMiddlewareCountsOnlyPostCertifyRequests(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	var sawCertifying []bool
	handler := window.Certify(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		certifying, _ := request.Context().Value(certifyingKey{}).(bool)
		sawCertifying = append(sawCertifying, certifying)
		window.mu.Lock()
		active := window.active
		window.mu.Unlock()
		if certifying != (active == 1) {
			t.Errorf("certifying=%v active=%d", certifying, active)
		}
		// A long certify outlives the idle timeout without ending the window.
		clock.Advance(20 * time.Minute)
		window.evaluate()
	}))
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/coding/certifier/canary", nil))
	if ended(ctx) {
		t.Fatal("the idle timeout ended the window during a certify request")
	}
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/v1/coding/certifier/canary", nil))
	window.evaluate()
	if !ended(ctx) {
		t.Fatal("a non-POST request counted as activity")
	}
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/coding/certifier/canary", nil))
	if len(sawCertifying) != 3 || !sawCertifying[0] || sawCertifying[1] || sawCertifying[2] {
		t.Fatalf("certifying marks=%v", sawCertifying)
	}
}

type pipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
	once   sync.Once
}

func (listener *pipeListener) Accept() (net.Conn, error) {
	select {
	case conn := <-listener.conns:
		return conn, nil
	case <-listener.closed:
		return nil, net.ErrClosed
	}
}

func (listener *pipeListener) Close() error {
	listener.once.Do(func() { close(listener.closed) })
	return nil
}

func (listener *pipeListener) Addr() net.Addr { return &net.UnixAddr{Name: "pipe", Net: "unix"} }

func TestRouterConnectionsAreActivityAndRefusedAfterTheWindow(t *testing.T) {
	window, ctx, clock := newTestAdmission(t, testLimits)
	raw := &pipeListener{conns: make(chan net.Conn, 2), closed: make(chan struct{})}
	listener := window.Listener(raw)
	server, client := net.Pipe()
	defer client.Close()
	raw.conns <- server
	conn, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	clock.Advance(30 * time.Minute)
	window.evaluate()
	if ended(ctx) {
		t.Fatal("idle timeout fired with an open router connection")
	}
	_ = conn.Close()
	_ = conn.Close()
	clock.Advance(10 * time.Minute)
	window.evaluate()
	if !ended(ctx) {
		t.Fatal("idle timeout did not follow the closed router connection")
	}

	// After the window, an accepted connection is closed at once and never
	// returned; Accept keeps waiting until the listener closes.
	late, lateClient := net.Pipe()
	defer lateClient.Close()
	raw.conns <- late
	accepted := make(chan error, 1)
	go func() {
		_, err := listener.Accept()
		accepted <- err
	}()
	_ = lateClient.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := lateClient.Read(make([]byte, 1)); err == nil {
		t.Fatal("a late router connection stayed open")
	}
	_ = raw.Close()
	if err := <-accepted; err == nil {
		t.Fatal("Accept returned a connection after the window ended")
	}
}
