# Parent-owned typed data evaluation

`Program::bind` consumes an opaque admitted suite plus independently approved
function signatures. Its schema must name exactly the admission allowlist, not
a subset or a module prefix. A domain-separated program digest binds the source,
admission policy, parameter order/types, and return types. It does not identify
a transport implementation, compiled artifact, runtime image, or kernel.

`Program::run` interprets the admitted descriptions in the protected parent. The
candidate interface receives only canonical approved function names and typed
arguments. It receives no suite, test name/index, assertion, expected value, or
pass-count authority. Tests use a fresh `ApiFactory::start` session each time.
Every successfully started session must finish, including assertion, budget,
type, and API failures. The evaluator returns no report if cleanup fails.

## Typed data and evaluation semantics

- Integers retain signedness and width, with range checks before construction.
  This profile fixes `usize`/`isize` to 64 bits. Negation is checked in debug and
  optimized builds; Boolean values are never integers or truthiness shortcuts.
- Arrays retain their lengths. Vectors, slices, tuples, immutable reference
  descriptions, options, and results have distinct schemas and variants.
  Candidate return schemas must match exactly; there is no return-value coercion.
- Text is owned UTF-8 data; a reference-to-text description represents a string
  borrow. `.len()` counts UTF-8 bytes. Supported equality is structural, with
  explicit sequence and text-borrow compatibility, never stringification.
  Tuples/options/results keep exact generic types, arrays keep equal-length
  comparison requirements, and string borrows do not erase reference depth.
- The only implicit API argument conversion is a borrowed array/vector to an
  explicitly declared borrowed slice with the same element type. Integer casts,
  owned-container conversions, custom objects, and custom trait dispatch do not
  exist at this boundary.
- Assertions, expected values, short-circuiting, primitive constants, enum
  inspection, indexing, and iterator projections stay parent-owned. Projection
  callbacks are data expressions, not candidate code. Static element hints
  handle empty projected sequences without requesting extra candidate results.
  Short-circuit operands must have Boolean type descriptions even when skipped.
  Empty projections preserve the body's known type over an incompatible expected
  type; constant integer bodies retain checked defaults without running a callback.
- A false assertion, failed unwrap/index, checked negation overflow, candidate
  failure, or wrong return schema fails that test. A valid domain `Err` value is
  data, not a transport failure. Transport, oracle/type, budget, and cleanup
  errors return no completed report. No expected-error assertion can turn an
  infrastructure failure into a pass.

This is a **closed immutable data model**, not the Rust compiler. References are
bounded copied descriptions, not process pointers. Moves, borrow lifetimes,
alias identity, custom destructors, and arbitrary Rust type inference are not
modeled. Unsuffixed integers use available expression/API context, otherwise
`i32`; an already-bound local keeps that concrete type. Unresolved empty or enum
expressions without sufficient context fail closed. Some syntactically admitted
Rust therefore cannot yet be evaluated. Private qualification must establish
that the approved suites use these supported semantics; syntax admission alone
is not that evidence.

## Bounds and adapter obligations

Each value is bounded to 4096 data nodes and 64 KiB of charged payload. Type
descriptions have at most 256 nodes and 16 levels. A test defaults to 100,000
expression steps, 128 calls, 262,144 cumulative charged data nodes, and 4 MiB of
cumulative charged payload; callers may lower, but not raise, these bounds.
Budgets reset per test, and final counts are derived only from completed tests.
Charged payload is not a measurement of allocator overhead or process RSS.

`CandidateApi` and `ApiFactory` remain trusted adapter interfaces. The
[bounded data channel](WIRE.md) is available, but it is **not a sandbox launcher
or cleanup adapter**. A production adapter must:

1. Launch a fresh confined candidate process for each test, never link/load
   candidate code into the parent, and bind the exact source/API/compiler profile.
2. Bound raw frame bytes before decoding, preserve integer values losslessly,
   enforce request identity/ordering and deadlines, and distinguish candidate
   failure from infrastructure failure. Never deserialize parent oracle objects.
3. Terminate/reap and verify cleanup before reporting `finish()` success. A
   trusted-adapter panic/crash requires an outer supervisor kill/reap guard;
   the library does not catch panics or enforce a wall-clock deadline itself.
4. Keep source, values, schemas, and receipts in protected parent storage. Values
   and programs intentionally lack automatic Debug/Display/serialization output.
5. Bind limits, transport/compiler revisions, candidate digest, image and host
   identity, and cleanup evidence in the enclosing private execution receipt.

Current tests use public synthetic adapters only. They prove core behavior, not
IPC isolation, compiler confinement, private base/reference performance, native
host qualification, or a deployed canary. No activation gate changes here.
