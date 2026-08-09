package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func testConfig() viewerConfig {
	return viewerConfig{
		DefaultSeed: 42, DefaultRunSize: "small", DefaultBenchVersion: protocol.BenchVersionV8,
		SupportedVersions: []int{protocol.BenchVersionV7, protocol.BenchVersionV8, protocol.BenchVersionV9},
	}
}

func TestDatasetEndpointReturnsCanonicalArtifact(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/dataset?seed=123456789&run_size=small&bench_version=8", nil)
	rec := httptest.NewRecorder()
	newHandler(testConfig()).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var response datasetResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.Review.Artifact.Seed != 123456789 || response.Review.Artifact.BenchVersion != 8 {
		t.Fatalf("wrong identity: seed=%d version=%d", response.Review.Artifact.Seed, response.Review.Artifact.BenchVersion)
	}
	if response.Summary.ToolCases != 6 || response.Summary.MemoryCases != 24 {
		t.Fatalf("unexpected small counts: %+v", response.Summary)
	}
	if response.Summary.MemoryRecords == 0 || response.Summary.PrerequisiteRecords == 0 {
		t.Fatalf("viewer omitted generated records: %+v", response.Summary)
	}
	sha, _, err := response.Review.Artifact.SHA256Hex()
	if err != nil {
		t.Fatal(err)
	}
	if response.DatasetSHA256 != sha {
		t.Fatalf("response sha=%s canonical=%s", response.DatasetSHA256, sha)
	}
	if got := rec.Header().Get("Content-Security-Policy"); got == "" {
		t.Fatal("missing content security policy")
	}
}

func TestDatasetEndpointRejectsUnsupportedInputs(t *testing.T) {
	for _, target := range []string{
		"/api/dataset?seed=nope", "/api/dataset?bench_version=6", "/api/dataset?run_size=huge",
	} {
		req := httptest.NewRequest(http.MethodGet, target, nil)
		rec := httptest.NewRecorder()
		newHandler(testConfig()).ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s status=%d body=%s", target, rec.Code, rec.Body.String())
		}
	}
}

func TestViewerIsLoopbackOnly(t *testing.T) {
	for _, addr := range []string{"127.0.0.1:8787", "localhost:8787", "[::1]:8787"} {
		if err := validateConfig(addr, testConfig()); err != nil {
			t.Errorf("%s: %v", addr, err)
		}
	}
	for _, addr := range []string{"0.0.0.0:8787", ":8787", "192.0.2.1:8787"} {
		if err := validateConfig(addr, testConfig()); err == nil {
			t.Errorf("%s unexpectedly accepted", addr)
		}
	}
}

func TestViewerIndexAndAssetsAreEmbedded(t *testing.T) {
	for _, target := range []string{"/", "/assets/app.js", "/assets/styles.css"} {
		req := httptest.NewRequest(http.MethodGet, target, nil)
		rec := httptest.NewRecorder()
		newHandler(testConfig()).ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Errorf("%s status=%d", target, rec.Code)
		}
	}
}

func TestViewerExposesMemoryReviewOrdering(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	newHandler(testConfig()).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("index status=%d", rec.Code)
	}
	for _, want := range []string{`id="memory-sort"`, `value="time"`, `value="category"`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("viewer index does not expose %q", want)
		}
	}

	req = httptest.NewRequest(http.MethodGet, "/assets/app.js", nil)
	rec = httptest.NewRecorder()
	newHandler(testConfig()).ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("app status=%d", rec.Code)
	}
	for _, want := range []string{"compareMemoryItems", "memoryCategory", "formatTimestamp"} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("viewer app does not implement %q", want)
		}
	}
}
