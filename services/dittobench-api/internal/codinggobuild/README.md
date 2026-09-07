# Fixed Go candidate compiler and launch transport

This package builds candidate-authorized implementation files and an independently
bound public API adapter. Private test files, private helpers, expected values and
report counts are not compiler inputs. `_test.go` files are rejected explicitly.

The compiler runs as the candidate UID with an empty supplementary group list,
fixed Go 1.26.6 tools, cgo disabled, internal linking, read-only module operation,
no build VCS stamping, no user Go environment, local-only toolchain selection and
disabled module/VCS network access. It never runs `go test` or `go generate`.
Compiler groups are killed/reaped before export data and executable output are
accepted. Private partial build directories are retained, not silently reused.

The parent verifies its existing bounded executor capabilities. Compilation uses
private writable output/cache directories but read-only implementation and adapter
files. Standard-library and candidate export archives are read through an explicit
completed-build importer; the oracle never invokes a package loader command.

The executable is copied into a root-owned executable memory file and sealed
against writing, shrinking, growing and seal changes. Launch rechecks the digest,
mode, ownership and seals. The C bootstrap accepts an anonymous descriptor only
with those mandatory seals. `/tmp` remains mounted `noexec`; no executable scratch
mount or ordinary-exec fallback is introduced. Explicit `MFD_EXEC` support and the
host's memory-file policy must be qualified; refusal is not bypassed. See the
[kernel's executable memory-file policy](https://docs.kernel.org/userspace-api/mfd_noexec.html).

The reviewed one-time pre-exec handoff installs confinement before candidate
initialization. Only the candidate API reply pipe survives in addition to stdio;
ordinary stdout/stderr are discarded, including initialization output. Later exec,
process creation and filter replacement remain denied. The parent owns deadlines
and verifies candidate termination.

The initial wire supports scalar values, slices, opaque error references and fixed
time inputs needed by the staged Go corpus. Slice argument snapshots preserve
observable in-place effects. General alias graphs, arbitrary native objects and
additional compiler/dependency profiles require separate validation; do not claim
universal `go test` compatibility.

The public container probe covers actual compilation, absence of initialization
and generation hooks during build, sealed-file integrity, pre-exec confinement,
integer precision, slice effects, errors, time zones, typed nil and parent-owned
assertions. It also verifies that the non-root assembler cannot read the protected
grader directory. No private input is used in image construction.

An operator-only private probe can mount a protected corpus read-only at runtime.
Its results and commitments belong in the private work context. They are diagnostic
controls, not catalog registration, production image approval or reward activation.
The fixture image is not a selectable production supervisor/grader image.
