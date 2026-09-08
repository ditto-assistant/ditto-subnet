# Fixed Rust API bridge

`bridge::generate` accepts only a bounded, independently approved ordered map of
crate-relative function names and data signatures. It accepts no oracle AST,
test source, expected values, private program digest, compiler path, or arbitrary
code snippet. Names are validated identifiers, and calls always resolve under
the fixed external `candidate` crate. Generated source is capped at 1 MiB and
has an exact SHA-256 for the enclosing private build receipt.

The bridge assigns every API to an explicit safe Rust function-pointer type.
An implementation cannot silently change an approved argument or return type;
the fixed compilation fails on an incompatible signature. Shared input borrows
have local owned backing storage, which survives the call and conversion of
borrowed results. The initial input profile supports owned primitives/containers
and one top-level immutable borrow; nested input borrows are rejected. Borrowed
outputs are copied into typed data descriptions. Lifetimes are not transmitted.

`native` supplies sealed conversions for the approved standard-library data
types, not extensible custom serializers. Integer widths, container kinds,
arrays, tuples through arity 16, options/results, and reference descriptions
remain distinct. Use `NativeValue::to_value(&value)` explicitly when preserving
a reference's type: ordinary Rust method autodereferencing can select a
referent's implementation instead. Generated dispatch uses this explicit form.

The generated main reads only the inherited Unix-stream API socket on FD 0.
It uses the bounded correlated wire protocol, catches ordinary panics as
candidate failure, and suppresses its panic hook. It has a fixed 30-second
session deadline and at most 128 requests. It has no private assertions, grader
file paths, test names/counts, report writer, or program fingerprint.

**Do not run a generated candidate binary on an ordinary host.** The bridge does
not install confinement: native constructors, allocators, and loader code can
run before `main`. It must be launched under the previously reviewed pre-exec
bootstrap inside an isolated executor container. Panic handling and language
type safety are not isolation mechanisms for malicious candidate code.

## Public compiled control

```sh
docker build -f services/dittobench-api/Dockerfile.coding-rust-bridge-probe \
  -t coding-rust-bridge:synthetic .
bash scripts/test-coding-rust-bridge.sh coding-rust-bridge:synthetic
```

The fixture image is explicitly non-selectable and contains public synthetic
source only. Its fixed Rust 1.98.0 compiler runs as UID/GID 10001, without
supplementary groups, in a network-disabled read-only container. Writable scratch
stays `noexec`. Candidate Cargo manifests/build scripts are not used: the fixture
invokes fixed `rustc` commands for an rlib and the approved bridge, with checked
overflow and a fixed tool path/environment. Compiler groups are killed/reaped
before the parent accepts the executable.

The parent copies that executable into a root-owned `MFD_EXEC` memory file and
requires write/grow/shrink/seal seals. There is no ordinary-exec or executable
scratch fallback. The launch helper duplicates and rechecks the FD, verifies
its digest, and passes only a same-parent Unix stream as candidate stdin. The
immutable bootstrap independently checks the executable and seals before its
one-time confined `execveat` handoff. Existing path-based callers are unchanged.

The fixture's successful build now uses the [frozen input boundary](INPUTS.md):
only manifest-listed source and the generated bridge reach the compiler. It
excludes an unlisted test file and changes the original source after capture to
verify that the compiler still consumes the committed snapshot. The compiler
program, environment, and command arguments come from the fixed recipe.

Controls verify protected grader reads are denied during compilation, a wrong
digest never enters the candidate, constructors run only after confinement,
typed API round trips preserve borrowed text, slices, owned nested values,
integer precision and domain errors, and candidate panic is not a domain error.
Termination/reaping is verified after the parent client finishes. An incompatible
native API signature must fail compilation.

## Remaining production work

This module is a bridge generator, not a supervisor driver. The separate
[runtime adapters](RUNTIME.md) now own fixed compilation, sealed artifacts,
per-test process launch, and verified cleanup. The [protected driver](DRIVER.md)
connects freeze/image authority, enclosing container policy, and private receipt
binding. Native qualification and private base/reference controls remain required. The
fixture's PID/RAM/scratch settings are not production runtime approval. No private
suite is linked into candidate binaries and no catalog or activation gate changes.
