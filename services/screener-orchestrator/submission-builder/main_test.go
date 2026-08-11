package main

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

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
