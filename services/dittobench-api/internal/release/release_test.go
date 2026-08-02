package release

import "testing"

const (
	binarySHA = "001d3aa9b1c2d3e4f50617283940a1b2c3d4e5f6"
	envSHA    = "b45a2d07b2e421fec23859657bd63818c6dcbdf1"
)

// stamp replaces the link-time values for one test and restores them after.
func stamp(t *testing.T, revision, version string) {
	t.Helper()
	priorRevision, priorVersion := sourceRevision, softwareVersion
	sourceRevision, softwareVersion = revision, version
	t.Cleanup(func() { sourceRevision, softwareVersion = priorRevision, priorVersion })
}

func env(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

// The whole point of the change: what is compiled in beats what the environment
// claims, because only the former can be stale-proof.
func TestEmbeddedRevisionWinsOverEnvironment(t *testing.T) {
	stamp(t, binarySHA, "1.4.0")
	id := Resolve(env(map[string]string{
		EnvSourceRevision:  envSHA,
		EnvSoftwareVersion: "source-build",
	}))
	if id.SourceRevision != binarySHA {
		t.Fatalf("source revision = %q, want the embedded %q", id.SourceRevision, binarySHA)
	}
	if id.SourceRevisionOrigin != OriginBinary {
		t.Fatalf("origin = %q, want %q", id.SourceRevisionOrigin, OriginBinary)
	}
	if id.SoftwareVersion != "1.4.0" || id.SoftwareVersionOrigin != OriginBinary {
		t.Fatalf("software version = %q/%q, want 1.4.0/binary", id.SoftwareVersion, id.SoftwareVersionOrigin)
	}
	if id.EnvSourceRevision != envSHA {
		t.Fatalf("the asserted value must still be reported, got %q", id.EnvSourceRevision)
	}
}

// An image built before this change embeds nothing, so the operator-asserted
// value must keep it running — flagged as asserted, not derived.
func TestEnvironmentFallbackWhenNothingEmbedded(t *testing.T) {
	stamp(t, "", "")
	id := Resolve(env(map[string]string{
		EnvSourceRevision:  envSHA,
		EnvSoftwareVersion: "0.10.0",
	}))
	// A host `go build` inside this repo stamps vcs.revision, which legitimately
	// outranks the environment; assert the fallback only when nothing was
	// embedded at all.
	if id.EmbeddedSourceRevision == "" {
		if id.SourceRevision != envSHA || id.SourceRevisionOrigin != OriginEnv {
			t.Fatalf("expected env fallback, got %q from %q", id.SourceRevision, id.SourceRevisionOrigin)
		}
		if id.SourceRevisionMismatch {
			t.Fatal("a lone asserted value cannot be a mismatch")
		}
	}
	if id.SoftwareVersion != "0.10.0" || id.SoftwareVersionOrigin != OriginEnv {
		t.Fatalf("software version = %q/%q, want 0.10.0/env", id.SoftwareVersion, id.SoftwareVersionOrigin)
	}
}

// The incident itself: a recreated container applies a new env var over a cached
// image. Both values exist and disagree; the binary wins and the deployment is
// marked untrustworthy.
func TestMismatchIsFlaggedAndBinaryStillWins(t *testing.T) {
	stamp(t, binarySHA, "1.4.0")
	id := Resolve(env(map[string]string{
		EnvSourceRevision:  envSHA,
		EnvSoftwareVersion: "1.3.0",
	}))
	if !id.SourceRevisionMismatch {
		t.Fatal("disagreeing embedded and asserted revisions must be flagged")
	}
	if id.SourceRevision != binarySHA || id.SourceRevisionOrigin != OriginBinary {
		t.Fatalf("mismatch must not change the winner: %q from %q", id.SourceRevision, id.SourceRevisionOrigin)
	}
	if !id.SoftwareVersionMismatch {
		t.Fatal("disagreeing software versions must be flagged too")
	}
}

func TestAgreementIsNotAMismatch(t *testing.T) {
	stamp(t, binarySHA, "1.4.0")
	// Case differences name the same commit; only a different commit matters.
	id := Resolve(env(map[string]string{
		EnvSourceRevision:  "001D3AA9B1C2D3E4F50617283940A1B2C3D4E5F6",
		EnvSoftwareVersion: "1.4.0",
	}))
	if id.SourceRevisionMismatch || id.SoftwareVersionMismatch {
		t.Fatalf("identical identities must not be flagged: %+v", id)
	}
}

// The Dockerfile's default build argument means "no identity supplied". Treating
// it as a real value would make every unstamped image report a false mismatch.
func TestUnknownSentinelCountsAsUnset(t *testing.T) {
	stamp(t, "unknown", "unknown")
	id := Resolve(env(map[string]string{
		EnvSourceRevision:  envSHA,
		EnvSoftwareVersion: " 0.10.0 ",
	}))
	if id.EmbeddedSourceRevision != "" && id.EmbeddedSourceRevision != buildInfoRevision() {
		t.Fatalf("the sentinel must not be treated as an embedded revision: %q", id.EmbeddedSourceRevision)
	}
	if id.SourceRevisionMismatch {
		t.Fatal("the sentinel must not manufacture a mismatch")
	}
	if id.EnvSoftwareVersion != "0.10.0" {
		t.Fatalf("environment values must be trimmed, got %q", id.EnvSoftwareVersion)
	}
}

// Fail closed: an explicitly stamped-but-malformed build must not silently
// borrow the environment's identity. The caller's canonical-SHA check then
// rejects it.
func TestMalformedEmbeddedRevisionStillWins(t *testing.T) {
	stamp(t, "not-a-sha", "")
	id := Resolve(env(map[string]string{EnvSourceRevision: envSHA}))
	if id.SourceRevision != "not-a-sha" || id.SourceRevisionOrigin != OriginBinary {
		t.Fatalf("embedded value must win even when malformed: %q from %q",
			id.SourceRevision, id.SourceRevisionOrigin)
	}
}

func TestNoIdentityAtAll(t *testing.T) {
	stamp(t, "", "")
	id := Resolve(nil)
	if id.SoftwareVersion != "" || id.SoftwareVersionOrigin != OriginNone {
		t.Fatalf("expected no software version, got %q from %q", id.SoftwareVersion, id.SoftwareVersionOrigin)
	}
	if id.EmbeddedSourceRevision == "" && id.SourceRevisionOrigin != OriginNone {
		t.Fatalf("expected no revision, got %q from %q", id.SourceRevision, id.SourceRevisionOrigin)
	}
}
