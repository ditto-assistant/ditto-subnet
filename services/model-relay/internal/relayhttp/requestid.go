// Package relayhttp carries the relay's HTTP plumbing: the request-ID
// middleware, the auth pass-through, and the wire error envelope. All shapes
// mirror apps/platform/ditto/api_server/middleware/* byte-for-byte on the
// wire; old brokers depend on them.
package relayhttp

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"time"
)

// requestIDHeader is the inbound/outbound correlation header.
const requestIDHeader = "X-Request-ID"

// requestIDPattern accepts the same shape the Python middleware accepts;
// anything else is replaced with a fresh uuid4 hex.
var requestIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

type requestIDKey struct{}

// RequestID returns the request ID stored on the context, or "" when the
// middleware did not run.
func RequestID(ctx context.Context) string {
	v, _ := ctx.Value(requestIDKey{}).(string)
	return v
}

// newRequestID mints a uuid4-hex-shaped ID (32 lowercase hex chars), matching
// the Python middleware's uuid4().hex fallback.
func newRequestID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is unrecoverable process state; fall back to a
		// constant so the envelope still has a request_id field.
		return "00000000000000000000000000000000"
	}
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // RFC 4122 variant
	return hex.EncodeToString(b[:])
}

// statusRecorder captures the response status for the access log line.
type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Write(p []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.ResponseWriter.Write(p)
}

// RequestIDMiddleware is the outermost middleware. It accepts a valid inbound
// X-Request-ID or mints a fresh one, stores it on the context, echoes it on
// every response, and emits one access log line per request:
// "{METHOD} {path} -> {status} in {ms}ms" (uvicorn's access log is what this
// line replaces in the Python relay).
func RequestIDMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := r.Header.Get(requestIDHeader)
		if !requestIDPattern.MatchString(rid) {
			rid = newRequestID()
		}
		ctx := context.WithValue(r.Context(), requestIDKey{}, rid)
		w.Header().Set(requestIDHeader, rid)

		rec := &statusRecorder{ResponseWriter: w}
		start := time.Now()
		next.ServeHTTP(rec, r.WithContext(ctx))
		status := rec.status
		if status == 0 {
			status = http.StatusOK
		}
		elapsed := time.Since(start)
		logger.LogAttrs(ctx, slog.LevelInfo,
			fmt.Sprintf("%s %s -> %d in %dms", r.Method, r.URL.Path, status, elapsed.Milliseconds()),
			slog.String("request_id", rid),
			slog.String("method", r.Method),
			slog.String("path", r.URL.Path),
			slog.Int("status", status),
			slog.Int64("duration_ms", elapsed.Milliseconds()),
		)
	})
}

// AuthPassThroughMiddleware is a literal no-op, mirroring the Python
// AuthPassThroughMiddleware: no authentication happens in middleware; every
// endpoint authenticates itself. It exists so the middleware stack shape (and
// any future cross-cutting auth concern) has a named home.
func AuthPassThroughMiddleware(next http.Handler) http.Handler {
	return next
}
