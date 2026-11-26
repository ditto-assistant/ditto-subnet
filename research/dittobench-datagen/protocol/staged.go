package protocol

import (
	"strconv"
	"strings"
	"time"
)

// Staged benchmark contracts.
//
// A staged version is a contract this module can already GENERATE (its own
// dataset epoch and rotate seed, a pinned known vector) but does not yet
// SUPPORT. It is deliberately outside supportedBenchVersions, so
// SupportedBenchVersion, NewestSupportedBenchVersion, SupportedBenchVersionList
// and every consumer that negotiates versions from them (the scorer's
// advertised window, efficiency readiness, the probe defaults) still stop at
// the shared ceiling in ditto/tests/contract/bench_versions.json. The datagen
// half of a contract can therefore land, be reviewed and be vector-pinned
// before the cross-layer change that raises that ceiling.
//
// Promotion is one move, made by the cross-layer PR that raises the shared
// ceiling: the constant and epoch below move into epoch.go's const and var
// blocks, the version joins supportedBenchVersions, and it leaves
// stagedBenchVersions. Generation bytes do not change on promotion, because
// RotateSeedForVersion and DatasetEpochForVersion already serve staged
// versions with the same epoch and the same mixing function.

// BenchVersionV19 is the GENERALIZATION contract (DITTO-2736): the v18 run with
// its public datagen memory half hardened against template matching. It adds
// shortcut-breaker families whose answers a rule keyed to the public
// generator's own habits gets wrong, renders question wording from a seeded
// phrase grammar instead of fixed lists, and moves composite weight from the
// public datagen half to the held-out generator columns
// (docs/bench-versions.md, "Bench v19"). Every lever is gated on
// bench_version >= 19, so v2..v18 regenerate and re-score byte-identically.
// Staged until the cross-layer PR supports it.
const BenchVersionV19 = 19

var datasetEpochV19 = time.Date(2027, 11, 1, 0, 0, 0, 0, time.UTC)

// stagedBenchVersions are the generatable-but-unsupported contracts, in order.
// Each is exactly one past the previous entry (or past the supported ceiling).
// v16 (the dueling contract) and v18 (the accuracy contract) were staged here
// and promoted into epoch.go by the cross-layer PRs that raised the shared
// ceiling.
var stagedBenchVersions = []int{BenchVersionV19}

// StagedBenchVersions returns a copy of the staged versions.
func StagedBenchVersions() []int {
	return append([]int(nil), stagedBenchVersions...)
}

// StagedBenchVersion reports whether version is staged (generatable, not yet
// supported).
func StagedBenchVersion(version int) bool {
	for _, v := range stagedBenchVersions {
		if v == version {
			return true
		}
	}
	return false
}

// GeneratableBenchVersion reports whether this module can generate a version:
// every supported version plus the staged ones. Offline research tools accept
// it so a staged contract can be audited; anything that negotiates or scores
// versions must keep using SupportedBenchVersion.
func GeneratableBenchVersion(version int) bool {
	return SupportedBenchVersion(version) || StagedBenchVersion(version)
}

// NewestGeneratableBenchVersion is the highest version this module can
// generate (the newest staged version when one exists).
func NewestGeneratableBenchVersion() int {
	if len(stagedBenchVersions) > 0 {
		return stagedBenchVersions[len(stagedBenchVersions)-1]
	}
	return NewestSupportedBenchVersion()
}

// stagedDatasetEpoch returns a staged contract's epoch. Add a case per staged
// version; promotion moves the case into DatasetEpochForVersion.
func stagedDatasetEpoch(version int) (time.Time, bool) {
	switch version {
	case BenchVersionV19:
		return datasetEpochV19, true
	default:
		return time.Time{}, false
	}
}

// versionListForErrors renders the supported list, plus the staged list when
// there is one, so an unsupported-version error names everything generatable.
func versionListForErrors() string {
	list := SupportedBenchVersionList()
	if len(stagedBenchVersions) == 0 {
		return list
	}
	staged := make([]string, 0, len(stagedBenchVersions))
	for _, v := range stagedBenchVersions {
		staged = append(staged, strconv.Itoa(v))
	}
	return list + "; staged: " + strings.Join(staged, ", ")
}
