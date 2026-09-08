# Protected Rust supervisor driver

The optional `driver` feature connects the Rust runtime to the existing trusted
Go supervisor and Platform-private grading receipt chain. It is not an image
approval, private catalog release, deployment, canary, or reward activation.

## Authority path

The independently approved grader command is exactly:

```text
dittobench-test-driver --group hidden --authority rust/hidden.json --authority-sha256 <sha256>
```

`visible` is the other supported group. The command and expected count are part
of the authenticated grading plan. The protected grader bundle supplies the
authority document; its exact hash must match the command before any source
manifest is prepared. The document contains schema
`dittobench-coding-rust-authority-v1`, group, crate name, suite-relative path and
SHA-256, approved implementation paths, explicit API signatures, and bounded
compiler/per-test timeouts. It contains no compiler flags or executable paths.

An API signature is `{name, parameters, result}`. Each type is a closed object
with a `kind` tag: `bool`, `u8/u16/u32/u64/usize`, `i8/i16/i32/i64/isize`, `text`,
`char`, `vec`, `slice`, `array`, `tuple`, `option`, `result`, or `ref`. Containers
use `item`, arrays add `length`, tuples use `items`, and results use `ok`/`error`.
The same bridge validation enforces the supported borrow/ABI subset. Unknown
extensions are ignored; known fields, duplicate fields, paths, shapes and bounds
remain strict. The full document bytes remain hash-bound, including extensions.

After authoritative pristine replay, the host executor reads only independently
approved `src/**/*.rs` files. It opens each path component relative to a held FD,
rejects symlinks/hardlinks/special files and unsafe writes, bounds file/aggregate
bytes, and checks metadata before/after hashing. It writes an exclusive private
`rust-inputs.json` control file containing schema
`dittobench-coding-rust-inputs-v1`, authority hash, approved image hash, and a
list of `{path, sha256}` entries. The driver independently verifies that file's
hash and exact allowlist, then uses `Snapshot::capture` for a second descriptor-
relative content verification before staging. No candidate-provided digest or
recursive source discovery is used as implementation authority.

The Go supervisor injects the control-manifest hash/path, nonce, expected count,
fixed report path and candidate UID/GID. The driver accepts only UID/GID 10001,
one of the two groups, 1–32 tests and the fixed control paths. Private hidden
suite bytes come only from `/run/dittobench-grader`; visible suites come from the
frozen `/workspace`. Both roots must be root-owned and private. Inputs and private
suite hashes are verified before compilation; unsupported oracle syntax, wrong
counts, invalid authority or changed freeze inputs produce no successful report.

## Runtime image and isolation

`Dockerfile.coding-rust` uses pinned Rust 1.98.0/Go 1.26.6 Linux-amd64 toolchains.
It installs the fixed C bootstrap, trusted Go supervisor, Rust driver, core-only
bridge libraries, and an immutable toolchain hash manifest. The candidate core
is built without the `driver` feature before the separate JSON-enabled driver
build. No Serde derive shared library or JSON-driver dependency is needed during
candidate compilation. The runtime target contains no public probe or private
suite. The separate test target is explicitly labelled as a fixture.

An approved image's `rust-call-ast-v1` label selects the fixed Rust scratch
layout only after exact digest/platform verification. The executor requires at
least 1 GiB RAM, 256 MiB scratch, 64 PIDs, fixed candidate IDs and bounded test
counts. `/out` receives at most 128 MiB of the existing signed scratch budget;
the remainder stays at `/tmp`. This does not increase the signed aggregate disk
allowance. `/out` is noexec/nosuid/nodev, mode 0700, UID/GID 10001. The container
inspection checks the exact extra mount/options; other profiles retain their
single scratch mount. Network denial, rootless isolated daemon policy, immutable
image, cgroups, readonly workspace/protected mounts and whole-container cleanup
remain the enclosing executor's responsibility and are not relaxed.

## Reports and sealed evidence

After normal compiler failure with verified cleanup, the driver emits a completed
zero-pass report. After evaluation it emits parent-owned counts only after each
candidate was terminated and reaped. Setup, transport, changed inputs, unsupported
private syntax, cleanup failure and unknown compiler signals fail closed without
an authoritative completed report. The existing outer watchdog still applies to
the entire command; per-test/compiler limits do not extend it.

The report is exclusively created at the protected fixed path, mode 0600, and
synced. Schema `dittobench-coding-trusted-test-report-v2` binds the supervisor
nonce/count and a `dittobench-coding-rust-runtime-v1` evidence object containing
authority, inputs, image, private-program, compiler and bridge-library digests.
Successful compilation also includes completed-build and artifact digests.
`compile_failed` may not claim those artifact fields or any passed test.

The supervisor rejects a completed driver exit other than 0/1 even if a valid-
looking report exists, and checks the report's runtime commitments against its
own freeze/image authority. A Rust driver-container OOM cannot invent a phase
report. The evidence is copied into `TestRun`, the canonical execution receipt
and its chained hash, and the existing hosted terminal serialization. The
Platform-private sealed-evidence path therefore retains it; candidate stdout,
compiler diagnostics and private source never become completion authority.
Legacy drivers continue to use the v1 report without runtime fields.

## Qualification boundary

Public tests cover native supervisor execution, source/type failures, private-read
denial, fresh state, panic/hang/forgery, mutated authority/inputs, report symlinks,
thread cleanup, report-to-freeze/image binding and receipt hash sensitivity.
They do not qualify private base/reference performance or approve an operational
runtime. The final planned PR still owns private four-language controls, native
host/image/key/recovery qualification tooling and shadow-canary readiness. Actual
provisioning, activation and canary execution require separate operator approval.
