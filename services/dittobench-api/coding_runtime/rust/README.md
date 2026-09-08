# Protected Rust oracle core

This crate provides a non-executing, closed syntax admission gate and a separate
parent-owned typed data evaluator. It is **not a standalone runtime grader** and
is not wired into the supervisor or runtime image selection. Admitting a suite
never means its tests passed. See [the evaluator boundary](EVALUATOR.md) and
[the bounded data channel](WIRE.md).
The [fixed API bridge](BRIDGE.md) adds a public compiled control.
The [Linux compiler and process adapters](RUNTIME.md) add fixed compilation,
sealed executables, pre-exec confinement, and cleanup-owned evaluation sessions.
The optional [protected supervisor driver](DRIVER.md) connects authenticated
freeze/API/image bindings to the private report and evidence chain. Private
runtime qualification and operational activation remain pending.
The [frozen compiler-input boundary](INPUTS.md) provides descriptor-relative
manifest capture, readonly materialization, and a fixed compiler recipe.

The trusted controller supplies the independently approved crate/function names,
exact source SHA-256, and nonzero expected test count. The parser does not discover
authority from candidate files or suite imports. `syn` parses source and macro
arguments without loading modules or expanding macros. Admission and evaluation
do not invoke a compiler or load candidate code. The separate Linux adapters
execute only the fixed compiler recipe and confined candidate process. There is
no Cargo or in-process candidate loading path. The data channel uses OS entropy
and a private Unix socket; the process adapter owns termination and reap.
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

These method and projection forms are descriptions for the **parent-owned data
evaluator**, not permission to dispatch candidate methods or execute Rust
closures. It implements checked integer-width, reference-description, result,
and sequence operations with operation/data budgets. Invalid receivers stop
evaluation; failed unwraps and out-of-range accesses fail the current test.
Syntax admission alone cannot determine whether such operations succeed. This
closed data model is not a general Rust type, ownership, or lifetime checker.

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
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-features
cargo test --locked --no-default-features
```

Tests are public synthetic controls only. The lockfile pins all dependencies;
CI pins the toolchain. No private suite fixtures belong in this crate, CI logs,
build contexts, or candidate binaries.

## Remaining layers

1. Bind each private suite to an independently approved, typed API/bridge
   inventory. Structural audits against pristine snapshot exports are not
   production API approval or runtime qualification; do not silently fall back
   to `cargo test` or inject private tests into candidate code.
2. Qualify the integrated compiler, typed transport, and parent evaluator against
   private controls in the approved native execution profile.
3. Qualify the integrated freeze/image authority, supervisor-owned accounting,
   whole-container cleanup and signed/sealed evidence path on the native host.
4. Run base/reference and hostile controls, then separately qualify the native
   host/runtime image and private shadow canary.

No catalog, hosted executor, keys, scoring, weights, or emissions are activated.
