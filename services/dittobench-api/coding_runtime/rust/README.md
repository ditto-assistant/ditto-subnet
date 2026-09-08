# Protected Rust suite admission

This crate is the first Rust oracle layer: a non-executing, closed syntax
admission gate. It is **not a grader** and is not wired into the supervisor or
runtime image selection. Admitting a suite never means its tests passed.

The trusted controller supplies the independently approved crate/function names,
exact source SHA-256, and nonzero expected test count. The parser does not discover
authority from candidate files or suite imports. `syn` parses source and macro
arguments without loading modules or expanding macros. There is no filesystem,
compiler, cargo, subprocess, or candidate execution path in this library.

The source and returned opaque `AdmittedSuite` must remain in the protected
Platform-side grader process. The result has no public AST accessor or
Debug/Display/serialization implementation. Parser errors expose only fixed
categories, never private names, literals, source excerpts, or spans. The caller
must enforce protected input custody, process resource limits, and private error
routing; this library is not an isolation boundary.

## Closed subset

- Explicit imports of approved functions from exactly one approved crate, or
  fully qualified calls to those same functions. No wildcard, alias, nested,
  absolute, generic, method, or dynamically selected calls.
- Unique, private, zero-argument `#[test] fn` items with default return type and
  at least one assertion. No extra attributes, helper functions, modules,
  constants, statics, custom macros, or other items.
- Immutable untyped local bindings, without shadowing or forward references.
- `assert!` and `assert_eq!` with exact argument counts and no format arguments.
- Integer, Boolean, string, and character literals; arrays, tuples, immutable
  references, parentheses, `!`/unary `-`, `==`/`!=`/`&&`/`||`, `None`, and single
  argument `Some`/`Ok`/`Err` descriptions. Integers are bounded to `u64` magnitude
  and explicit supported suffixes. Admission is not Rust type checking.
- `vec![a, b]` descriptions with recursively parsed arguments. Repetition and
  all other expression macros are rejected, including macros nested in vectors.

Limits are applied before the recursive lexer: 32 KiB source, 256 ASCII
punctuation bytes, and 64 opening delimiter bytes in total. These deliberately
conservative limits count bytes even inside comments and strings, so they can
reject otherwise valid Rust. The admission walk additionally allows at most
32 tests, 16 arguments per call, and 32 expression levels, including nested
macro arguments. These are fail-closed limits, not a general Rust compatibility
promise. The protected caller still needs an outer memory/CPU deadline.

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

1. Audit private-suite syntax against this closed subset and add explicitly
   specified constructs with regression coverage; do not silently fall back to
   `cargo test` or inject private tests into candidate code.
2. Build the parent-owned evaluator, typed candidate API transport, compiler
   allowlist/bridge, and a separately constrained non-root compiler phase.
3. Seal the compiled candidate and integrate the existing pre-exec bootstrap,
   deadlines, kill/reap verification, and supervisor-owned result accounting.
4. Run base/reference and hostile controls, then separately qualify the native
   host/runtime image and private shadow canary.

No catalog, hosted executor, keys, scoring, weights, or emissions are activated.
