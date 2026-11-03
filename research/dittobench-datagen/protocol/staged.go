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

// BenchVersionV16 is the DUELING contract (docs/dueling-bench.md). The
// canonical run is a datagen half generated here (the v15 contract at the
// dueling profile, with connector tools that no longer self-identify, a
// seeded set of linked connectors that some cases need, and wording decoys)
// plus generator slice columns that Platform delivers (dueling-slice-v1).
// Every lever is gated on bench_version >= 16, so v2..v15 regenerate
// byte-identically. Staged until the cross-layer PR supports it.
const BenchVersionV16 = 16

var datasetEpochV16 = time.Date(2027, 8, 1, 0, 0, 0, 0, time.UTC)

// stagedBenchVersions are the generatable-but-unsupported contracts, in order.
// Each is exactly one past the previous entry (or past the supported ceiling).
var stagedBenchVersions = []int{BenchVersionV16}

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

func stagedDatasetEpoch(version int) (time.Time, bool) {
	switch version {
	case BenchVersionV16:
		return datasetEpochV16, true
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
