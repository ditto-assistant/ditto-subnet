# Native Node/TypeScript driver

This directory implements the `node-call-ast-v1` runtime-image candidate, not an
approved private runtime deployment. See the [driver contract](../../docs/coding-node-driver-v2.md)
for supported syntax, integration tests and remaining qualification gates. No
private suite, reference patch, catalog approval or deployment is included.

The native confinement addon checks real/effective/saved non-root IDs, empty
supplementary groups and zero permitted/effective/inheritable capabilities.
Before candidate import it installs a Linux amd64 seccomp filter with mandatory
thread synchronization. A main-thread-only filter is insufficient for Node's
already-running V8/libuv threads. Failed synchronization has no fallback.

The filter prevents new processes, exec, signal-based interference, process-group
escape, ptrace/process-memory APIs, namespace changes and io_uring. Same-process
threads remain supported. Network, filesystem and resource isolation must still
be enforced by the outer rootless executor. This addon is not a host sandbox.

The public Docker fixture verifies the extra filter on every preexisting thread,
and inheritance by a newly created Worker. It checks failed process creation,
signal and setuid calls, protected-file denial and minimal child environment.
The fixture carries the supervisor-fixture label and must never be approved as
a production image.

```bash
docker build -f services/dittobench-api/Dockerfile.coding-node-confinement \
  -t coding-node-confinement:qualification-v2 .
bash scripts/test-coding-node-confinement.sh coding-node-confinement:qualification-v2
node --test services/dittobench-api/coding_runtime/node/wire.test.cjs
```

The result wire preserves JSON-shaped data, undefined, negative zero, big integers
and Buffers through explicit tags, with depth/count bounds. Candidate-side values
remain untrusted proposals. The trusted parent owns all assertions,
counts, timeouts, nonces and final reports; no test source or expected answer may
enter the candidate process.

The confined bridge has a bounded, correlated API channel separate from ordinary
stdout/stderr. Its pinned TypeScript compiler executes inside confinement without
candidate build hooks or tsconfig loading; container tests exercise enums,
constructor parameter properties, classes, method receivers, asynchronous returns
and lossless wire values. Those tests prove bridge behavior, not trusted grading.

The trusted assertion driver is tested through the Go supervisor with public
correct and adversarial candidates. No hidden suite or expected value is sent
through the bridge. Private suite compatibility, native host qualification and
explicit image/profile approval remain required before selection.

Reference: [Linux seccomp thread synchronization](https://man7.org/linux/man-pages/man2/seccomp.2.html).
