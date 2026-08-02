package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The published ENTRYPOINT already carries `-port 8000`, so `docker run <image>
// version` reaches flag.Args() as a trailing argument. An overridden entrypoint
// puts it first. Both must work, and nothing else may be mistaken for it.
func TestVersionCommandRecognizedFromResidualArgs(t *testing.T) {
	for _, tc := range []struct {
		name     string
		args     []string
		want     bool
		wantJSON bool
	}{
		{name: "docker run image version", args: []string{"version"}, want: true},
		{name: "json output", args: []string{"version", "-json"}, want: true, wantJSON: true},
		{name: "json double dash", args: []string{"version", "--json"}, want: true, wantJSON: true},
		{name: "no args", args: nil},
		{name: "unrelated positional", args: []string{"serve"}},
		{name: "version not first", args: []string{"serve", "version"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := versionCommandRequested(tc.args); got != tc.want {
				t.Fatalf("versionCommandRequested(%v) = %v, want %v", tc.args, got, tc.want)
			}
			if got := versionJSONRequested(tc.args); got != tc.wantJSON {
				t.Fatalf("versionJSONRequested(%v) = %v, want %v", tc.args, got, tc.wantJSON)
			}
		})
	}
}

func TestVersionCommandReportsBinaryProvenance(t *testing.T) {
	identity := release.Identity{
		SoftwareVersion:         "1.4.0",
		SoftwareVersionOrigin:   release.OriginBinary,
		SourceRevision:          testSourceRevision,
		SourceRevisionOrigin:    release.OriginBinary,
		EmbeddedSourceRevision:  testSourceRevision,
		EnvSourceRevision:       "b45a2d07b2e421fec23859657bd63818c6dcbdf1",
		SourceRevisionMismatch:  true,
		EmbeddedSoftwareVersion: "1.4.0",
	}
	var buf bytes.Buffer
	if err := writeVersion(&buf, identity, false); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	for _, want := range []string{
		testSourceRevision,
		"b45a2d07b2e421fec23859657bd63818c6dcbdf1",
		"source_revision_mismatch:  true",
		"1.4.0",
		"v7_manifest_sha256",
		"v8_manifest_sha256",
		"WARNING",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("version output missing %q:\n%s", want, out)
		}
	}
	// An operator must be able to read v7 readiness off an unstarted container.
	readiness := efficiency.V7CalibrationReadiness()
	if readiness.ManifestSHA256 != "" && !strings.Contains(out, readiness.ManifestSHA256) {
		t.Fatalf("version output must carry the v7 calibration manifest digest:\n%s", out)
	}
}

func TestVersionCommandJSONMatchesAdvertisedCapabilities(t *testing.T) {
	identity := release.Identity{
		SoftwareVersion:        "1.4.0",
		SoftwareVersionOrigin:  release.OriginEnv,
		SourceRevision:         testSourceRevision,
		SourceRevisionOrigin:   release.OriginEnv,
		EnvSourceRevision:      testSourceRevision,
		EnvSoftwareVersion:     "1.4.0",
		EmbeddedSourceRevision: "",
	}
	var buf bytes.Buffer
	if err := writeVersion(&buf, identity, true); err != nil {
		t.Fatal(err)
	}
	var got versionReport
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatalf("version -json must emit parseable JSON: %v\n%s", err, buf.String())
	}
	if got.SourceRevision != testSourceRevision || got.SourceRevisionOrigin != release.OriginEnv {
		t.Fatalf("wrong provenance: %+v", got)
	}
	if got.SourceRevisionBinary != "" {
		t.Fatalf("nothing was embedded, got %q", got.SourceRevisionBinary)
	}
	if !got.SourceRevisionCanonical {
		t.Fatal("a canonical 40-hex revision must report as canonical")
	}
	// The version command and /v1/capabilities must never disagree about what
	// this build can administer — that is the whole point of asking a container.
	s := &server{softwareVersion: "1.4.0", sourceRevision: testSourceRevision}
	live := capabilitiesOf(t, s)
	if len(got.SupportedBenchVersions) != len(live.SupportedBenchVersions) {
		t.Fatalf("version %v disagrees with capabilities %v",
			got.SupportedBenchVersions, live.SupportedBenchVersions)
	}
	for i, version := range live.SupportedBenchVersions {
		if got.SupportedBenchVersions[i] != version {
			t.Fatalf("version %v disagrees with capabilities %v",
				got.SupportedBenchVersions, live.SupportedBenchVersions)
		}
	}
	if got.V7ManifestSHA256 != live.V7Calibration.ManifestSHA256 {
		t.Fatalf("v7 manifest digest disagrees: %q vs %q",
			got.V7ManifestSHA256, live.V7Calibration.ManifestSHA256)
	}
	if got.V8ManifestSHA256 != live.V8Readiness.ManifestSHA256 {
		t.Fatalf("v8 manifest digest disagrees: %q vs %q", got.V8ManifestSHA256, live.V8Readiness.ManifestSHA256)
	}
	wantAdvertised := false
	for _, version := range live.SupportedBenchVersions {
		if version == protocol.BenchVersionV7 {
			wantAdvertised = true
		}
	}
	if got.V7Advertised != wantAdvertised {
		t.Fatalf("v7_advertised = %v, want %v", got.V7Advertised, wantAdvertised)
	}
	wantV8Advertised := false
	for _, version := range live.SupportedBenchVersions {
		wantV8Advertised = wantV8Advertised || version == protocol.BenchVersionV8
	}
	if got.V8Advertised != wantV8Advertised {
		t.Fatalf("v8_advertised = %v, want %v", got.V8Advertised, wantV8Advertised)
	}
}

// A malformed identity must be visible as malformed rather than merely absent:
// this is what a 503 from /v1/capabilities means, explained offline.
func TestVersionCommandMarksNonCanonicalRevision(t *testing.T) {
	var buf bytes.Buffer
	if err := writeVersion(&buf, release.Identity{
		SourceRevision:         "not-a-sha",
		SourceRevisionOrigin:   release.OriginBinary,
		EmbeddedSourceRevision: "not-a-sha",
	}, true); err != nil {
		t.Fatal(err)
	}
	var got versionReport
	if err := json.Unmarshal(buf.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.SourceRevisionCanonical {
		t.Fatal("a malformed revision must not report as canonical")
	}
}

func TestVersionCommandRendersMissingIdentity(t *testing.T) {
	var buf bytes.Buffer
	if err := writeVersion(&buf, release.Identity{
		SourceRevisionOrigin:  release.OriginNone,
		SoftwareVersionOrigin: release.OriginNone,
	}, false); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "(none)") {
		t.Fatalf("an unstamped binary must say so plainly:\n%s", buf.String())
	}
	if strings.Contains(buf.String(), "WARNING") {
		t.Fatalf("a lone missing identity is not a mismatch:\n%s", buf.String())
	}
}

// logReleaseIdentity runs before the server exists; make sure no formatting path
// panics on the identities it will actually see.
func TestLogReleaseIdentityHandlesEveryOrigin(t *testing.T) {
	for _, identity := range []release.Identity{
		{},
		{SourceRevision: testSourceRevision, SourceRevisionOrigin: release.OriginEnv, EnvSourceRevision: testSourceRevision},
		{
			SourceRevision: testSourceRevision, SourceRevisionOrigin: release.OriginBinary,
			EmbeddedSourceRevision: testSourceRevision, EnvSourceRevision: "b45a2d07b2e421fec23859657bd63818c6dcbdf1",
			SourceRevisionMismatch: true, SoftwareVersionMismatch: true,
		},
	} {
		logReleaseIdentity(identity)
	}
}
