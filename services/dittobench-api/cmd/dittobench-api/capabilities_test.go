package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/llm"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testSourceRevision = "0123456789abcdef0123456789abcdef01234567"

// capabilitiesOf serves /v1/capabilities from s and decodes a 200 response.
func capabilitiesOf(t *testing.T, s *server) capabilitiesResponse {
	t.Helper()
	rr := httptest.NewRecorder()
	s.handleCapabilities(rr, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var got capabilitiesResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	return got
}

func TestCapabilitiesReportBoundReleaseIdentity(t *testing.T) {
	s := &server{softwareVersion: "0.10.0", sourceRevision: testSourceRevision}
	rr := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil)
	s.handleCapabilities(rr, req)
	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rr.Code, rr.Body.String())
	}
	var got capabilitiesResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.SoftwareVersion != "0.10.0" || got.SourceRevision != testSourceRevision {
		t.Fatalf("wrong identity: %+v", got)
	}
	if got.FullRunCapacity != maxConcurrentRuns {
		t.Fatalf("full-run capacity = %d, want %d", got.FullRunCapacity, maxConcurrentRuns)
	}
	if got.MemoryPhaseCapacity != maxConcurrentMemoryPhases {
		t.Fatalf("memory-phase capacity = %d, want %d", got.MemoryPhaseCapacity, maxConcurrentMemoryPhases)
	}
	// The validator is technically ready for v7 (embedded quality-only manifest),
	// so it advertises a valid calibration readiness with the reviewed aggregate
	// route. Advertisement is capability, not activation; dispatch is the
	// platform's rollout decision.
	if !efficiency.ValidV7CalibrationReadiness(got.V7Calibration) || got.V7Calibration.ManifestSHA256 == "" {
		t.Fatalf("technically-ready v7 must advertise a valid calibration readiness: %+v", got.V7Calibration)
	}
	if !strings.Contains(rr.Body.String(), `"profile_revision":"openrouter-route-a471cd87ae7df5b9-v1"`) {
		t.Fatalf("advertised v7 must expose its reviewed aggregate route: %s", rr.Body.String())
	}
	want := []int{}
	// v7 is advertised iff the validator is technically ready (the embedded
	// quality-only manifest). No env var / activation flag gates advertisement;
	// dispatch is the platform's rollout decision.
	if efficiency.ProductionReadyForVersion(7) {
		want = append(want, 7)
	}
	if efficiency.ProductionReadyForVersion(8) {
		want = append(want, 8)
	}
	if len(got.SupportedBenchVersions) != len(want) {
		t.Fatalf("wrong supported versions: %v (want %v)", got.SupportedBenchVersions, want)
	}
	for i, v := range want {
		if got.SupportedBenchVersions[i] != v {
			t.Fatalf("wrong supported versions: %v (want %v)", got.SupportedBenchVersions, want)
		}
	}
	if rr.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("capabilities response must not be cached")
	}
}

func TestV8CapabilityIsQualityOnlyAndFailClosed(t *testing.T) {
	if efficiency.ReadyForV8QualityOnly(efficiency.QualityOnlyContract{}) {
		t.Fatal("missing v8 contract was accepted")
	}
	contract := efficiency.V8ContractSnapshot()
	if !efficiency.ReadyForV8QualityOnly(contract) || !efficiency.ProductionReadyForVersion(8) {
		t.Fatal("embedded v8 quality-only contract must be technically ready")
	}
	if contract.ScorerTokenPolicy != "quality-only" || contract.EfficiencyAuthority != "ditto-platform-relative-cohort-v1" {
		t.Fatalf("v8 must delegate efficiency to the platform cohort: %+v", contract)
	}
	readiness := efficiency.V8Readiness()
	if !efficiency.ValidV8Readiness(readiness) || readiness.Provenance != efficiency.ProvenanceReviewedContract {
		t.Fatalf("invalid v8 readiness: %+v", readiness)
	}

	s := &server{softwareVersion: "0.10.0", sourceRevision: testSourceRevision}
	got := capabilitiesOf(t, s)
	found := false
	for _, version := range got.SupportedBenchVersions {
		found = found || version == protocol.BenchVersionV8
	}
	if !found {
		t.Fatal("technically ready v8 was not advertised")
	}
}

func TestV8CapabilityRequiresLiveExecutorReadiness(t *testing.T) {
	s := &server{
		softwareVersion: "0.10.0",
		sourceRevision:  testSourceRevision,
		sandbox: &diagnosticSandbox{
			v8IsolationErr: errors.New("rootless policy is not enabled"),
		},
	}
	got := capabilitiesOf(t, s)
	foundV7, foundV8 := false, false
	for _, version := range got.SupportedBenchVersions {
		foundV7 = foundV7 || version == protocol.BenchVersionV7
		foundV8 = foundV8 || version == protocol.BenchVersionV8
	}
	if efficiency.ProductionReadyForVersion(protocol.BenchVersionV7) && !foundV7 {
		t.Fatal("the additive rootless migration must not withdraw v7")
	}
	if foundV8 {
		t.Fatal("v8 was advertised without a verified rootless executor")
	}
}

// v7 capability advertisement is gated ONLY on technical readiness
// (ReadyForV7QualityOnly), exactly like v5/v6 gate on their reviewed manifests.
// There is no validator-side activation flag: a ready validator advertises v7,
// and the platform's benchmark rollout decides whether v7 is dispatched.
func TestV7CapabilityGatedOnTechnicalReadinessOnly(t *testing.T) {
	if !efficiency.ProductionReadyForVersion(7) {
		t.Fatal("embedded quality-only v7 manifest must be technically ready")
	}
	s := &server{softwareVersion: "0.10.0", sourceRevision: testSourceRevision}
	rr := httptest.NewRecorder()
	s.handleCapabilities(rr, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	var got capabilitiesResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, v := range got.SupportedBenchVersions {
		if v == protocol.BenchVersionV7 {
			found = true
		}
	}
	// Advertisement follows readiness with no env var involved.
	if found != efficiency.ProductionReadyForVersion(7) {
		t.Fatalf("v7 advertisement must track technical readiness: advertised=%v ready=%v",
			found, efficiency.ProductionReadyForVersion(7))
	}
	if !efficiency.ValidV7CalibrationReadiness(got.V7Calibration) {
		t.Fatalf("ready v7 must expose a valid calibration readiness: %+v", got.V7Calibration)
	}
}

func TestV7CalibrationReadinessRequiresManifestAndExactRoutes(t *testing.T) {
	readiness := efficiency.CalibrationReadiness{
		ManifestSHA256: strings.Repeat("a", 64),
		SupportedRoutes: []efficiency.CalibrationRouteIdentity{{
			Provider: "openrouter", ProfileRevision: "openrouter-route-a471cd87ae7df5b9-v1",
			Model: llm.V7HarnessModel,
		}},
	}
	if !efficiency.ValidV7CalibrationReadiness(readiness) {
		t.Fatal("complete v7 calibration identity rejected")
	}
	readiness.SupportedRoutes[0].ProfileRevision = "openrouter-route-8efde5ce9f5a4e58-v1"
	if efficiency.ValidV7CalibrationReadiness(readiness) {
		t.Fatal("v7 calibration identity accepted superseded aggregate route")
	}
	readiness.SupportedRoutes[0].ProfileRevision = "openrouter-route-a471cd87ae7df5b9-v1"
	readiness.ManifestSHA256 = ""
	if efficiency.ValidV7CalibrationReadiness(readiness) {
		t.Fatal("v7 calibration identity accepted without reviewed manifest digest")
	}
	readiness.ManifestSHA256 = strings.Repeat("a", 64)
	readiness.SupportedRoutes[0].Provider = "groq"
	if efficiency.ValidV7CalibrationReadiness(readiness) {
		t.Fatal("v7 calibration identity accepted an unreviewed provider route")
	}
	readiness.SupportedRoutes[0].Provider = "openrouter"
	readiness.SupportedRoutes = append(readiness.SupportedRoutes, readiness.SupportedRoutes[0])
	if efficiency.ValidV7CalibrationReadiness(readiness) {
		t.Fatal("v7 calibration identity accepted an extra route")
	}
}

func TestCapabilitiesFailClosedOnUnboundIdentity(t *testing.T) {
	for _, revision := range []string{"", "not-a-sha", "0123456789ABCDEF0123456789ABCDEF01234567", "0000000000000000000000000000000000000000"} {
		s := &server{softwareVersion: "0.10.0", sourceRevision: revision}
		rr := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil)
		s.handleCapabilities(rr, req)
		if rr.Code != http.StatusServiceUnavailable {
			t.Fatalf("revision %q: expected 503, got %d", revision, rr.Code)
		}
	}
	// Fail-closed still applies when the malformed value came from the binary:
	// an explicitly stamped-but-broken build must not serve an identity.
	s := &server{
		softwareVersion:      "0.10.0",
		sourceRevision:       "not-a-sha",
		sourceRevisionOrigin: release.OriginBinary,
	}
	rr := httptest.NewRecorder()
	s.handleCapabilities(rr, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	if rr.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 for a malformed embedded revision, got %d", rr.Code)
	}
	if !strings.Contains(rr.Body.String(), "scorer release identity unavailable") {
		t.Fatalf("the established 503 body must not change: %s", rr.Body.String())
	}
}

// The subnet reads source_revision_origin to tell a proven revision from an
// asserted one.
func TestCapabilitiesReportProvenanceOrigin(t *testing.T) {
	binary := capabilitiesOf(t, &server{
		softwareVersion:       "0.10.0",
		sourceRevision:        testSourceRevision,
		sourceRevisionOrigin:  release.OriginBinary,
		softwareVersionOrigin: release.OriginBinary,
	})
	if binary.SourceRevisionOrigin != release.OriginBinary {
		t.Fatalf("source_revision_origin = %q, want binary", binary.SourceRevisionOrigin)
	}
	if binary.SoftwareVersionOrigin != release.OriginBinary {
		t.Fatalf("software_version_origin = %q, want binary", binary.SoftwareVersionOrigin)
	}
	if binary.SourceRevisionMismatch {
		t.Fatal("an agreeing deployment must not be flagged")
	}

	asserted := capabilitiesOf(t, &server{
		softwareVersion:       "0.10.0",
		sourceRevision:        testSourceRevision,
		sourceRevisionOrigin:  release.OriginEnv,
		softwareVersionOrigin: release.OriginEnv,
	})
	if asserted.SourceRevisionOrigin != release.OriginEnv {
		t.Fatalf("source_revision_origin = %q, want env", asserted.SourceRevisionOrigin)
	}
	if asserted.SourceRevision != testSourceRevision {
		t.Fatalf("the env fallback must still report a revision, got %q", asserted.SourceRevision)
	}
}

// The incident, as a validator sees it: the scorer keeps serving (it must not
// refuse to start or 503), reports the revision it was actually built from, and
// marks itself so the subnet can degrade it.
func TestCapabilitiesFlagMismatchWithoutRefusingService(t *testing.T) {
	got := capabilitiesOf(t, &server{
		softwareVersion:        "0.10.0",
		sourceRevision:         testSourceRevision,
		sourceRevisionOrigin:   release.OriginBinary,
		sourceRevisionMismatch: true,
	})
	if !got.SourceRevisionMismatch {
		t.Fatal("a disagreeing deployment must be flagged in capabilities")
	}
	if got.SourceRevision != testSourceRevision || got.SourceRevisionOrigin != release.OriginBinary {
		t.Fatalf("the binary-derived revision must still win: %+v", got)
	}
	if len(got.SupportedBenchVersions) == 0 {
		t.Fatal("a flagged scorer still negotiates normally")
	}
}

// Older ditto-subnet releases parse this response. Every pre-existing key must
// keep its name, type, and meaning; the new keys are purely additive.
func TestCapabilitiesRemainsBackwardCompatible(t *testing.T) {
	s := &server{
		softwareVersion:        "0.10.0",
		sourceRevision:         testSourceRevision,
		sourceRevisionOrigin:   release.OriginBinary,
		sourceRevisionMismatch: true,
	}
	rr := httptest.NewRecorder()
	s.handleCapabilities(rr, httptest.NewRequest(http.MethodGet, "/v1/capabilities", nil))
	var raw map[string]any
	if err := json.Unmarshal(rr.Body.Bytes(), &raw); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{
		"software_version", "source_revision", "supported_bench_versions",
		"full_run_capacity", "memory_phase_capacity", "v7_calibration",
	} {
		if _, ok := raw[key]; !ok {
			t.Fatalf("capabilities dropped the established key %q: %s", key, rr.Body.String())
		}
	}
	if raw["source_revision"] != testSourceRevision {
		t.Fatalf("source_revision must stay a bare string revision: %v", raw["source_revision"])
	}
	// New keys, and only these.
	added := map[string]bool{
		"source_revision_origin": true, "source_revision_mismatch": true,
		"software_version_origin": true, "v8_readiness": true,
	}
	known := map[string]bool{
		"software_version": true, "source_revision": true, "supported_bench_versions": true,
		"full_run_capacity": true, "memory_phase_capacity": true, "v7_calibration": true,
	}
	for key := range raw {
		if !known[key] && !added[key] {
			t.Fatalf("unexpected capability key %q — consumers parse this response", key)
		}
	}
	// source_revision_mismatch is always present so a consumer never has to
	// guess whether false means "fine" or "old scorer".
	if _, ok := raw["source_revision_mismatch"]; !ok {
		t.Fatal("source_revision_mismatch must always be emitted")
	}
}
