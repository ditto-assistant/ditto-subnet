# Restricted Python test-driver image

`Dockerfile.coding-python` builds the `python-call-ast-v2` runtime-image
candidate. It contains the existing Go supervisor, pinned Python 3.13.14, a
restricted parent-owned assertion interpreter, and an isolated candidate API
bridge. Pytest and its dependencies are hash-locked for authoring diagnostics;
the trusted grader does not parse pytest output or import candidate code.

This is a limited Python profile, not support for every private task. It does
not approve an image/profile, alter a private artifact, install a worker, publish
a release or enable scoring. Node, Go, Rust and richer Python test syntax need
separately reviewed drivers. Existing private base/reference observations cannot
be relabeled as execution through this image.

## Oracle boundary

The protected suite remains source text inside the trusted grader. The parent
parses its AST but never executes it with Python `eval`, `exec`, or an import of
the candidate. It interprets only:

- absolute `from module import export` declarations, with public dotted
  module names, simple export identifiers and an independently approved module allowlist;
- distinct, undecorated, no-argument `test_*` functions with assertions and calls
  (an optional literal `-> None` return annotation is allowed);
- local assignments, candidate API calls and public attributes;
- JSON and byte-string literals, tuples/lists/string-keyed dictionaries, numeric
  negation, indexing, single equality/inequality comparisons, singleton identity
  checks against `None`/booleans, and short-circuit `and`/`or`;
- bounded list/tuple loops and `with pytest.raises(BuiltinError): candidate_call()`
  checks. Pytest is a parent-owned syntax marker, never imported by the oracle.

Unbounded loops, fixtures, decorators, other context managers, standard-library imports,
splats, assertion messages, chained comparisons, arbitrary identity checks, dunder access,
unbound locals and unsupported statements are rejected. The driver checks the
number of discovered test functions against the approved expected count. It does
not fill an expected count with invented successes.

Each test receives a fresh candidate process. Imports and calls are represented
by opaque remote targets. The child loads only the named workspace module and
returns JSON values or opaque object references. References support subsequent
attribute/method operations, not arbitrary reference-valued arguments. The
profile accepts JSON-shaped data, builtin byte strings and tuples. Tuples retain
their type through an explicit wire tag; custom container types and nonfinite
values are not silently converted into equivalent-looking lists/numbers. Byte
strings use explicit base64-tagged envelopes, including within lists/dictionaries
and tuples, without interpreting user dictionary keys as protocol tags.
Qualify the exact suite and API against these restrictions.

Expected-exception checks admit only the fixed builtin exception family allowlist
and a single candidate-call expression. Only a correlated API-call exception can
satisfy them. Import failures, malformed frames, process exits, timeouts, assertion
failures and a normally returning API cannot. Exception messages and custom type
names are not transported. Named builtin ancestry permits ordinary subclasses
without loading any candidate type in the parent. Context aliases, message matching
and candidate-defined exception types remain unsupported. Loops are bounded by
10,000 parent statement steps and the per-test deadline; possibly unbound names
after zero-iteration loops or expected exceptions are rejected at admission.

The v2 profile changes data/protocol and syntax admission. An approved v1 image
or profile does not authorize it: requalify and bind the exact new runtime image.
The native OCI importer accepts both supported Python profiles and binds the
actual image profile into its approval. Archive verification and post-import
inspection reject any profile substitution, even between these supported versions.

Before candidate import, an immutable bridge handshake confirms successful child
initialization/confinement. A missing handshake is infrastructure failure, not a
candidate score. Ordinary candidate stdout/stderr are discarded separately from
the answer channel. The entire candidate-side runtime remains untrusted: replies
are proposed API values, not trusted internal Python return-value attestations.

Every RPC has a fresh random correlation ID and a 64 KiB frame limit. Expected
values and assertions never enter the request. The parent compares proposed API
values itself. Missing, unsolicited, malformed or incorrect responses fail the
test; a child exiting zero or printing a fake test report cannot pass it.

The fresh-process-per-test policy is explicit. It does not preserve pytest's
module-global state across test functions. Private suite compatibility and
base/reference behavior must be re-established under this execution profile,
even when the source test files are unchanged. A parse-only audit is not grading.

## Process and filesystem confinement

Plain `str.encode()` without arguments is a bounded parent-owned UTF-8 data
operation. It uses the exact built-in string type and fixed strict UTF-8 codec;
subclass hooks, arbitrary codecs/error handlers and method references are not
delegated to parent Python execution. Remote method receivers are evaluated once.

The parent is root only inside the existing isolated rootless executor container.
The child starts with the plan-bound nonzero UID/GID, no supplementary groups,
no inherited private descriptors, an explicit minimal environment, and isolated
Python startup (`-I`). Candidate modules are imported only after a Linux amd64
seccomp filter is installed. Other architectures/ABIs, including x32, fail closed.

The child filter denies new processes, exec, process-group escape,
ptrace/process-memory access, namespace changes, signal syscalls and io_uring.
It permits only thread-group clones, whose threads are killed/reaped with the
child process. `clone3` returns ENOSYS so libc can use the checked legacy clone
path; ordinary process clones remain denied. APIs requiring subprocesses are
outside this profile and must not be selected without a different qualification.
The container's network denial, resource limits, read-only root and ordinary
seccomp policy remain additional requirements. This is not a general host sandbox
and must never be used to execute untrusted repositories directly on a host.

The control and grader directories must be canonical root-owned mode 0700.
The child cannot traverse them. Suite paths cannot escape their fixed mount or
use symlinks. Candidate pipes are private to one test process; the parent kills
and reaps that process before accepting its test completion or starting another.
The outer Go supervisor and host executor still verify their own cleanup.

Container testing identified two prerequisites in the shared executor:

- Docker's default private UTS namespace is represented by an empty `UTSMode`.
  The invalid CLI spelling `--uts private` is removed; `host` remains rejected.
- The trusted parent requires `CAP_KILL` to terminate its different-UID child.
  It is added to the exact inspected parent capability allowlist, alongside
  CHOWN, DAC_OVERRIDE, SETUID and SETGID. Non-root children retain no effective
  capabilities. Install/requalify the executor and image together.

## Approved command shape

A native grading profile can select a command shaped like:

```text
dittobench-test-driver --group hidden --suite tests/test_hidden.py --module module_under_test --candidate-timeout-ms 10000
```

The relative suite path and timeout are curator-approved command data, not a
candidate-selected path. `visible` reads the protected visible test path in the
pristine workspace; `hidden` reads beneath `/run/dittobench-grader`. The supervisor
appends its report path, nonce, expected count and candidate UID/GID. Report
creation is exclusive and root-owned, and follows verified child termination.
Unsupported suites produce no successful authoritative report.

The repeated `--module` options identify candidate APIs independently of test
imports. Standard-library/test-framework names and unlisted helpers are rejected;
the driver must not accidentally delegate a trusted oracle helper to candidate
code merely because the suite imports it.

The command timeout remains controlled by the existing approved supervisor
request. The additional child timeout is per test, at most 300 seconds; it cannot
extend the outer grading deadline. Grading output remains private.

## Build and validation

From the monorepo root:

```bash
docker build -f services/dittobench-api/Dockerfile.coding-python \
  --target test -t coding-python:synthetic .
bash scripts/test-coding-python-driver.sh coding-python:synthetic
docker build -f services/dittobench-api/Dockerfile.coding-python \
  --target runtime -t coding-python:runtime .
uv run pytest -q ditto/tests/test_coding_python_driver.py
```

The `test` stage adds only a public synthetic probe and carries the certification
fixture label. The default/final `runtime` stage contains no probe, corpus,
private test, reference patch or credential. Its base images and diagnostic
dependencies are pinned. A local image ID is not a published/approved repository
digest: export or publish the intended runtime manifest, verify it, and approve
that exact immutable digest before constructing a production profile.

CI runs real containers for successful/incorrect API values, early exit, forged
reports, excessive output, timeout, protected-file access, report writes, fork,
exec, setsid, setuid, effective capabilities, environment confinement, tuple
semantics, unsupported syntax and count mismatch. Unit tests additionally cover
AST admission, local scope, dictionary/JSON restrictions and parent-owned counts.

These synthetic tests may run on the development/CI Docker daemon. They do not
prove the separate production rootless daemon, its ownership label, actual private
base/reference results, deployed worker convergence, custody service or live
canary. The production executor retains all of those admission requirements.
