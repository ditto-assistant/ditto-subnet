package relayhttp

import (
	"compress/gzip"
	"net/http"
	"strconv"
	"strings"
)

// gzipMinimumSize mirrors SizedGZipMiddleware(minimum_size=1000): responses
// below it pass through byte-identical, with no Vary header.
const gzipMinimumSize = 1000

// gzipCompressLevel mirrors compresslevel=6.
const gzipCompressLevel = 6

// sizedGzipWriter buffers the response so the compress-or-not decision is
// made on the COMPLETE body size, like the Python middleware does with the
// declared Content-Length. Every relay response is a fully-buffered JSON or
// text body (there is no streaming anywhere on this surface), so buffering
// is exact, never partial.
type sizedGzipWriter struct {
	http.ResponseWriter
	acceptsGzip bool
	status      int
	body        []byte
}

func (w *sizedGzipWriter) WriteHeader(status int) { w.status = status }

func (w *sizedGzipWriter) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	w.body = append(w.body, p...)
	return len(p), nil
}

func (w *sizedGzipWriter) flush() {
	if w.status == 0 {
		// Handler wrote nothing at all; nothing to send beyond the default.
		w.status = http.StatusOK
	}
	header := w.ResponseWriter.Header()
	if w.acceptsGzip && len(w.body) >= gzipMinimumSize {
		header.Add("Vary", "Accept-Encoding")
		header.Set("Content-Encoding", "gzip")
		header.Del("Content-Length")
		// Compress into memory first so Content-Length is exact, matching
		// the Python middleware's rewritten Content-Length.
		var buf strings.Builder
		gz, _ := gzip.NewWriterLevel(&buf, gzipCompressLevel)
		_, _ = gz.Write(w.body)
		_ = gz.Close()
		compressed := buf.String()
		header.Set("Content-Length", strconv.Itoa(len(compressed)))
		w.ResponseWriter.WriteHeader(w.status)
		_, _ = w.ResponseWriter.Write([]byte(compressed))
		return
	}
	header.Set("Content-Length", strconv.Itoa(len(w.body)))
	w.ResponseWriter.WriteHeader(w.status)
	_, _ = w.ResponseWriter.Write(w.body)
}

// SizedGzipMiddleware mirrors apps/platform's SizedGZipMiddleware: compress
// a response only when the request accepts gzip AND the complete body is at
// least 1000 bytes; smaller responses pass through byte-identical with no
// Vary header. It sits INNERMOST in the stack, exactly where the Python
// middleware registers.
func SizedGzipMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		accepts := false
		for _, part := range strings.Split(r.Header.Get("Accept-Encoding"), ",") {
			token := strings.TrimSpace(part)
			if token == "gzip" || strings.HasPrefix(token, "gzip;") {
				accepts = true
				break
			}
		}
		gw := &sizedGzipWriter{ResponseWriter: w, acceptsGzip: accepts}
		next.ServeHTTP(gw, r)
		gw.flush()
	})
}
