package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/efficiency"
	"github.com/ditto-assistant/dittobench-api/internal/release"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// The version command answers "what is this container actually running?" without
// starting the server, opening a port, or touching a Docker daemon:
//
//	docker run --rm <image> version
//	docker run --rm <image> version -json
//
// The published ENTRYPOINT is ["/dittobench-api", "-port", "8000"], so those
// arguments arrive appended after the flags and land in flag.Args(). A leading
// `version` (with an overridden entrypoint) and the conventional `-version` flag
// work too.
const versionCommandName = "version"

// versionCommandRequested reports whether the residual non-flag arguments name
// the version subcommand.
func versionCommandRequested(args []string) bool {
	return len(args) > 0 && args[0] == versionCommandName
}

// versionJSONRequested reports whether the subcommand asked for machine-readable
// output. The subnet updater parses this; an operator reads the text form.
func versionJSONRequested(args []string) bool {
	if !versionCommandRequested(args) {
		return false
	}
	for _, arg := range args[1:] {
		if arg == "-json" || arg == "--json" {
			return true
		}
	}
	return false
}

// versionReport is the stable shape of `version -json`. Field names match
// /v1/capabilities wherever the two report the same fact.
type versionReport struct {
	SoftwareVersion         string         `json:"software_version"`
	SoftwareVersionOrigin   release.Origin `json:"software_version_origin"`
	SourceRevision          string         `json:"source_revision"`
	SourceRevisionOrigin    release.Origin `json:"source_revision_origin"`
	SourceRevisionBinary    string         `json:"source_revision_binary"`
	SourceRevisionEnv       string         `json:"source_revision_env"`
	SourceRevisionMismatch  bool           `json:"source_revision_mismatch"`
	SourceRevisionCanonical bool           `json:"source_revision_canonical"`
	SupportedBenchVersions  []int          `json:"supported_bench_versions"`
	V7Advertised            bool           `json:"v7_advertised"`
	V7ManifestSHA256        string         `json:"v7_manifest_sha256"`
	V8Advertised            bool           `json:"v8_advertised"`
	V8ManifestSHA256        string         `json:"v8_manifest_sha256"`
}

func newVersionReport(identity release.Identity) versionReport {
	v7 := efficiency.V7CalibrationReadiness()
	v8 := efficiency.V8Readiness()
	versions := supportedBenchVersions()
	v7Advertised, v8Advertised := false, false
	for _, version := range versions {
		if version == protocol.BenchVersionV7 {
			v7Advertised = true
		}
		if version == protocol.BenchVersionV8 {
			v8Advertised = true
		}
	}
	return versionReport{
		SoftwareVersion:         identity.SoftwareVersion,
		SoftwareVersionOrigin:   identity.SoftwareVersionOrigin,
		SourceRevision:          identity.SourceRevision,
		SourceRevisionOrigin:    identity.SourceRevisionOrigin,
		SourceRevisionBinary:    identity.EmbeddedSourceRevision,
		SourceRevisionEnv:       identity.EnvSourceRevision,
		SourceRevisionMismatch:  identity.SourceRevisionMismatch,
		SourceRevisionCanonical: canonicalSourceRevision(identity.SourceRevision),
		SupportedBenchVersions:  versions,
		V7Advertised:            v7Advertised,
		V7ManifestSHA256:        v7.ManifestSHA256,
		V8Advertised:            v8Advertised,
		V8ManifestSHA256:        v8.ManifestSHA256,
	}
}

func writeVersion(w io.Writer, identity release.Identity, asJSON bool) error {
	report := newVersionReport(identity)
	if asJSON {
		encoder := json.NewEncoder(w)
		encoder.SetIndent("", "  ")
		return encoder.Encode(report)
	}
	versions := make([]string, 0, len(report.SupportedBenchVersions))
	for _, version := range report.SupportedBenchVersions {
		versions = append(versions, fmt.Sprintf("%d", version))
	}
	lines := [][2]string{
		{"software_version", fmt.Sprintf("%s (%s)", orNone(report.SoftwareVersion), report.SoftwareVersionOrigin)},
		{"source_revision", fmt.Sprintf("%s (%s)", orNone(report.SourceRevision), report.SourceRevisionOrigin)},
		{"source_revision_binary", orNone(report.SourceRevisionBinary)},
		{"source_revision_env", orNone(report.SourceRevisionEnv)},
		{"source_revision_mismatch", fmt.Sprintf("%t", report.SourceRevisionMismatch)},
		{"source_revision_canonical", fmt.Sprintf("%t", report.SourceRevisionCanonical)},
		{"supported_bench_versions", strings.Join(versions, ",")},
		{"v7_advertised", fmt.Sprintf("%t", report.V7Advertised)},
		{"v7_manifest_sha256", orNone(report.V7ManifestSHA256)},
		{"v8_advertised", fmt.Sprintf("%t", report.V8Advertised)},
		{"v8_manifest_sha256", orNone(report.V8ManifestSHA256)},
	}
	if _, err := fmt.Fprintln(w, "dittobench-api"); err != nil {
		return err
	}
	for _, line := range lines {
		if _, err := fmt.Fprintf(w, "  %-26s %s\n", line[0]+":", line[1]); err != nil {
			return err
		}
	}
	if report.SourceRevisionMismatch {
		if _, err := fmt.Fprintln(w,
			"\nWARNING: the compiled revision and DITTOBENCH_SOURCE_SHA disagree — "+
				"this image is almost certainly stale. Rebuild/pull the image; do not "+
				"trust the asserted value."); err != nil {
			return err
		}
	}
	return nil
}

func orNone(value string) string {
	if value == "" {
		return "(none)"
	}
	return value
}

// logReleaseIdentity makes provenance visible in the first lines of a container's
// log, and shouts when the binary disagrees with what the deployment asserts —
// the failure that previously read as a code regression.
func logReleaseIdentity(identity release.Identity) {
	log.Printf("release identity: software_version=%q (%s) source_revision=%q (%s)",
		identity.SoftwareVersion, identity.SoftwareVersionOrigin,
		identity.SourceRevision, identity.SourceRevisionOrigin)
	if identity.SourceRevisionOrigin == release.OriginEnv {
		log.Printf("WARNING: no source revision compiled into this binary — falling back to the "+
			"operator-asserted %s. Provenance is claimed, not proven; build with "+
			"--build-arg DITTOBENCH_SOURCE_SHA=<sha> to fix.", release.EnvSourceRevision)
	}
	if identity.SourceRevisionMismatch {
		log.Printf("WARNING: SOURCE REVISION MISMATCH — this binary was built from %s but %s asserts %s. "+
			"The container is running a stale image; recreate it with a rebuilt/re-pulled image. "+
			"Reporting the binary-derived revision.",
			identity.EmbeddedSourceRevision, release.EnvSourceRevision, identity.EnvSourceRevision)
	}
	if identity.SoftwareVersionMismatch {
		log.Printf("WARNING: software version mismatch — binary=%s %s=%s",
			identity.EmbeddedSoftwareVersion, release.EnvSoftwareVersion, identity.EnvSoftwareVersion)
	}
}
