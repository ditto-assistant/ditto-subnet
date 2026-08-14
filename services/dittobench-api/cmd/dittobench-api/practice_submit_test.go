package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/ratelimit"
	"github.com/ditto-assistant/dittobench-api/internal/scorer"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// newPracticeTestServer models the hosted practice deployment: public (SSRF
// guard armed, rate limiter live) and unable to execute anything — no screened
// images, no Docker daemon. harness_url is the only submission path it offers.
func newPracticeTestServer() *server {
	return &server{limiter: ratelimit.New(submitsPerWindow, submitWindow)}
}

// submitOn posts body to /v1/submit and returns the response recorder.
func submitOn(s *server, body string) *httptest.ResponseRecorder {
	rr := httptest.NewRecorder()
	s.handleSubmit(rr, httptest.NewRequest(http.MethodPost, "/v1/submit", strings.NewReader(body)))
	return rr
}

// The screened-image contract governs validator-side SOURCE BUILDS. A direct
// harness_url practice run builds nothing, so it must clear the contract on the
// public endpoint — not only in local/dev mode. Gating that exemption on
// allowPrivate took hosted practice offline entirely once v8 became the only
// supported version: every playground submission answered 403.
//
// The request below is stopped one check later, by the SSRF guard on a private
// address, which proves it got past the contract without starting a real run.
func TestPracticeSubmitAcceptsDirectHarnessOnV8(t *testing.T) {
	rr := submitOn(newPracticeTestServer(),
		`{"bench_version":8,"run_size":"small","harness_url":"http://127.0.0.1:9"}`)
	if rr.Code == http.StatusForbidden {
		t.Fatalf("hosted practice rejected a direct harness on v8: %s", rr.Body.String())
	}
	if strings.Contains(rr.Body.String(), "screener-built image") {
		t.Fatalf("the source-build contract fired on a path that builds nothing: %s", rr.Body.String())
	}
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected the SSRF guard to answer 400, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestPracticeSubmitDefaultsOmittedVersionToV9(t *testing.T) {
	version, message := requestedPracticeBenchVersion(0)
	if message != "" || version != protocol.BenchVersionV9 {
		t.Fatalf("practice default = (%d, %q), want (9, empty)", version, message)
	}

	rr := submitOn(newPracticeTestServer(),
		`{"run_size":"small","harness_url":"http://127.0.0.1:9"}`)
	if strings.Contains(rr.Body.String(), "bench_version") {
		t.Fatalf("omitted practice version was rejected instead of defaulted: %s", rr.Body.String())
	}
	if rr.Code != http.StatusBadRequest {
		t.Fatalf("expected the later SSRF guard to answer 400, got %d: %s", rr.Code, rr.Body.String())
	}
}

func TestPracticeSubmitAcceptsReleasedV10(t *testing.T) {
	version, message := requestedPracticeBenchVersion(protocol.BenchVersionV10)
	if message != "" || version != protocol.BenchVersionV10 {
		t.Fatalf("v10 practice = (%d, %q), want (%d, empty)", version, message, protocol.BenchVersionV10)
	}
	found := false
	for _, advertised := range supportedBenchVersions() {
		found = found || advertised == protocol.BenchVersionV10
	}
	if !found {
		t.Fatalf("released v10 is missing from capabilities: %v", supportedBenchVersions())
	}
}

// The exemption is keyed on the source kind, so both build modes stay gated on
// the same deployment.
func TestPracticeSubmitStillRejectsV8SourceBuilds(t *testing.T) {
	for _, tc := range []struct{ name, body string }{
		{"git", `{"bench_version":8,"run_size":"small","git_url":"https://example.com/miner.git"}`},
		{"tarball", `{"bench_version":8,"run_size":"small","tarball_url":"https://example.com/source.tgz"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rr := submitOn(newPracticeTestServer(), tc.body)
			if rr.Code != http.StatusForbidden {
				t.Fatalf("expected 403, got %d: %s", rr.Code, rr.Body.String())
			}
			if !strings.Contains(rr.Body.String(), "screener-built image") {
				t.Fatalf("expected the screened-image contract, got %s", rr.Body.String())
			}
		})
	}
}

// The canonical validator path is unaffected: it enforces the contract for every
// source kind, harness_url included, so a scored ticket can never be answered by
// a harness the miner runs themselves.
func TestCanonicalScoreStillRejectsDirectHarnessOnV8(t *testing.T) {
	const datasetSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	rr := httptest.NewRecorder()
	body := `{"bench_version":8,"inference_session_id":"test","seed":42,"run_size":"full",` +
		`"dataset_sha256":"` + datasetSHA + `","harness_url":"https://harness.example.com"}`
	newScoreTestServer().handleVersionedScore(rr, httptest.NewRequest(http.MethodPost, "/v2/score", strings.NewReader(body)))
	if rr.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d: %s", rr.Code, rr.Body.String())
	}
	if !strings.Contains(rr.Body.String(), "screener-built image") {
		t.Fatalf("expected the screened-image contract, got %s", rr.Body.String())
	}
}

func TestV9PracticeDoesNotClaimCanonicalBaseEvidence(t *testing.T) {
	req := submitRequest{
		BenchVersion: protocol.BenchVersionV9,
		HarnessURL:   "http://127.0.0.1:8080",
		RunSize:      "small",
	}
	if runScope(req) != scorer.ScopePractice {
		t.Fatal("v9 practice request was not classified as practice")
	}
	if requiresV9BaseEvidence(req) {
		t.Fatal("v9 practice must not manufacture signature-bound base evidence")
	}
}

func TestV9ScoredRequestStillRequiresCanonicalBaseEvidence(t *testing.T) {
	req := submitRequest{
		BenchVersion:          protocol.BenchVersionV9,
		ExpectedDatasetSHA256: strings.Repeat("a", 64),
		TarballSHA256:         strings.Repeat("b", 64),
		RunSize:               "full",
	}
	if runScope(req) != scorer.ScopeScored {
		t.Fatal("v9 dataset-pinned request was not classified as scored")
	}
	if !requiresV9BaseEvidence(req) {
		t.Fatal("canonical v9 scoring must retain signature-bound base evidence")
	}
}
