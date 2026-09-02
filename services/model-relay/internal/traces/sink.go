package traces

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// Sink is one storage destination for finished spool files.
type Sink interface {
	// Name is the operator-facing label (e.g. "hippius", "backblaze").
	Name() string
	// Required sinks gate local deletion: a file stays on disk until every
	// required sink has it. Optional sinks are best-effort mirrors.
	Required() bool
	// Ensure verifies the destination is reachable and the bucket exists,
	// creating it when the credentials allow. Called once at startup.
	Ensure(ctx context.Context) error
	// Put stores one object and, when sha256Hex is non-empty, verifies the
	// stored copy by reading it back before reporting success — a PUT the
	// destination acknowledged but mangled is a FAILED put. open must return
	// a fresh reader each call so a retry can replay the body. sha256Hex ""
	// skips verification (callers without a precomputed digest).
	Put(ctx context.Context, key string, size int64, contentType, sha256Hex string, open func() (io.ReadCloser, error)) error
}

// S3Config configures an S3-compatible sink (Hippius, Backblaze B2, AWS,
// MinIO, GCS XML API).
type S3Config struct {
	Name            string
	Endpoint        string // https://eu-central-1.hippius.com, https://s3.us-west-004.backblazeb2.com
	Region          string // "decentralized" for Hippius, the B2 region for Backblaze
	Bucket          string
	AccessKeyID     string
	SecretAccessKey string
	Required        bool
	// PathStyle addresses objects as endpoint/bucket/key (default, and what
	// Hippius needs); false uses bucket.endpoint/key.
	PathStyle bool
	// Prefix is prepended to every key (no leading slash; trailing slash
	// optional). Lets several relays or environments share a bucket.
	Prefix string
}

// S3Sink implements Sink over presigned-URL SigV4 (see presignURL for why).
type S3Sink struct {
	cfg    S3Config
	client *http.Client
	now    func() time.Time
}

const (
	s3PresignTTL   = 15 * time.Minute
	s3MaxAttempts  = 4
	s3AttemptDelay = 500 * time.Millisecond
)

// NewS3Sink validates cfg and returns a sink.
func NewS3Sink(cfg S3Config, client *http.Client) (*S3Sink, error) {
	if cfg.Name == "" {
		return nil, errors.New("s3 sink: name is required")
	}
	u, err := url.Parse(strings.TrimRight(cfg.Endpoint, "/"))
	if err != nil || !u.IsAbs() || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, fmt.Errorf("s3 sink %s: endpoint must be an absolute http(s) URL, got %q", cfg.Name, cfg.Endpoint)
	}
	if cfg.Bucket == "" || strings.ContainsAny(cfg.Bucket, "/ ") {
		return nil, fmt.Errorf("s3 sink %s: bucket is required and must not contain '/' or spaces", cfg.Name)
	}
	if cfg.AccessKeyID == "" || cfg.SecretAccessKey == "" {
		return nil, fmt.Errorf("s3 sink %s: access key id and secret are required", cfg.Name)
	}
	if cfg.Region == "" {
		cfg.Region = "us-east-1"
	}
	cfg.Endpoint = u.String()
	cfg.Prefix = strings.Trim(cfg.Prefix, "/")
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Minute}
	}
	return &S3Sink{cfg: cfg, client: client, now: time.Now}, nil
}

func (s *S3Sink) Name() string   { return s.cfg.Name }
func (s *S3Sink) Required() bool { return s.cfg.Required }

// objectURL is the unsigned URL for key ("" for the bucket itself).
func (s *S3Sink) objectURL(key string) (*url.URL, error) {
	base, err := url.Parse(s.cfg.Endpoint)
	if err != nil {
		return nil, err
	}
	full := key
	if s.cfg.Prefix != "" && key != "" {
		full = s.cfg.Prefix + "/" + key
	}
	if s.cfg.PathStyle {
		base.Path = "/" + s.cfg.Bucket
		if full != "" {
			base.Path += "/" + full
		}
	} else {
		base.Host = s.cfg.Bucket + "." + base.Host
		base.Path = "/" + full
	}
	base.RawPath = ""
	return base, nil
}

func (s *S3Sink) presign(method, key string) (string, error) {
	target, err := s.objectURL(key)
	if err != nil {
		return "", err
	}
	return presignURL(method, target, presignCredentials{
		AccessKeyID:     s.cfg.AccessKeyID,
		SecretAccessKey: s.cfg.SecretAccessKey,
		Region:          s.cfg.Region,
	}, s.now(), s3PresignTTL), nil
}

// Ensure HEADs the bucket and creates it on 404. Any other failure is
// returned verbatim so a credential or endpoint problem is visible at boot
// rather than on the first upload.
func (s *S3Sink) Ensure(ctx context.Context) error {
	status, body, err := s.do(ctx, "HEAD", "", 0, "", nil)
	if err != nil {
		return fmt.Errorf("s3 sink %s: head bucket: %w", s.cfg.Name, err)
	}
	switch {
	case status >= 200 && status < 300:
		return nil
	case status == http.StatusNotFound:
	default:
		return fmt.Errorf("s3 sink %s: head bucket %s: http %d %s", s.cfg.Name, s.cfg.Bucket, status, truncate(body, 200))
	}
	status, body, err = s.do(ctx, "PUT", "", 0, "", nil)
	if err != nil {
		return fmt.Errorf("s3 sink %s: create bucket: %w", s.cfg.Name, err)
	}
	if status >= 200 && status < 300 || status == http.StatusConflict {
		return nil
	}
	return fmt.Errorf("s3 sink %s: create bucket %s: http %d %s", s.cfg.Name, s.cfg.Bucket, status, truncate(body, 200))
}

// Put uploads with bounded retries on transport errors and on the status
// family the Python client retries (429/5xx, plus Hippius' transient 402
// billing-balance hiccup). A 2xx alone is not success: uploads are presigned
// with UNSIGNED-PAYLOAD, so nothing on the wire protects the body, and the
// bucket audit of 2026-09-02 found ~1.5% of stored objects stably corrupt
// (mid-stream damage and truncation) across every day of the bucket's life.
// When the caller supplies the object's sha256, each accepted PUT is read
// back and hashed; a mismatch counts as a failed attempt and the body is
// re-sent.
func (s *S3Sink) Put(ctx context.Context, key string, size int64, contentType, sha256Hex string, open func() (io.ReadCloser, error)) error {
	var lastErr error
	for attempt := 1; attempt <= s3MaxAttempts; attempt++ {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		status, body, err := s.do(ctx, "PUT", key, size, contentType, open)
		switch {
		case err != nil:
			lastErr = err
		case status >= 200 && status < 300:
			if sha256Hex == "" {
				return nil
			}
			verr := s.verifyObject(ctx, key, size, sha256Hex)
			if verr == nil {
				return nil
			}
			lastErr = verr
		case shouldRetryStatus(status, body):
			lastErr = fmt.Errorf("http %d %s", status, truncate(body, 200))
		default:
			return fmt.Errorf("s3 sink %s: put %s: http %d %s", s.cfg.Name, key, status, truncate(body, 200))
		}
		if attempt < s3MaxAttempts {
			delay := s3AttemptDelay << (attempt - 1)
			if delay > 8*time.Second {
				delay = 8 * time.Second
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}
	}
	return fmt.Errorf("s3 sink %s: put %s: %w", s.cfg.Name, key, lastErr)
}

// verifyObject reads the object back and compares size and sha256 against
// what was just sent. Any disagreement — wrong bytes, wrong length, or a
// failure to read the copy at all — is returned as an error so the caller's
// retry loop re-sends the body.
func (s *S3Sink) verifyObject(ctx context.Context, key string, size int64, sha256Hex string) error {
	signed, err := s.presign("GET", key)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, signed, nil)
	if err != nil {
		return err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return fmt.Errorf("verify %s: %w", key, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		text, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("verify %s: http %d %s", key, resp.StatusCode, truncate(string(text), 200))
	}
	hasher := sha256.New()
	got, err := io.Copy(hasher, resp.Body)
	if err != nil {
		return fmt.Errorf("verify %s: read stored copy: %w", key, err)
	}
	if got != size {
		return fmt.Errorf("verify %s: stored %d bytes, sent %d", key, got, size)
	}
	if sum := hex.EncodeToString(hasher.Sum(nil)); sum != sha256Hex {
		return fmt.Errorf("verify %s: stored sha256 %s != sent %s", key, sum, sha256Hex)
	}
	return nil
}

// do issues one presigned request and returns status + a bounded body.
func (s *S3Sink) do(ctx context.Context, method, key string, size int64, contentType string,
	open func() (io.ReadCloser, error)) (int, string, error) {
	signed, err := s.presign(method, key)
	if err != nil {
		return 0, "", err
	}
	var body io.ReadCloser
	if open != nil {
		body, err = open()
		if err != nil {
			return 0, "", err
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, signed, body)
	if err != nil {
		if body != nil {
			_ = body.Close()
		}
		return 0, "", err
	}
	if open != nil {
		req.ContentLength = size
		if contentType != "" {
			req.Header.Set("Content-Type", contentType)
		}
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	text, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	return resp.StatusCode, string(text), nil
}

func shouldRetryStatus(status int, body string) bool {
	switch status {
	case http.StatusTooManyRequests, http.StatusInternalServerError, http.StatusBadGateway,
		http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return true
	case http.StatusPaymentRequired:
		lowered := strings.ToLower(body)
		return strings.Contains(lowered, "uploadnotpermitted") && strings.Contains(lowered, "failed to fetch billing balance")
	}
	return false
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

// fileOpener returns an open func over path for Sink.Put.
func fileOpener(path string) func() (io.ReadCloser, error) {
	return func() (io.ReadCloser, error) { return os.Open(path) }
}
