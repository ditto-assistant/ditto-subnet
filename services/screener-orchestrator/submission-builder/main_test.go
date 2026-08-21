package main

import (
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSuccessHoldWaitsForControllerDelete(t *testing.T) {
	if successHoldDuration < 20*time.Minute {
		t.Fatalf("success hold must outlast delete retries: %s", successHoldDuration)
	}
	if successHoldDuration > 45*time.Minute {
		t.Fatalf("success hold cap is too long: %s", successHoldDuration)
	}
}

func TestDecodeURLRequiresTLS(t *testing.T) {
	encoded := "aHR0cDovL2V4YW1wbGUudGVzdC9hcnRpZmFjdA=="
	if _, err := decodeURL(encoded); err == nil {
		t.Fatal("non-TLS URL was accepted")
	}
}

func TestSanitizedEnvironmentStripsCapabilities(t *testing.T) {
	t.Setenv("DITTO_BUILD_JOB_TOKEN", "do-not-forward")
	t.Setenv("UNRELATED_SECRET", "do-not-forward")
	t.Setenv("GO_TEST_VISIBLE", "allowed")

	joined := strings.Join(sanitizedEnvironment(), "\n")
	if strings.Contains(joined, "do-not-forward") {
		t.Fatal("secret-bearing environment reached Kaniko")
	}
	if !strings.Contains(joined, "GO_TEST_VISIBLE=allowed") {
		t.Fatal("ordinary environment was unexpectedly removed")
	}
}

func TestFailureStageNameIsBoundedAndPublicSafe(t *testing.T) {
	private := errors.New("private source URL and token must never be emitted")
	if got := failureStageName(stageFailure("UPLOAD", private)); got != "UPLOAD" {
		t.Fatalf("unexpected stage: %s", got)
	}
	if got := failureStageName(private); got != "CONTRACT" {
		t.Fatalf("unexpected default stage: %s", got)
	}
	if got := failureExitCode("UPLOAD"); got != 74 {
		t.Fatalf("unexpected upload exit code: %d", got)
	}
	if got := failureExitCode("anything else"); got != 76 {
		t.Fatalf("unexpected contract exit code: %d", got)
	}
}

func TestDownloadVerifiedRejectsDigestMismatchAndRemovesFile(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("miner source"))
	}))
	defer server.Close()

	path := filepath.Join(t.TempDir(), "source.tar.gz")
	err := downloadVerified(server.Client(), server.URL, path, strings.Repeat("0", 64))
	if err == nil {
		t.Fatal("digest mismatch was accepted")
	}
	if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
		t.Fatal("mismatched source remained on disk")
	}
}

func TestHashBoundedReportsExactBytesAndDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "image.tar")
	content := []byte("oci archive")
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
	digest, size, err := hashBounded(path, int64(len(content)))
	if err != nil {
		t.Fatal(err)
	}
	expected := sha256.Sum256(content)
	if digest != hex.EncodeToString(expected[:]) || size != int64(len(content)) {
		t.Fatalf("unexpected digest/size: %s %d", digest, size)
	}
}

func TestPrepareStoredImageGzipsUncompressedTar(t *testing.T) {
	src := filepath.Join(t.TempDir(), "image.tar")
	dst := filepath.Join(t.TempDir(), "image.tar.gz")
	content := []byte("oci archive")
	if err := os.WriteFile(src, content, 0o600); err != nil {
		t.Fatal(err)
	}
	path, digest, size, err := prepareStoredImage(src, dst, 1024, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if path != dst {
		t.Fatalf("uncompressed tar was not gzipped: %s", path)
	}
	stored, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) < 2 || stored[0] != 0x1f || stored[1] != 0x8b {
		t.Fatal("stored object is not gzip")
	}
	expected := sha256.Sum256(stored)
	if digest != hex.EncodeToString(expected[:]) || size != int64(len(stored)) {
		t.Fatalf("gzip digest/size pin the uncompressed tar: %s %d", digest, size)
	}
	gz, err := gzip.NewReader(bytes.NewReader(stored))
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := io.ReadAll(gz)
	if err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(decoded, content) {
		t.Fatalf("gzip payload = %q, want %q", decoded, content)
	}
}

func TestPrepareStoredImagePassesThroughExistingGzip(t *testing.T) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	if _, err := gz.Write([]byte("oci archive")); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	src := filepath.Join(t.TempDir(), "image.tar")
	dst := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(src, buf.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	path, digest, size, err := prepareStoredImage(src, dst, 1024, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if path != src {
		t.Fatal("already-gzipped Kaniko output was re-compressed")
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatal("pass-through gzip wrote a destination file")
	}
	expected := sha256.Sum256(buf.Bytes())
	if digest != hex.EncodeToString(expected[:]) || size != int64(buf.Len()) {
		t.Fatalf("unexpected pass-through digest/size: %s %d", digest, size)
	}
}

func TestPrepareStoredImageRejectsOversizeUncompressed(t *testing.T) {
	src := filepath.Join(t.TempDir(), "image.tar")
	dst := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(src, []byte("oci archive"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := prepareStoredImage(src, dst, 4, 1024); err == nil {
		t.Fatal("oversize uncompressed tar was accepted")
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatal("failed gzip left a destination file")
	}
}
