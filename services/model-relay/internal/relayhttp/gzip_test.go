package relayhttp

import (
	"compress/gzip"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func gzipEcho(body string) http.Handler {
	return SizedGzipMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(body))
	}))
}

func TestSizedGzipCompressesLargeBodies(t *testing.T) {
	body := strings.Repeat("x", 2000)
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.Header.Set("Accept-Encoding", "gzip, deflate")
	w := httptest.NewRecorder()
	gzipEcho(body).ServeHTTP(w, r)

	if w.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("large body must be gzipped")
	}
	if w.Header().Get("Vary") != "Accept-Encoding" {
		t.Fatalf("Vary must be set when compressing")
	}
	gz, err := gzip.NewReader(w.Body)
	if err != nil {
		t.Fatalf("gzip reader: %v", err)
	}
	decoded, err := io.ReadAll(gz)
	if err != nil {
		t.Fatalf("decompress: %v", err)
	}
	if string(decoded) != body {
		t.Fatalf("round trip mismatch")
	}
}

func TestSizedGzipPassesSmallBodiesUntouched(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.Header.Set("Accept-Encoding", "gzip")
	w := httptest.NewRecorder()
	gzipEcho(`{"ok":true}`).ServeHTTP(w, r)

	if w.Header().Get("Content-Encoding") != "" || w.Header().Get("Vary") != "" {
		t.Fatalf("small body must pass through byte-identical with no Vary")
	}
	if w.Body.String() != `{"ok":true}` {
		t.Fatalf("body altered: %q", w.Body.String())
	}
}

func TestSizedGzipRespectsClientWithoutGzip(t *testing.T) {
	body := strings.Repeat("y", 2000)
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	w := httptest.NewRecorder()
	gzipEcho(body).ServeHTTP(w, r)

	if w.Header().Get("Content-Encoding") != "" {
		t.Fatalf("no gzip without Accept-Encoding")
	}
	if w.Body.String() != body {
		t.Fatalf("body altered")
	}
}
