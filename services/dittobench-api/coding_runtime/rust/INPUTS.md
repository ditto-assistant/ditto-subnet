# Frozen Rust compiler inputs

`workspace` is a Linux-only capture/materialization boundary. It does not execute
code, launch a compiler, authenticate a freeze, or own a compiler process. A
production adapter must supply the authenticated implementation manifest and
expected freeze-owner UID (root in the hosted execution boundary), after stopping
all source writers. Never derive the manifest by recursively copying a workspace
or taking candidate claims as authorization.

## Capture authority

- The manifest must include `src/lib.rs` and at most 256 exact implementation
  paths with SHA-256 values. Paths are bounded ASCII `src/**/*.rs` names, with
  at most eight components and no traversal, empty components, hidden names,
  Cargo/config/build inputs, or reserved test/grader/cache directories and names.
  These naming checks supplement the independent allowlist; they do not classify
  arbitrary source bytes as safe or prove that a controller chose the right role.
- The original root is private to its approved owner. Root/descendant ownership,
  permissions, and filesystem identity are checked; group/other writes and
  special mode bits are rejected. Ancestors must also be trusted and protected
  against other-user renames. Root-owned sticky temporary ancestors are allowed;
  the final source root must still be private.
- Every path component is opened relative to a held directory descriptor with
  `O_NOFOLLOW`; directories also require `O_DIRECTORY`. Regular files require one
  link and bounded size. Nonblocking file opens prevent a FIFO from hanging the
  initial open. Symlinks, hardlinks, special files, and cross-device descendants
  fail closed. Mount-namespace control remains the qualified caller's obligation.
- Bytes are read through the verified file descriptor with a 1 MiB per-file and
  16 MiB aggregate bound. Size/identity/mode/ownership/timestamps are rechecked,
  and every byte digest must match the independent manifest. Original paths are
  never reopened to select the captured content.

The resulting snapshot holds private owned bytes and a domain-separated content
commitment over ordered paths, lengths, and digests. Host path names do not change
that commitment. Later edits to the original tree cannot change the captured
bytes. Unlisted files are neither enumerated nor copied.

## Fresh compiler-visible materialization

Staging creates an exclusive OS-random child under a protected parent. It never
merges into, truncates, or reuses an existing directory. Only captured source
files and the separately generated public `bridge.rs` are written. Each file is
exclusively created, synced, and hashed back through its descriptor. Files become
0444 and directories 0555; the root is published only after preceding operations
succeed. No writable file handle escapes. Normal error returns leave incomplete
staging directories private for explicit owner cleanup, not automatic reuse.

`StagedInputs` retains the directory FD and binds source, bridge, and compiler
recipe commitments. Production should use that held directory as the compiler
cwd/mount source and retain its exclusive lease through compilation. Permissions
protect against the separate compiler UID, not against other trusted root
writers. A source/staging digest is not an image, toolchain, or execution receipt.

## Fixed compiler recipe

The recipe selects `/opt/rustc`, a PATH-only environment, edition 2021, the explicit
`x86_64-unknown-linux-gnu` target, checked overflow, and `/usr/bin/cc` as linker.
The two fixed commands compile `src/lib.rs`
as the `candidate` rlib and then compile `bridge.rs` against that artifact and the
immutable bridge dependencies. Output paths are `/out/libcandidate.rlib` and
`/out/candidate`. There are no candidate Cargo manifests, user flags, test-harness
mode, external dependency resolution, or build-script commands in the recipe.

The caller must provide a fresh bounded `/out` writable mount, readonly approved
inputs/tools, network isolation, non-root credentials with no supplementary
groups, deadlines, and compiler cgroup/process cleanup. Filesystem byte bounds
are not wall-clock I/O deadlines; the outer lifecycle guard remains mandatory.
This layer does not verify installed compiler bytes, compile success, output ELF
identity, process termination, or production admission by itself.

## Evidence and remaining work

Unit tests cover path/ownership/permission failures, links and FIFOs, byte limits,
digest mismatch, exact copying, source edits after capture, protected ancestors,
and recipe invariants. The public compiled fixture now uses these captured input
files and recipe commands, excludes a synthetic unlisted test file, and mutates
the original source before compiling to verify independence.

The separate [Linux runtime adapter](RUNTIME.md) now consumes staged inputs,
owns fixed non-root compiler groups, seals output, and owns per-test processes.
The [protected driver](DRIVER.md) connects authenticated manifest/image authority,
enclosing container policy, and private receipt binding. Private controls,
native-host qualification, and operational approval remain required. No catalog,
infrastructure, or Coding activation changes are made here.
