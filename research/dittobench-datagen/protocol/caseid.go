package protocol

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// OpaqueCaseID derives a case's wire id as a pure function of
// (seed, kind, ordinal). The id carries no category, question-type, or
// attribute information: the harness must hold the verbatim id (the mock tool
// endpoint keys on it), so anything encoded in it is readable at run time, and
// the previous prefixed ids handed a pattern-matching harness the answer class
// for free (routing traps, injection/abstention flags, the memory attribute).
//
// A hash id blinds only the running harness, which does not know the seed.
// Auditability is unaffected: the seed is public once scoring completes, so
// anyone can recompute every id (and the full artifact carries the id-to-case
// mapping). kind is a generator-internal namespace ("tool", "mem", "iso-a",
// "iso-b") that keeps ordinals collision-free across suites; it is hash input
// only and not recoverable from the output.
func OpaqueCaseID(seed int64, kind string, ordinal int) string {
	h := sha256.Sum256(fmt.Appendf(nil, "dittobench-case|%d|%s|%d", seed, kind, ordinal))
	return "c" + hex.EncodeToString(h[:8])
}
