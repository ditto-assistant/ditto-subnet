# Coding sandbox-executor shadow core

`internal/codingexecutor` implements the shadow container adapter between the
coding runner/grader interfaces and a pinned trusted supervisor image. It has no
scorer route, does not load private catalog data, and cannot affect a benchmark
score or validator weight.

## Ownership and trust boundary

One immutable executor configuration binds:

- the complete validated coding-grader manifest and plan;
- an image reference ending in the exact selected OCI digest;
- `linux/amd64`;
- the complete resource-profile digest and concrete limits;
- a fixed supervisor entrypoint;
- a non-root candidate UID/GID;
- mandatory rootless and isolated-daemon checks.

Preflight queries the selected Docker daemon and image. It requires rootless
security options, the release-owned isolated-daemon label, an exact repo/image
digest, the declared platform, no image-declared volumes, and no
credential-shaped baked environment. It then creates (but does not start) a
policy-probe container and verifies the observed image ID, HostConfig, mounts,
network, capabilities, resources, security options, and tmpfs before returning
a grader attestation.

The public trusted supervisor is built from
`cmd/dittobench-coding-supervisor`; the certification-only image is defined by
`Dockerfile.coding-supervisor`. It is part of the pinned grader image, not
candidate code.
It must run as the container parent, drop the child to the declared candidate
UID/GID, keep the control and protected mounts inaccessible to that child, kill
the complete child process tree, and write one bounded nonce-bound response.
Test mode additionally requires the fixed `dittobench-test-driver`. The public
image ships only a synthetic certification driver; a repository-specific
production driver must run hidden tests as the trusted parent, isolate candidate
code in a non-root child, and emit the separate root-owned nonce-bound test
report. Its final environment and build provenance remain a private-artifact
deliverable.

The public image is labeled as a certification fixture and is rejected by
normal executor preflight. Only the opt-in integration test enables it; a
production manifest must select a different reviewed image digest and trusted
repository driver.

## Container policy

Every command uses `docker create`, inspection of the resulting HostConfig and
mounts, then `docker start --attach`. It receives a fresh exact-name container
with:

```text
--pull never
--platform linux/amd64
--network none
--read-only
--ipc none
--uts private
--init
--cap-drop ALL
--security-opt no-new-privileges
--memory / --memory-swap
--cpus
--pids-limit
--tmpfs /tmp:rw,noexec,nosuid,nodev
--log-driver none
```

The trusted supervisor parent runs as root only inside the rootless user
namespace with the minimal `CHOWN`, `DAC_OVERRIDE`, `SETUID`, and `SETGID`
capabilities. Authoring/build children run from candidate-owned scratch copies
under the plan-bound non-root UID/GID and inherit no root capabilities. The
trusted test driver retains only what it needs to launch its own non-root
candidate child and must not pass the control or protected descriptors into it.

No environment variables, proxy settings, credentials, host gateway, published
port, Docker socket, or caller-selected entrypoint are supplied.

The validator-owned candidate workspace is read-only in every container.
Authoring/build children receive a bounded writable scratch copy. The coding
runner latches the supervisor's scratch-mutation signal; build output is
discarded. Only test phases receive the separate read-only protected bundle;
the trusted driver must keep it outside the candidate child namespace.

## Receipt and cleanup

The host writes a mode-0400 request containing the exact argv, command digest,
timeout, expected test count, random nonce, and candidate identity. The
supervisor writes a bounded mode-safe response through the private control
mount. The host rejects unknown fields, wrong nonce/mode/command/count,
candidate output during grading, an incomplete process-tree teardown, symlinks,
oversized responses, and trailing JSON.

For test mode the supervisor does not infer success from the candidate process
exit code. It requires the fixed trusted driver to create a separate mode-0600
report containing the same nonce and exact expected count. Missing, stale,
symlinked, candidate-owned, or incoherent reports are infrastructure failures.
The supervisor passes only root-owned report/nonce/count parameters plus the
plan-bound candidate UID/GID to that driver; a production driver must omit
those descriptors when it launches candidate code.

Authoring and build commands execute from bounded scratch copies rather than
the validator-owned bind mount. Authoring receipts bind a deterministic
before/after tree comparison through `workspace_mutated`; the coding runner
latches that signal as candidate integrity even though the original workspace
remains pristine. Build scratch output is discarded.

The fixed wire is:

```json
{
  "schema": "dittobench-coding-supervisor-request-v1",
  "nonce": "random-192-bit-hex",
  "mode": "authoring | build | test",
  "command_id": "opaque-manifest-command",
  "command_sha256": "lowercase-sha256",
  "argv": ["python", "-m", "pytest"],
  "timeout_milliseconds": 60000,
  "expected_total": 3,
  "candidate_uid": 65532,
  "candidate_gid": 65532
}
```

```json
{
  "schema": "dittobench-coding-supervisor-response-v1",
  "nonce": "same-random-192-bit-hex",
  "mode": "test",
  "command_id": "opaque-manifest-command",
  "command_sha256": "same-lowercase-sha256",
  "returncode": 0,
  "passed": 3,
  "total": 3,
  "completed": true,
  "timed_out": false,
  "stdout": "",
  "stderr": "",
  "workspace_mutated": false,
  "process_tree_dead": true
}
```

Candidate timeout is represented by a coherent supervisor receipt. Missing or
malformed supervisor evidence is validator infrastructure. The exact random
container ID is inspected, force-removed with its volumes, and proven absent
after every attempt. A failed create is swept by exact random name long enough
to catch late daemon materialization. Cleanup failures are joined to any prior
execution failure instead of being suppressed.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingexecutor ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go vet ./internal/codingexecutor ./internal/codingrunner ./internal/codinggrader ./internal/codingcontract
go test ./...
```

The unit suite uses an injected Docker control client plus the real supervisor
subprocess and never runs an untrusted repository on the test host. Build the
public certification image from the monorepo root, publish it to a private test
registry by immutable digest, then run the opt-in rootless-daemon certification:

```bash
docker build -f services/dittobench-api/Dockerfile.coding-supervisor \
  -t REGISTRY/dittobench-coding-supervisor:cert .
export DITTOBENCH_CODING_EXECUTOR_IMAGE='REGISTRY/dittobench-coding-supervisor@sha256:...'
(cd services/dittobench-api && \
  go test ./internal/codingexecutor -run TestDockerExecutorIntegration -v)
```

The gate refuses a rootful, unlabeled, or non-isolated daemon. Production still
requires a repository-specific trusted test driver, external stale-resource
sweeper, host firewall proof, private catalog transport, and hostile-container
certification.
