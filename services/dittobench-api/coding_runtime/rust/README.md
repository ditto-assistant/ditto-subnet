# Protected Rust suite admission

This crate is the first Rust oracle layer: a non-executing, closed syntax
admission gate. It is **not a grader** and is not wired into the supervisor or
runtime image selection. Admitting a suite never means its tests passed.

The trusted controller supplies the independently approved crate/function names,
exact source SHA-256, and nonzero expected test count. The parser does not discover
authority from candidate files or suite imports. `syn` parses source and macro
arguments without loading modules or expanding macros. There is no filesystem,
compiler, cargo, subprocess, or candidate execution path in this library.
The opaque result also binds a domain-separated digest of the source, approved
namespace/function set, and expected count; future evaluation must preserve that
same authority rather than reusing admission with another policy.

The source and returned opaque `AdmittedSuite` must remain in the protected
Platform-side grader process. The result has no public AST accessor or
Debug/Display/serialization implementation. Parser errors expose only fixed
categories, never private names, literals, source excerpts, or spans. The caller
must enforce protected input custody, process resource limits, and private error
routing; this library is not an isolation boundary.

## Closed subset

- Explicit imports of approved functions from exactly one approved crate, or
  fully qualified calls to those same functions. Approval names exact
  crate-relative paths such as `encoding::decode`, with at most four segments;
  approving a function never approves its entire module. Nested/grouped imports
  retain their full bindings and reject leaf-name collisions. No wildcard,
  alias, absolute, generic API, or dynamically selected calls.
- Unique, private, zero-argument `#[test] fn` items with default return type and
  at least one assertion. No extra attributes, helper functions, modules,
  constants, statics, custom macros, or other items.
- Immutable untyped local bindings, without shadowing or forward references.
- `assert!` and `assert_eq!` with exact argument counts and no format arguments.
- Integer, Boolean, string, and character literals; arrays, tuples, immutable
  references, parentheses, `!`/unary `-`, `==`/`!=`/`&&`/`||`, `None`, and single
  argument `Some`/`Ok`/`Err` descriptions. Integers are bounded to `u64` magnitude
  and explicit supported suffixes. Admission is not Rust type checking.
- `MIN`/`MAX` descriptions for the supported primitive integer types only.
  Primitive and intrinsic names cannot be used as the approved crate name.
- `vec![a, b]` descriptions with recursively parsed arguments. Repetition and
  all other expression macros are rejected, including macros nested in vectors.
- Closed, zero-argument data-operation descriptions: `unwrap`, `unwrap_err`,
  `is_ok`, `is_err`, `is_some`, `is_none`, `len`, and `is_empty`. Tuple fields
  `.0` through `.15` and unsuffixed literal sequence indices below 4096 are
  admitted; named fields and computed indices are not.
- One closed `value.iter().map(|item| expression).collect::<Vec<_>>()` projection.
  The binding is immutable and non-shadowing; its body permits only admitted
  data expressions and read-only local captures. Candidate calls, nested
  projections, block bodies, general closures, custom collectors, and arbitrary
  iterator protocols are rejected. The binder never escapes its projection.

These method and projection forms are descriptions for a **future parent-owned
evaluator**, not permission to dispatch candidate methods or execute Rust
closures. That evaluator must implement typed data semantics (including reference,
integer-width, result, and sequence behavior), operation/allocation budgets, and
fail closed on invalid receivers, failed unwraps, or out-of-range access. Syntax
admission alone intentionally cannot determine whether such operations succeed.

Limits are applied before the recursive lexer: 32 KiB source, 256 ASCII
punctuation bytes, and 64 opening delimiter bytes in total. These deliberately
conservative limits count bytes even inside comments and strings, so they can
reject otherwise valid Rust. The admission walk additionally allows at most
32 tests, 16 arguments per call, and 32 expression levels, including nested
macro arguments and the skipped iterator/projection nodes. These are fail-closed
limits, not a general Rust compatibility promise. The protected caller still
needs an outer memory/CPU deadline.

## Validation

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

Tests are public synthetic controls only. The lockfile pins all dependencies;
CI pins the toolchain. No private suite fixtures belong in this crate, CI logs,
build contexts, or candidate binaries.

## Remaining layers

1. Bind each private suite to an independently approved, typed API/bridge
   inventory. Structural audits against pristine snapshot exports are not
   production API approval or runtime qualification; do not silently fall back
   to `cargo test` or inject private tests into candidate code.
2. Build the parent-owned evaluator, typed candidate API transport, compiler
   allowlist/bridge, and a separately constrained non-root compiler phase.
3. Seal the compiled candidate and integrate the existing pre-exec bootstrap,
   deadlines, kill/reap verification, and supervisor-owned result accounting.
4. Run base/reference and hostile controls, then separately qualify the native
   host/runtime image and private shadow canary.

No catalog, hosted executor, keys, scoring, weights, or emissions are activated.
