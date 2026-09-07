# Restricted Node/TypeScript test-driver image

`Dockerfile.coding-node` implements the `node-call-ast-v1` runtime-image candidate.
It contains the existing Go supervisor, pinned Node 24.20.0, integrity-locked
TypeScript 5.9.3, a closed trusted assertion interpreter and a separate non-root
candidate API bridge. It neither runs the original suite as JavaScript nor
imports candidate code into the trusted parent.

This is a reviewed-syntax profile, not general Jest/Mocha/Vitest compatibility.
An image build does not approve a private suite or host. Curators must qualify
unchanged private base/reference suites against this exact profile and image;
do not silently translate tests or relabel older observations.

## Accepted trusted suite

```typescript
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Counter } from './counter.ts';

test('increments', async () => {
  const counter: Counter = new Counter(3);
  assert.equal(counter.add(2), 5);
  assert.equal(await counter.later(4), 9);
});
```

The parser accepts named/default/namespace candidate imports from independently
approved workspace-relative modules. `node:test`, `node:assert/strict` and literal
`Buffer.from` construction from `node:buffer` are trusted builtins, never delegated
to candidate code. Test declarations are distinct, named `test` calls with a
no-argument arrow/function body, optionally async. Every test must have at least
one assertion, and the discovered test count must equal the approved total.

Bodies support local `const`/`let` initialization, API calls/construction, public
properties and constant indexing, awaited API results, JSON-shaped literals,
undefined, big integers, byte literals, numeric negation, primitive comparisons
and limited arithmetic. Type annotations are parsed but not type-checked.
`equal`/`strictEqual` and their negations compare scalar values with Node strict
semantics, including negative zero. Deep assertions compare transported data;
opaque object references cannot establish equality, truthiness or object identity.

Loops, conditions, hooks, fixtures, skips, test contexts, nested tests, assertion
messages, exception assertions, arbitrary imports, dynamic loading, local callable
execution, prototype access, spreading/destructuring and unsupported expressions
are rejected before candidate execution. Reference-valued API arguments and
arbitrary native objects are outside this profile. Type-only imports, decorators
and unsupported TypeScript test syntax are not silently erased into acceptance.

The pinned TypeScript parser produces a closed instruction representation. It
does not evaluate, compile or execute the suite. Assertions and expected values
remain in the trusted process; only API targets and argument values go to the
candidate. A fresh process per test resets module globals but does not promise
the semantics of a framework that shares module state across tests.

## Candidate execution and isolation

The bridge runs `.js`, `.mjs`, `.cjs`, `.ts`, `.mts` and `.cts` workspace modules.
TypeScript is transpiled inside confinement with explicit ES2022/ES-module or
CommonJS output and interop support. No candidate tsconfig, build hooks or external
package installation is used. This is not type-checking, tsconfig path mapping,
or qualification of arbitrary repository dependencies. Any additional dependencies
must be supplied in a separately reviewed immutable runtime.

Before candidate import, the native addon verifies real/effective/saved non-root
UID/GID, empty supplementary groups and zero permitted/effective/inheritable
capabilities. It applies a Linux amd64 seccomp filter to every existing thread
with mandatory `SECCOMP_FILTER_FLAG_TSYNC`; a positive return or error fails
startup, with no single-thread fallback. New same-process threads inherit it.
See [Linux seccomp thread synchronization](https://man7.org/linux/man-pages/man2/seccomp.2.html).

The filter denies process creation, exec, process-group escape, namespace changes,
signals, ptrace/process-memory operations and io_uring. The outer isolated
rootless container still provides network denial, protected root-owned `0700`
grader/control paths, read-only root, resource bounds and process-group cleanup.
Do not run candidates directly on a host using this addon.

The driver's immutable entrypoint clears the environment before Node starts.
Candidate startup gets only a fixed PATH, no protected descriptors, and a separate
API pipe; ordinary stdout/stderr are discarded. The parent requires a successful
confinement handshake before sending any API input. Startup failure is an
infrastructure error and produces no authoritative test report.

Each API frame is at most 64 KiB and has a fresh random correlation ID. Duplicate
JSON keys, malformed UTF-8, unknown fields, unsolicited/multiple frames, incorrect
IDs and oversized data are rejected. Wire values use explicit tags so user keys
cannot impersonate references; nesting and element counts are bounded. Candidate
responses remain proposed API values, not trusted internal-language attestations.

The trusted parent owns comparisons and totals, kills/reaps the child before
accepting completion or starting the next test, and creates an exclusive root-owned
report only after all children terminate. Early exit, fake reports, wrong values,
timeouts and malformed API replies do not pass tests. The Go supervisor separately
validates the nonce-bound report and verifies process-group termination.

## Command and qualification

An independently approved grading profile may use this command shape:

```text
dittobench-test-driver --group hidden --suite tests/hidden.ts --module counter.ts --candidate-timeout-ms 10000
```

Use `visible` for protected source in the pristine workspace and `hidden` for
the protected grader mount. The supervisor appends the report path, nonce, expected
count and candidate UID/GID. The per-test deadline cannot extend the outer command
deadline. Results expose no private test names, errors, output or assertions.

Validation from the monorepo root:

```bash
npm --prefix services/dittobench-api/coding_runtime/node ci --ignore-scripts --no-audit --no-fund
node --test services/dittobench-api/coding_runtime/node/'*.test.cjs'
docker build -f services/dittobench-api/Dockerfile.coding-node-confinement -t coding-node-confinement:synthetic .
bash scripts/test-coding-node-confinement.sh coding-node-confinement:synthetic
docker build -f services/dittobench-api/Dockerfile.coding-node --target test -t coding-node:synthetic .
bash scripts/test-coding-node-driver.sh coding-node:synthetic
```

The standalone confinement image and `test` target contain public probes and carry
the fixture label. The final `runtime` target excludes probes, private artifacts
and credential-shaped environment and stamps an explicit source revision.

This PR adds no catalog entry, approved image digest, native host apply, private
execution authority or reward activation. Native OCI import must explicitly admit
the Node profile in a later integrated layer; Python-only approvals cannot be
reused for it. Actual private suite compatibility, host/resource qualification,
custody/recovery and the live shadow canary remain separate acceptance gates.
