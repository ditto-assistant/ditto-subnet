package traces

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// corruptingS3 is an S3 stand-in whose storage damages what it acknowledges:
// it returns 200 on PUT but keeps a mangled copy — the failure mode the
// 2026-09-02 bucket audit found on ~1.5% of production objects. mode "always"
// corrupts every stored copy; "once" corrupts only the first.
type corruptingS3 struct {
	mu     sync.Mutex
	mode   string
	stored map[string][]byte
	puts   int
	gets   int
	server *httptest.Server
}

func newCorruptingS3(t *testing.T, mode string) *corruptingS3 {
	t.Helper()
	c := &corruptingS3{mode: mode, stored: map[string][]byte{}}
	c.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		defer c.mu.Unlock()
		key := strings.TrimPrefix(r.URL.Path, "/")
		switch r.Method {
		case http.MethodHead:
			w.WriteHeader(http.StatusOK)
		case http.MethodPut:
			body, _ := io.ReadAll(r.Body)
			c.puts++
			if c.mode == "always" || (c.mode == "once" && c.puts == 1) {
				if len(body) > 0 {
					body = append([]byte{}, body...)
					body[len(body)/2] ^= 0xFF // silent storage damage
				}
			}
			c.stored[key] = body
			w.WriteHeader(http.StatusOK)
		case http.MethodGet:
			c.gets++
			body, ok := c.stored[key]
			if !ok {
				w.WriteHeader(http.StatusNotFound)
				return
			}
			_, _ = w.Write(body)
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(c.server.Close)
	return c
}

func (c *corruptingS3) sink(t *testing.T) *S3Sink {
	t.Helper()
	s, err := NewS3Sink(S3Config{
		Name: "hippius", Endpoint: c.server.URL, Region: "decentralized",
		Bucket: "b", AccessKeyID: "k", SecretAccessKey: "s",
		Required: true, PathStyle: true,
	}, c.server.Client())
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func payload() (string, int64, func() (io.ReadCloser, error)) {
	body := strings.Repeat("trace-bytes-", 64)
	sum := sha256.Sum256([]byte(body))
	return hex.EncodeToString(sum[:]), int64(len(body)),
		func() (io.ReadCloser, error) { return io.NopCloser(strings.NewReader(body)), nil }
}

// A destination that acknowledges the PUT but keeps damaged bytes must NOT
// count as success. Before verify-after-put this test's Put returned nil on
// the first 2xx — the exact path 143 production objects were lost through.
func TestPutFailsWhenStoreCorruptsEveryCopy(t *testing.T) {
	c := newCorruptingS3(t, "always")
	sum, size, open := payload()
	err := c.sink(t).Put(context.Background(), "k1", size, "application/zstd", sum, open)
	if err == nil {
		t.Fatal("Put must fail when every stored copy hashes wrong")
	}
	if !strings.Contains(err.Error(), "sha256") {
		t.Fatalf("failure should name the hash mismatch, got: %v", err)
	}
	if c.puts != s3MaxAttempts {
		t.Fatalf("body should be re-sent each attempt: %d puts, want %d", c.puts, s3MaxAttempts)
	}
}

// Transient storage damage heals on the re-send: attempt 1 stores garbage,
// attempt 2 stores faithfully and verification passes.
func TestPutRetriesUntilStoredCopyVerifies(t *testing.T) {
	c := newCorruptingS3(t, "once")
	sum, size, open := payload()
	if err := c.sink(t).Put(context.Background(), "k1", size, "application/zstd", sum, open); err != nil {
		t.Fatalf("second attempt stores faithfully, Put should succeed: %v", err)
	}
	if c.puts != 2 {
		t.Fatalf("puts = %d, want 2 (corrupt then clean)", c.puts)
	}
	if c.gets < 2 {
		t.Fatalf("each accepted PUT must be read back: gets = %d, want >= 2", c.gets)
	}
}

// An empty digest keeps the old contract: no read-back at all.
func TestEmptyDigestSkipsVerification(t *testing.T) {
	c := newCorruptingS3(t, "always")
	_, size, open := payload()
	if err := c.sink(t).Put(context.Background(), "k1", size, "application/zstd", "", open); err != nil {
		t.Fatalf("verification skipped, 2xx alone should succeed: %v", err)
	}
	if c.gets != 0 {
		t.Fatalf("no digest was given, nothing should be read back: gets = %d", c.gets)
	}
}
