package sandbox

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// tentry is a minimal tar member for building fixtures.
type tentry struct {
	name     string
	body     string
	typeflag byte
	linkname string
}

func makeTarGz(t *testing.T, entries []tentry) []byte {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for _, e := range entries {
		tf := e.typeflag
		if tf == 0 {
			tf = tar.TypeReg
		}
		hdr := &tar.Header{Name: e.name, Mode: 0o644, Typeflag: tf, Linkname: e.linkname}
		if tf == tar.TypeReg {
			hdr.Size = int64(len(e.body))
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatalf("write header %q: %v", e.name, err)
		}
		if tf == tar.TypeReg {
			if _, err := tw.Write([]byte(e.body)); err != nil {
				t.Fatalf("write body %q: %v", e.name, err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// serve returns a test server that responds to any GET with body.
func serve(t *testing.T, body []byte) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(body)
	}))
	t.Cleanup(srv.Close)
	return srv
}

// AllowPrivate is required because the test server binds 127.0.0.1, which the
// SSRF guard would otherwise refuse.
func localDocker() *LocalDocker { return &LocalDocker{AllowPrivate: true} }

func TestFetchTarball_RootDockerfile(t *testing.T) {
	data := makeTarGz(t, []tentry{
		{name: "Dockerfile", body: "FROM scratch\n"},
		{name: "src/main.rs", body: "fn main() {}"},
	})
	srv := serve(t, data)
	work := t.TempDir()

	ctxDir, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, work)
	if err != nil {
		t.Fatalf("fetchTarball: %v", err)
	}
	if ctxDir != work {
		t.Fatalf("context dir = %q, want extraction root %q", ctxDir, work)
	}
	if !isFile(filepath.Join(work, "Dockerfile")) {
		t.Fatal("Dockerfile not extracted at root")
	}
	if !isFile(filepath.Join(work, "src", "main.rs")) {
		t.Fatal("nested file not extracted")
	}
}

func TestFetchTarball_SingleSubdirDockerfile(t *testing.T) {
	data := makeTarGz(t, []tentry{
		{name: "harness/", typeflag: tar.TypeDir},
		{name: "harness/Dockerfile", body: "FROM scratch\n"},
		{name: "harness/Cargo.toml", body: "[package]"},
	})
	srv := serve(t, data)
	work := t.TempDir()

	ctxDir, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, work)
	if err != nil {
		t.Fatalf("fetchTarball: %v", err)
	}
	want := filepath.Join(work, "harness")
	if ctxDir != want {
		t.Fatalf("context dir = %q, want single subdir %q", ctxDir, want)
	}
}

func TestFetchTarball_NoDockerfile(t *testing.T) {
	data := makeTarGz(t, []tentry{{name: "README.md", body: "hi"}})
	srv := serve(t, data)
	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, t.TempDir()); err == nil {
		t.Fatal("expected error when no Dockerfile is present")
	}
}

// TestFetchTarball_ZipSlip is the key host-safety test: a member named "../…"
// must be rejected and must NOT write outside the extraction root.
func TestFetchTarball_ZipSlip(t *testing.T) {
	data := makeTarGz(t, []tentry{
		{name: "Dockerfile", body: "FROM scratch\n"},
		{name: "../pwned.txt", body: "escaped"},
	})
	srv := serve(t, data)
	parent := t.TempDir()
	work := filepath.Join(parent, "extract")
	if err := os.Mkdir(work, 0o755); err != nil {
		t.Fatal(err)
	}

	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, work); err == nil {
		t.Fatal("expected zip-slip to be rejected")
	}
	if _, err := os.Stat(filepath.Join(parent, "pwned.txt")); err == nil {
		t.Fatal("zip-slip escaped the extraction root")
	}
}

func TestFetchTarball_AbsolutePathRejected(t *testing.T) {
	data := makeTarGz(t, []tentry{{name: "/etc/pwned", body: "x"}})
	srv := serve(t, data)
	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, t.TempDir()); err == nil {
		t.Fatal("expected absolute-path member to be rejected")
	}
}

// TestFetchTarball_SymlinkSkipped: a symlink member (the classic escape vector)
// is silently skipped, not materialized.
func TestFetchTarball_SymlinkSkipped(t *testing.T) {
	data := makeTarGz(t, []tentry{
		{name: "Dockerfile", body: "FROM scratch\n"},
		{name: "evil", typeflag: tar.TypeSymlink, linkname: "/etc/passwd"},
	})
	srv := serve(t, data)
	work := t.TempDir()

	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, work); err != nil {
		t.Fatalf("fetchTarball: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(work, "evil")); err == nil {
		t.Fatal("symlink member should not have been created")
	}
}

func TestFetchTarball_SHA256(t *testing.T) {
	data := makeTarGz(t, []tentry{{name: "Dockerfile", body: "FROM scratch\n"}})
	srv := serve(t, data)
	sum := sha256.Sum256(data)
	good := hex.EncodeToString(sum[:])

	// Matching digest (upper-cased to prove case-insensitive compare) → ok.
	if _, err := localDocker().fetchTarball(context.Background(),
		Source{TarballURL: srv.URL, TarballSHA256: strings.ToUpper(good)}, t.TempDir()); err != nil {
		t.Fatalf("matching sha256 rejected: %v", err)
	}
	// Wrong digest → error.
	if _, err := localDocker().fetchTarball(context.Background(),
		Source{TarballURL: srv.URL, TarballSHA256: "00" + good[2:]}, t.TempDir()); err == nil {
		t.Fatal("expected sha256 mismatch to be rejected")
	}
}

func TestFetchTarball_CompressedSizeCap(t *testing.T) {
	// A tarball whose compressed size exceeds the (shrunk) cap is rejected.
	orig := maxTarballBytes
	maxTarballBytes = 256
	defer func() { maxTarballBytes = orig }()

	// Incompressible-ish payload so the gzip output clears 256 bytes.
	big := make([]byte, 4096)
	for i := range big {
		big[i] = byte(i * 7)
	}
	data := makeTarGz(t, []tentry{
		{name: "Dockerfile", body: "FROM scratch\n"},
		{name: "blob.bin", body: string(big)},
	})
	srv := serve(t, data)
	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, t.TempDir()); err == nil {
		t.Fatal("expected oversized compressed tarball to be rejected")
	}
}

func TestFetchTarball_ExtractedSizeCap(t *testing.T) {
	// Uncompressed output over the (shrunk) cap is rejected — the gzip-bomb guard.
	orig := maxExtractedBytes
	maxExtractedBytes = 512
	defer func() { maxExtractedBytes = orig }()

	data := makeTarGz(t, []tentry{
		{name: "Dockerfile", body: "FROM scratch\n"},
		{name: "big.txt", body: strings.Repeat("A", 4096)},
	})
	srv := serve(t, data)
	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, t.TempDir()); err == nil {
		t.Fatal("expected oversized extracted output to be rejected")
	}
}

func TestFetchTarball_HTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden) // e.g. an expired presigned URL
	}))
	t.Cleanup(srv.Close)
	if _, err := localDocker().fetchTarball(context.Background(), Source{TarballURL: srv.URL}, t.TempDir()); err == nil {
		t.Fatal("expected non-200 fetch to error")
	}
}

// TestDrainCounted is the CPU/decompression-DoS guard in isolation: the body of
// a skipped (non-regular) or directory entry must be charged against the
// remaining extraction budget so a large-bodied non-regular entry can't force
// gzip to inflate arbitrary bytes the per-file guard never sees. (tar.Writer
// refuses to attach a body to a symlink header, so the branch is exercised
// directly rather than through a fixture.)
func TestDrainCounted(t *testing.T) {
	// Body within budget: fully drained, counted, no error.
	n, err := drainCounted(bytes.NewReader(bytes.Repeat([]byte("A"), 400)), 512)
	if err != nil || n != 400 {
		t.Fatalf("within budget: n=%d err=%v, want n=400 err=nil", n, err)
	}
	// Body over the remaining budget: rejected (would push past the cap).
	if _, err := drainCounted(bytes.NewReader(bytes.Repeat([]byte("A"), 4096)), 512); err == nil {
		t.Fatal("expected an over-budget skipped-entry body to be rejected")
	}
	// A negative remaining budget (already at the cap) rejects any non-empty body.
	if _, err := drainCounted(bytes.NewReader([]byte("x")), -1); err == nil {
		t.Fatal("expected a body to be rejected when the budget is already spent")
	}
}

// TestExtractTarGz_CtxCanceled: a cancelled context stops extraction promptly
// instead of grinding through inflation.
func TestExtractTarGz_CtxCanceled(t *testing.T) {
	data := makeTarGz(t, []tentry{{name: "Dockerfile", body: "FROM scratch\n"}})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := extractTarGz(ctx, bytes.NewReader(data), t.TempDir()); err == nil {
		t.Fatal("expected extraction to honor a cancelled context")
	}
}

// TestRedactURL: the presigned signature query is stripped, so a leaked
// transport error can't carry the credential.
func TestRedactURL(t *testing.T) {
	url := "https://bucket.s3.amazonaws.com/agent.tar.gz?X-Amz-Signature=deadbeefSECRET"
	msg := `tarball fetch: Get "` + url + `": dial tcp: timeout`
	got := redactURL(msg, url)
	if strings.Contains(got, "SECRET") || strings.Contains(got, "X-Amz-Signature") {
		t.Fatalf("signature leaked after redaction: %q", got)
	}
	if !strings.Contains(got, "agent.tar.gz") {
		t.Fatalf("redaction dropped the non-secret path: %q", got)
	}
	// A URL with no query is returned unchanged.
	if s := redactURL("no query here", "https://host/x.tar.gz"); s != "no query here" {
		t.Fatalf("unexpected mutation: %q", s)
	}
}

func TestSafeJoin(t *testing.T) {
	root := "/tmp/root"
	ok := []string{"Dockerfile", "src/main.rs", "./a/b", "a/../b"}
	for _, n := range ok {
		if _, err := safeJoin(root, n); err != nil {
			t.Errorf("safeJoin(%q) unexpected error: %v", n, err)
		}
	}
	bad := []string{"", "../escape", "a/../../escape", "/abs/path", `\abs`}
	for _, n := range bad {
		if _, err := safeJoin(root, n); err == nil {
			t.Errorf("safeJoin(%q) should have been rejected", n)
		}
	}
}

func TestBuildContextDir(t *testing.T) {
	// Root Dockerfile.
	root := t.TempDir()
	writeFileT(t, filepath.Join(root, "Dockerfile"), "FROM scratch")
	if got, err := buildContextDir(root); err != nil || got != root {
		t.Fatalf("root case: got %q err %v", got, err)
	}

	// Single subdir with Dockerfile.
	base := t.TempDir()
	sub := filepath.Join(base, "harness")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFileT(t, filepath.Join(sub, "Dockerfile"), "FROM scratch")
	if got, err := buildContextDir(base); err != nil || got != sub {
		t.Fatalf("subdir case: got %q err %v", got, err)
	}

	// No Dockerfile anywhere → error.
	empty := t.TempDir()
	writeFileT(t, filepath.Join(empty, "README"), "hi")
	if _, err := buildContextDir(empty); err == nil {
		t.Fatal("expected error when no Dockerfile is present")
	}
}

func TestSafeTagTarball(t *testing.T) {
	// Presigned URL (query stripped, suffix stripped, sha suffixed).
	got := safeTag(Source{
		TarballURL:    "https://storage.googleapis.com/bucket/abc123.tar.gz?X-Goog-Signature=deadbeef",
		TarballSHA256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
	})
	if strings.ContainsAny(got, "?/") || strings.Contains(got, ".tar.gz") {
		t.Fatalf("tag %q leaks url/query/suffix", got)
	}
	if !strings.Contains(got, "abc123") || !strings.Contains(got, "0123456789ab") {
		t.Fatalf("tag %q missing name or sha prefix", got)
	}
}

func TestSafeTagScreenedImageIncludesImageID(t *testing.T) {
	base := Source{TarballURL: "https://example.test/shared/source.tar.gz"}
	first := base
	first.ScreenedImageID = "sha256:" + strings.Repeat("1", 64)
	second := base
	second.ScreenedImageID = "sha256:" + strings.Repeat("2", 64)

	firstTag := safeTag(first)
	secondTag := safeTag(second)
	if firstTag == secondTag {
		t.Fatalf("screened image IDs collided on local tag %q", firstTag)
	}
	if !strings.Contains(firstTag, "image-111111111111") {
		t.Fatalf("tag %q does not retain screened image ID", firstTag)
	}
}

func writeFileT(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}
