package main

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// newScoreTestServer returns a server that skips the rate limiter (allowPrivate)
// so the precondition branches of handleScore are reached directly. The
// preconditions all return before submitRunSize, so no store/sandbox is needed.
func newScoreTestServer() *server {
	return &server{allowPrivate: true}
}

func TestValidateBenchVersionResultRejectsContradictions(t *testing.T) {
	if err := validateBenchVersionResult(8, 8, &protocol.RunDetails{BenchVersion: 8}); err != nil {
		t.Fatalf("matching v8 result rejected: %v", err)
	}
	for _, tc := range []struct {
		artifact int
		details  *protocol.RunDetails
	}{
		{artifact: 7, details: &protocol.RunDetails{BenchVersion: 8}},
		{artifact: 8, details: &protocol.RunDetails{BenchVersion: 7}},
		{artifact: 8, details: nil},
	} {
		if err := validateBenchVersionResult(8, tc.artifact, tc.details); err == nil {
			t.Fatalf("accepted contradictory result: artifact=%d details=%+v", tc.artifact, tc.details)
		}
	}
}

// TestHandleScorePreconditions pins the canonical validator path's required
// fields: a pinned seed, the platform dataset_sha256, a run_size, and exactly one
// harness source. Each missing/invalid field is a distinct 400.
func TestHandleScorePreconditions(t *testing.T) {
	s := newScoreTestServer()
	const datasetSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	cases := []struct {
		name, body, wantSubstr string
	}{
		{"bad json", `{`, "invalid or oversized JSON body"},
		{"missing seed", `{"bench_version":7,"inference_session_id":"test","run_size":"full","dataset_sha256":"` + datasetSHA + `","harness_url":"http://h"}`, "seed is required"},
		{"missing hash", `{"bench_version":7,"inference_session_id":"test","seed":42,"run_size":"full","harness_url":"http://h"}`, "dataset_sha256 is required"},
		{"malformed hash", `{"bench_version":7,"inference_session_id":"test","seed":42,"run_size":"full","dataset_sha256":"abc","harness_url":"http://h"}`, "64 lowercase hex"},
		{"missing run_size", `{"bench_version":7,"inference_session_id":"test","seed":42,"dataset_sha256":"` + datasetSHA + `","harness_url":"http://h"}`, "run_size is required"},
		{"no source", `{"bench_version":7,"inference_session_id":"test","seed":42,"run_size":"full","dataset_sha256":"` + datasetSHA + `"}`, "exactly one of"},
		{"two sources", `{"bench_version":7,"inference_session_id":"test","seed":42,"run_size":"full","dataset_sha256":"` + datasetSHA + `","harness_url":"http://h","git_url":"http://g"}`, "exactly one of"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/v1/score", strings.NewReader(c.body))
			s.handleScore(rr, req)
			if rr.Code != http.StatusBadRequest {
				t.Fatalf("expected 400, got %d (body %s)", rr.Code, rr.Body.String())
			}
			if !strings.Contains(rr.Body.String(), c.wantSubstr) {
				t.Fatalf("expected error to mention %q, got %s", c.wantSubstr, rr.Body.String())
			}
		})
	}
}

func TestVersionedScoreRequiresSupportedBenchVersion(t *testing.T) {
	s := newScoreTestServer()
	base := `"seed":42,"run_size":"full","dataset_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","harness_url":"http://h"`
	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{name: "omitted", body: `{` + base + `}`, want: "bench_version is required"},
		{name: "old v1", body: `{"bench_version":1,` + base + `}`, want: "unsupported bench_version"},
		{name: "future", body: `{"bench_version":9,` + base + `}`, want: "unsupported bench_version"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rr := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/v2/score", strings.NewReader(tc.body))
			s.handleVersionedScore(rr, req)
			if rr.Code != http.StatusBadRequest || !strings.Contains(rr.Body.String(), tc.want) {
				t.Fatalf("expected 400 containing %q, got %d %s", tc.want, rr.Code, rr.Body.String())
			}
		})
	}
}

func TestVerifyDatasetHashFailsClosed(t *testing.T) {
	const datasetSHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if err := verifyDatasetHash(datasetSHA, datasetSHA, nil); err != nil {
		t.Fatalf("exact hash rejected: %v", err)
	}
	if err := verifyDatasetHash(datasetSHA, "", errors.New("forced hash failure")); err == nil || !strings.Contains(err.Error(), "hashing failed") {
		t.Fatalf("hashing failure did not fail closed: %v", err)
	}
	if err := verifyDatasetHash(datasetSHA, strings.Repeat("b", 64), nil); err == nil || !strings.Contains(err.Error(), "mismatch") {
		t.Fatalf("hash mismatch accepted: %v", err)
	}
}

func TestRetiredBenchmarkVersionsAreRejected(t *testing.T) {
	s := newScoreTestServer()
	s.requireTicketInference = true
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/v2/score",
		strings.NewReader(`{"bench_version":6}`),
	)
	s.handleVersionedScore(rr, req)
	if rr.Code != http.StatusBadRequest || !strings.Contains(rr.Body.String(), "unsupported bench_version") {
		t.Fatalf("expected retired-version 400, got %d %s", rr.Code, rr.Body.String())
	}
}

func TestBenchmarkV7IntrinsicallyRequiresTicketInference(t *testing.T) {
	s := newScoreTestServer()
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/v2/score",
		strings.NewReader(`{"bench_version":7}`),
	)
	s.handleVersionedScore(rr, req)
	if rr.Code != http.StatusServiceUnavailable ||
		!strings.Contains(rr.Body.String(), "ticket inference session is required") {
		t.Fatalf("expected intrinsic v7 ticket inference 503, got %d %s", rr.Code, rr.Body.String())
	}
}

func TestBenchmarkV8IntrinsicallyRequiresTicketInference(t *testing.T) {
	s := newScoreTestServer()
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v2/score", strings.NewReader(`{"bench_version":8}`))
	s.handleVersionedScore(rr, req)
	if rr.Code != http.StatusServiceUnavailable || !strings.Contains(rr.Body.String(), "ticket inference session is required") {
		t.Fatalf("expected intrinsic v8 ticket inference 503, got %d %s", rr.Code, rr.Body.String())
	}
}

func TestRequestedBenchVersionSupportsOnlyV7AndV8(t *testing.T) {
	if got, msg := requestedBenchVersion(0, false); got != 7 || msg != "" {
		t.Fatalf("omitted practice version must select v7, got (%d, %q)", got, msg)
	}
	for _, version := range []int{7, 8} {
		if got, msg := requestedBenchVersion(version, true); got != version || msg != "" {
			t.Fatalf("explicit v%d rejected: (%d, %q)", version, got, msg)
		}
	}
	for version := 2; version <= 6; version++ {
		if got, msg := requestedBenchVersion(version, true); got != 0 || !strings.Contains(msg, "unsupported") {
			t.Fatalf("retired v%d accepted: (%d, %q)", version, got, msg)
		}
	}
}
