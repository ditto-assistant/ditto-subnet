package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
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

func TestNamedConfigDigestAcceptsKanikoTarNames(t *testing.T) {
	hexDigest := strings.Repeat("ab", 32)
	cases := map[string]string{
		hexDigest + ".json":         hexDigest,
		"sha256:" + hexDigest:       hexDigest,
		"blobs/sha256/" + hexDigest: hexDigest,
		"./sha256:" + hexDigest:     hexDigest,
		"index.json":                "",
		"sha256:not-a-digest":       "",
	}
	for name, want := range cases {
		if got := namedConfigDigest(name); got != want {
			t.Fatalf("%s: got %q want %q", name, got, want)
		}
	}
}

func TestConfigDigestFromClassicDockerSave(t *testing.T) {
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	digest := sha256.Sum256(config)
	hexDigest := hex.EncodeToString(digest[:])
	manifest, err := json.Marshal([]map[string]any{{
		"Config":   hexDigest + ".json",
		"RepoTags": []string{"ditto-screen/11111111-1111-4111-8111-111111111111-22222222-2222-4222-8222-222222222222:latest"},
		"Layers":   []string{},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, member := range []struct {
		name string
		body []byte
	}{
		{name: hexDigest + ".json", body: config},
		{name: "manifest.json", body: manifest},
	} {
		if err := writer.WriteHeader(&tar.Header{Name: member.name, Size: int64(len(member.body)), Mode: 0o600}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(member.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, archive.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := configDigestFromDockerSave(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != "sha256:"+hexDigest {
		t.Fatalf("unexpected config digest: %s", got)
	}
}

func TestConfigDigestFromKanikoGoContainerRegistryTar(t *testing.T) {
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	digest := sha256.Sum256(config)
	hexDigest := hex.EncodeToString(digest[:])
	manifest, err := json.Marshal([]map[string]any{{
		"Config":   "sha256:" + hexDigest,
		"RepoTags": []string{"ditto-screen/11111111-1111-4111-8111-111111111111-22222222-2222-4222-8222-222222222222:latest"},
		"Layers":   []string{},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, member := range []struct {
		name string
		body []byte
	}{
		{name: "sha256:" + hexDigest, body: config},
		{name: "manifest.json", body: manifest},
	} {
		if err := writer.WriteHeader(&tar.Header{Name: member.name, Size: int64(len(member.body)), Mode: 0o600}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(member.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, archive.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := configDigestFromDockerSave(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != "sha256:"+hexDigest {
		t.Fatalf("unexpected config digest: %s", got)
	}
}

func TestConfigDigestFromGzipDockerSave(t *testing.T) {
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	digest := sha256.Sum256(config)
	hexDigest := hex.EncodeToString(digest[:])
	manifest, err := json.Marshal([]map[string]any{{
		"Config":   hexDigest + ".json",
		"RepoTags": []string{"ditto-screen/11111111-1111-4111-8111-111111111111-22222222-2222-4222-8222-222222222222:latest"},
		"Layers":   []string{},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, member := range []struct {
		name string
		body []byte
	}{
		{name: hexDigest + ".json", body: config},
		{name: "manifest.json", body: manifest},
	} {
		if err := writer.WriteHeader(&tar.Header{Name: member.name, Size: int64(len(member.body)), Mode: 0o600}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(member.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	var gzipped bytes.Buffer
	zipper := gzip.NewWriter(&gzipped)
	if _, err := zipper.Write(archive.Bytes()); err != nil {
		t.Fatal(err)
	}
	if err := zipper.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(path, gzipped.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := configDigestFromDockerSave(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != "sha256:"+hexDigest {
		t.Fatalf("unexpected config digest: %s", got)
	}
}
