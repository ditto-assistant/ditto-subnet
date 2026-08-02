// Package release resolves this binary's own provenance: which source commit it
// was compiled from and which release it belongs to.
//
// Provenance must be a property of the COMPILED ARTIFACT, not of the
// environment it happens to be started in. An environment variable asserts what
// an operator BELIEVES is running; only a value linked into the binary reports
// what IS running. The two diverge whenever a container is recreated against a
// cached image: the new environment applies immediately while the old binary
// keeps executing, so the scorer advertises a revision whose code it does not
// contain. That exact divergence made a stale-image deployment look like a code
// regression during the v7 rollout.
//
// Resolution order, strongest evidence first:
//
//  1. Values linked in with -ldflags -X (see the Dockerfile). This is the
//     authoritative source. It cannot drift from the binary because it is part
//     of it.
//  2. runtime/debug build info (vcs.revision). Free and automatic, but only
//     populated when the build ran inside a git work tree with a git binary
//     present. The release image build satisfies neither (BuildKit checks out a
//     git context without .git, and the golang:1.23-alpine build stage does not
//     install git), so this only ever fires for host builds — useful for
//     developers, never load-bearing in production.
//  3. DITTOBENCH_SOURCE_SHA / DITTOBENCH_SOFTWARE_VERSION from the environment.
//     A legacy, operator-asserted fallback retained ONLY so an unstamped image
//     keeps working. It is used only when nothing at all was embedded.
//
// A value embedded in the binary always wins, even if it is malformed: an
// explicitly stamped-but-broken build must fail closed rather than quietly
// borrow an environment variable's identity.
package release

import (
	"runtime/debug"
	"strings"
)

// Stamped at link time. Do not set these anywhere else:
//
//	go build -ldflags "-X github.com/ditto-assistant/dittobench-api/internal/release.sourceRevision=<40-hex sha>"
//	go build -ldflags "-X github.com/ditto-assistant/dittobench-api/internal/release.softwareVersion=<version>"
var (
	sourceRevision  string
	softwareVersion string
)

// Environment variables holding the legacy operator-asserted identity.
const (
	EnvSourceRevision  = "DITTOBENCH_SOURCE_SHA"
	EnvSoftwareVersion = "DITTOBENCH_SOFTWARE_VERSION"
)

// unsetSentinel is the Dockerfile's default build argument. A build that did not
// deliberately supply an identity carries this value, so it means "unset" rather
// than a real revision — otherwise every unstamped image would report a bogus
// mismatch against its environment.
const unsetSentinel = "unknown"

// Origin names where a resolved identity actually came from, so a consumer can
// tell derived provenance from asserted provenance.
type Origin string

const (
	// OriginBinary means the value was compiled into this binary.
	OriginBinary Origin = "binary"
	// OriginEnv means nothing was embedded and the value was asserted by the
	// process environment.
	OriginEnv Origin = "env"
	// OriginNone means neither source supplied a value.
	OriginNone Origin = "none"
)

// Identity is one binary's resolved release provenance.
type Identity struct {
	// SourceRevision is the winning value: embedded when present, else env.
	SourceRevision string
	// SourceRevisionOrigin says which of the two produced SourceRevision.
	SourceRevisionOrigin Origin
	// EmbeddedSourceRevision and EnvSourceRevision are the raw inputs, kept so
	// operators can see both sides of a disagreement.
	EmbeddedSourceRevision string
	EnvSourceRevision      string
	// SourceRevisionMismatch is true when BOTH sources supplied a revision and
	// they disagree. The binary still wins; this only marks the deployment as
	// untrustworthy so the subnet can degrade the validator.
	SourceRevisionMismatch bool

	SoftwareVersion         string
	SoftwareVersionOrigin   Origin
	EmbeddedSoftwareVersion string
	EnvSoftwareVersion      string
	SoftwareVersionMismatch bool
}

// Resolve computes this binary's identity. getenv is injected so the resolution
// rules are testable without mutating the process environment.
func Resolve(getenv func(string) string) Identity {
	if getenv == nil {
		getenv = func(string) string { return "" }
	}
	id := Identity{
		EmbeddedSourceRevision:  embeddedSourceRevision(),
		EnvSourceRevision:       normalize(getenv(EnvSourceRevision)),
		EmbeddedSoftwareVersion: normalize(softwareVersion),
		EnvSoftwareVersion:      normalize(getenv(EnvSoftwareVersion)),
	}
	id.SourceRevision, id.SourceRevisionOrigin = pick(id.EmbeddedSourceRevision, id.EnvSourceRevision)
	id.SourceRevisionMismatch = disagree(id.EmbeddedSourceRevision, id.EnvSourceRevision)
	id.SoftwareVersion, id.SoftwareVersionOrigin = pick(id.EmbeddedSoftwareVersion, id.EnvSoftwareVersion)
	id.SoftwareVersionMismatch = disagree(id.EmbeddedSoftwareVersion, id.EnvSoftwareVersion)
	return id
}

// embeddedSourceRevision prefers the explicitly linked revision and falls back
// to whatever the toolchain stamped automatically. Both are properties of the
// binary; the explicit one is simply the only one the release build produces.
func embeddedSourceRevision() string {
	if linked := normalize(sourceRevision); linked != "" {
		return linked
	}
	return buildInfoRevision()
}

// buildInfoRevision reports the VCS revision the go toolchain recorded at build
// time, when it recorded one at all.
func buildInfoRevision() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return ""
	}
	var revision string
	for _, setting := range info.Settings {
		switch setting.Key {
		case "vcs.revision":
			revision = normalize(setting.Value)
		case "vcs.modified":
			// A dirty work tree means the binary is not the commit it names.
			// Refusing to claim that revision keeps provenance honest.
			if setting.Value == "true" {
				return ""
			}
		}
	}
	return revision
}

func pick(embedded, env string) (string, Origin) {
	switch {
	case embedded != "":
		return embedded, OriginBinary
	case env != "":
		return env, OriginEnv
	default:
		return "", OriginNone
	}
}

func disagree(embedded, env string) bool {
	return embedded != "" && env != "" && !strings.EqualFold(embedded, env)
}

func normalize(value string) string {
	value = strings.TrimSpace(value)
	if strings.EqualFold(value, unsetSentinel) {
		return ""
	}
	return value
}
