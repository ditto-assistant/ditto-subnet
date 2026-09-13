package main

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestHandleGenerateRequiresSupportedVersion uses the newest supported version
// plus one as the invalid placeholder and asserts the advertised range through
// the protocol helper, so a bench bump can neither turn this test red nor leave
// the service advertising a stale list.
func TestHandleGenerateRequiresSupportedVersion(t *testing.T) {
	for _, path := range []string{
		"/generate?seed=42&run_size=small&bench_version=" + strconv.Itoa(protocol.NewestSupportedBenchVersion()+1),
		"/generate?seed=42&run_size=small&bench_version=garbage",
	} {
		rr := httptest.NewRecorder()
		handleGenerate(rr, httptest.NewRequest(http.MethodPost, path, nil))
		if rr.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d, want 400", path, rr.Code)
		}
		if !strings.Contains(rr.Body.String(), "supported: "+protocol.SupportedBenchVersionList()) {
			t.Errorf("%s: stale supported-version response: %q", path, rr.Body.String())
		}
	}
}

// TestHandleGenerateAcceptsNewestVersion keeps the service honest the other
// way: the newest contract the module reproduces must be servable.
func TestHandleGenerateAcceptsNewestVersion(t *testing.T) {
	rr := httptest.NewRecorder()
	handleGenerate(rr, httptest.NewRequest(http.MethodPost, "/generate?seed=42&run_size=small&bench_version="+strconv.Itoa(protocol.NewestBenchVersion()), nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("newest version: status %d: %s", rr.Code, rr.Body.String())
	}
	if got := rr.Header().Get("X-Bench-Version"); got != strconv.Itoa(protocol.NewestBenchVersion()) {
		t.Fatalf("X-Bench-Version=%q, want %d", got, protocol.NewestBenchVersion())
	}
}

func TestHandleGenerateOmittedVersionIsDeprecatedV2Compatibility(t *testing.T) {
	rr := httptest.NewRecorder()
	handleGenerate(rr, httptest.NewRequest(http.MethodPost, "/generate?seed=42&run_size=small", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rr.Code, rr.Body.String())
	}
	if got := rr.Header().Get("X-Bench-Version"); got != "2" {
		t.Fatalf("X-Bench-Version=%q, want 2", got)
	}
	if got := rr.Header().Get("Deprecation"); got != "true" {
		t.Fatalf("Deprecation=%q, want true", got)
	}
}

func TestHandleGenerateVersionedVectors(t *testing.T) {
	for _, version := range []string{"2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"} {
		rr := httptest.NewRecorder()
		handleGenerate(rr, httptest.NewRequest(http.MethodPost, "/generate?seed=42&run_size=small&bench_version="+version, nil))
		if rr.Code != http.StatusOK {
			t.Fatalf("v%s: status %d: %s", version, rr.Code, rr.Body.String())
		}
		if rr.Header().Get("X-Dataset-SHA256") == "" {
			t.Fatalf("v%s: missing dataset digest", version)
		}
		if got := rr.Header().Get("X-Bench-Version"); got != version {
			t.Fatalf("v%s: X-Bench-Version=%q", version, got)
		}
	}
}
