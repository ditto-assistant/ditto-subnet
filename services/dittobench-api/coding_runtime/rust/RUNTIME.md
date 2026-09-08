# Linux Rust compiler and process ownership

These adapters run only inside an exclusively leased Linux-amd64 private executor
container. They do not construct a host sandbox, authenticate a task or image,
mount filesystems, install a production runtime, or activate a catalog.

## Compilation and artifact authority

`compiler::compile` consumes opaque `StagedInputs` and independently supplied
image/compiler/bridge-library digests. It checks root identity, bounded parent
capabilities, no-new-privileges, a single-threaded parent, root-owned protected
tool paths, and exact compiler/library bytes. The qualified immutable image must
bind the remaining compiler dependencies, standard library and `/usr/bin/cc`.
Supplying a hash is not proof of image authentication.

The fixed recipe runs twice with no ambient environment beyond its fixed PATH,
no supplementary groups, UID/GID 10001, private umask, descriptor-held cwd, closed
unrelated FDs, and CPU/address-space/file-size/descriptor limits. Output must be
an empty, exclusively locked 0700 UID/GID 10001 `/out` tmpfs, noexec/nosuid/nodev,
at most 128 MiB. No Cargo, test harness, build script, user flags, dependency
download, or external proc-macro command is selected. Compiler diagnostics and
stdout are discarded rather than retained as unbounded or public output.

Both invocations share one absolute deadline (maximum 120 seconds). The parent
is a subreaper and owns a separate process group for each invocation. It observes
exit with `waitid(WNOWAIT)`, preserving the leader PID until the final group kill,
then reaps the group and checks that it no longer exists. Cleanup failure takes
precedence over compiler success, compile errors, and timeout. Do not integrate
this into a parent whose other code concurrently reaps these children.

After all compiler writers stop, output is opened relative to the held directory
FD with NOFOLLOW/NONBLOCK. `artifact` requires a bounded single-link regular file
owned by the compiler UID, validates Linux-amd64 ELF, copies through held FDs,
rechecks source identity/metadata, seals a root-owned 0555 executable memory file
against writes/resizes/seal changes, and hashes it back. It requires explicit
`MFD_EXEC` support; there is no scratch-exec or ordinary-file fallback. Imports
recheck root ownership, anonymous status, mandatory seals and exact digest.

`Build` binds image, compiler, bridge library, staged inputs, fixed recipe, and
sealed executable digests. It is an internal completed-build commitment, not a
signed freeze authority or externally verifiable execution receipt. Partial
outputs remain private and a populated `/out` cannot be silently reused.

## Candidate execution

`ProcessFactory` validates the approved bridge schema and owns a sealed artifact.
It supplies one fresh `CandidateApi` per test, without receiving a test name,
index, source, expected value, program digest, or expected count. It opens the
fixed protected C bootstrap through a held FD, verifies the parent capability
profile, and closes unrelated inherited descriptors (including non-CLOEXEC FDs).
Only stdio and the two explicit bootstrap descriptors survive initial exec;
ordinary stdout/stderr go to `/dev/null` and stdin is the private API socket.

The native seccomp handoff verifies the bootstrap PID, binary FD, UID/GID and
single transferred listener. The one CONTINUE is granted only to its initial
`execveat` after the existing C filter is installed. No target memory is read,
no target syscall is emulated, and no candidate notification is serviced. Closing
the final listener denies later exec; the filter already denies new processes,
process-group escape and filter replacement, while permitting native threads.
The listener has one reader and is polled before receipt. The
[Linux notification API](https://www.man7.org/linux/man-pages/man2/seccomp_unotify.2.html)
specifies that receipt after readable readiness returns a notification or an
interrupt error if the target disappears; this adapter treats that error as
failed startup and cleans up rather than retrying an unbounded receive.

Startup and execution each have a nonzero maximum 30-second deadline. Calls share
the execution deadline and the bounded wire's session/challenge/sequence checks.
A panic reply is a candidate failure; malformed/closed/timed-out channels are
transport failure. Every failure poisons that session. `finish` closes the
channel, kills the owned child and waits for reap within five seconds. A child
exit is never itself an assertion result. Cleanup failure prevents the evaluator
from returning a report. Finish is idempotent; calls after finish are rejected;
dropping the adapter also attempts cleanup but cannot manufacture a receipt.

## Mandatory enclosing supervisor boundary

The caller must authenticate freeze manifests and approved API/image identities,
quiesce source writers, protect private inputs, provide readonly tools/inputs and
bounded scratch mounts, deny network, enforce PID/memory/CPU cgroups, retain an
exclusive attempt/container lease, and verify whole-container teardown. Compiler
process-group cleanup is not cgroup or host isolation; only fixed trusted tools
may run before the candidate bootstrap. A compiler compromise, parent crash,
uninterruptible I/O, or cleanup failure requires container teardown/quarantine.
File hashing, entropy, parsing, scheduling and syscall startup still require an
outer wall-clock guard. Library timeouts do not bound arbitrary filesystem I/O.

Supervisor integration must bind approved schema and generated bridge to the
same build/program authority, preserve failure classification, and sign/seal the
complete private execution receipt only after all cleanup checks pass. No private
oracle bytes may be included in image builds or candidate binaries. These
integration is implemented by the optional [supervisor driver](DRIVER.md);
native qualification and operational approval remain required before production use.

## Public controls

The existing isolated Rust fixture now uses the native compiler adapter. It
checks tool digest mismatch, frozen-input independence, populated-output reuse
rejection, constructor confinement, typed bridge compatibility, fresh per-test
state, panic classification, hanging-call timeout, non-CLOEXEC FD isolation,
idempotent finish, drop cleanup, and absence of unreaped children. Unit tests
cover sealing, identity/ELF/size failures, immutable bytes, capability/deadline
validation and trusted-process cleanup. These are public synthetic controls,
not private corpus performance, approved native-host evidence or a shadow canary.
