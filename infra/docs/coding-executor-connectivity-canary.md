# Dedicated executor connectivity canary

This is an operator-controlled, ticketless proof of the validator-to-executor
mTLS path. It is not a worker, deployment controller, assignment, or coding
evaluation. Every committed gate is false.

## Preconditions

Run only after the complete PR stack has merged into one released validator
stack and both hosts have been converged to that exact revision. The executor
must already have its default-off scorer, signed Unix ingress, verified server
identity, and private mTLS transport explicitly enabled. The validator must
already have its root-owned client identity verified.

The validator must use the updater-validated managed release under
`.validator-stack-update/current`. The runner rejects a source build, mutable
tag, build context, non-current descriptor, dirty checkout, public executor
origin, `/dev/null` credential source, or revision disagreement.

The operator sets these validator-stack role values for one reviewed converge:

```yaml
validator_stack_coding_executor_identity_enabled: true
validator_stack_coding_executor_connectivity_canary_enabled: false
validator_stack_coding_executor_connectivity_canary_run_enabled: true
validator_stack_coding_executor_remote_enabled: false
validator_stack_coding_shadow_enabled: false
validator_stack_coding_executor_base_url: https://10.x.y.z:9443
```

The role requires the client CA, certificate, and key to be pre-positioned at
the fixed root-only paths documented in `coding-executor-hosts.md`. Certificate
or key contents never enter Git, Ansible variables, process arguments, logs, or
the diagnostic receipt.

## Execution

The role invokes `scripts/run-coding-executor-connectivity-canary.py`. The
runner revalidates the managed Compose model, exact Git revision, immutable
validator image and descriptor digests, canary-only gates, fixed in-container
credential paths, and three distinct root-owned mode-`0400` host files. It then
runs only the `ditto-subnet` service with `--rm --no-deps --pull never`.

The persistent validator environment keeps all runtime and canary gates false.
The runner injects the canary-only flag, fixed in-container secret paths, and
private executor origin only into its Compose `config` and `run` subprocesses.
A normal validator restart during or after the probe therefore remains a normal
validator restart rather than entering one-shot mode.

The validator process sends one mTLS-authenticated `GET /v1/coding/ready`,
verifies the fixed response, and exits before loading its signing key or
constructing Platform, Pylon, telemetry, or worker clients.

## Diagnostic receipt

Success atomically writes a mode-`0600` JSON receipt at:

```text
/var/lib/ditto-validator/coding-executor-canary/receipt.json
```

The receipt records public release provenance, the public validator hotkey,
timestamps, and a SHA-256 of the private executor origin. It explicitly records
that no ticket authority, Platform contact, candidate execution, or S3 access
occurred. It is unsigned operator-local diagnostic evidence and is never valid
for certification, scoring, assignment, weights, or emissions.

After recording the result, return both canary run controls to false. Do not
enable remote execution in the same converge. The next reviewed slice may use
the successful receipt only as a human rollout prerequisite; code must not
treat it as execution authority.
