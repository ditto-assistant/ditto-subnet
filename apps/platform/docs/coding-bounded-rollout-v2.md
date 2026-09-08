# Bounded single-host shadow rollout

This operator-only controller runs a finite, independently approved cohort of
already-created and admitted assignments. It does not select tasks, register a
release, create assignments, provision hosts/keys, approve evidence, enable a
scheduler, or change weights. The initial scope is one exact native host/boot,
not multi-host scheduling. Every production step still requires separate approval.

## Approval and bounds

The private approval model is `BoundedRolloutApproval`, schema
`dittobench-coding-bounded-rollout-v2`, purpose `shadow-cohort-once`. Supply its
SHA independently through the root-owned manual systemd unit; a downloaded file
and a self-computed sidecar are not independent approval. Required commitments:

- Exact installed runtime revision/archive SHA and controller source-file SHA.
- Exact host machine-ID SHA and boot UUID.
- The independently approved v3 connectivity profile's canonical JSON SHA and
  a worker-owned protected copy of that profile.
- Independently accepted host, native-matrix, canary, custody, recovery and
  rollback evidence digests. The earlier evidence verifier's pending-acceptance
  list is not itself approval; this controller never manufactures acceptance.
- An explicit 1–64 assignment list, 1–4 parallel bound, expiry, total reserved
  cost ceiling, and boolean deciding whether completed candidate failures may
  continue. Infrastructure/integrity failures always stop admission.

Each list item binds evaluation/attempt/worker UUIDs, exact runtime-config bytes,
assignment and inference-policy digests, assignment deadline, and its policy's
maximum cost ceiling. IDs/config paths cannot repeat. Configs are read/hash-checked
once into immutable runtime objects; policy/profile bytes and credentials are not
reloaded from config paths when later work begins. Existing database authority
still independently rechecks each immutable assignment and irreversible start.
Private transport/keys remain separately checked by the existing runtime.

The controller reserves the sum of maximum per-assignment inference costs
against the cohort ceiling before launching anything. No refund/retry accounting
can admit extra work. These are the existing enforced policy/reservation ceilings,
not a guarantee against a provider billing outside its contract; vendor account
limits and pricing/custody approval remain operator responsibilities.

All items must share one database configuration, unique private runtime roots,
canonical router endpoints, and the approved installed worker/interpreter.
Overlapping runtimes cannot share a router; later assignments may reuse a router
only after the previous runtime has fully returned from cleanup. This permits
the full finite cohort without expanding the simultaneous endpoint allowance.
Router/proxy destinations must be in the approved network profile. The controller
checks every unstarted assignment and deadline before launch; the child runtime
rechecks before its irreversible start. Config byte mismatch fails before private
configuration dependencies are loaded. No private source or secret belongs in Git.

## Native host and installation

Run only as `ditto-coding-hosted` in the nondelegated
`system.slice/ditto-coding-hosted-worker.service` cgroup on the pinned
`ditto-coding-hosted-v2` Linux host. Use the protected installed Platform Python
environment from `/opt/ditto-coding-hosted/<revision>/apps/platform/.venv`.
The controller checks its installation/source, native socket, empty client
directory, boot identity, rootless/containerd/cgroup metadata and zero containers.
These metadata checks do not replace actual host enforcement qualification.

Pre-provision a dedicated worker-owned mode-0700 directory at
`/var/lib/ditto-coding-hosted/rollout`. All inputs follow existing protected,
owner-only, canonical file rules. The installed native runtime and independent
evidence approval must precede this step. No missing host directory is repaired.

The connectivity role remains default-off. Its default worker mode is `single`,
preserving one-attempt behavior and v2 policy. For the bounded entrypoint, supply:

```yaml
coding_hosted_worker_mode: rollout
coding_hosted_rollout_approval_sha256: INDEPENDENTLY_APPROVED_64_HEX_SHA
coding_hosted_rollout_connectivity_sha256: INDEPENDENTLY_APPROVED_CANONICAL_PROFILE_SHA
```

`coding_hosted_worker_config` then names the protected rollout approval file.
Installation still does not start/enable the unit. Root `ExecStartPre` requires
the actual installed profile to match the independent hash and v3 schema before
installing its rules. The controller checks the same hash against its protected
copy. v3 permits at most eight explicit candidate router/proxy endpoint pairs;
v2 retains its two-endpoint cap. All address, cgroup, UID, expiry and reply rules
remain enforced. The network window must cover rollout expiry plus one hour for
the existing finalization/drain allowance. A normal v2 install cannot activate v3.

## Stop, retention and status

A host-wide file lock excludes overlapping controllers. Before any runtime
starts, exclusive fsynced consumed/active markers and a redacted plan record bind
the cohort. A different approval cannot bypass retained active state. Immutable
launch/finish/failure records provide progress; launch is intent, not proof that
the database start committed.

Failure, expiry or SIGINT/SIGTERM stops new admission, then cancels/drains active
runtimes with the existing 30-minute cleanup allowance. The rollout unit uses
mixed signaling and retains the 35-minute systemd stop timeout. Cancellation-
resistant leftovers are unconfirmed, never successful. The dedicated CLI exits
without unbounded implicit asyncio shutdown; systemd handles remaining cgroup
processes. Docker resources can still require reconciliation, so failure and
interrupt state are retained.

Only after every accepted runtime outcome, database-handle closure, no remaining
runtime tasks and zero native containers does the controller fsync the result and
remove its own active marker. Consumed and progress/result records remain. There
is no automatic retry, marker reset, broad cleanup or decommission. Reconcile exact
processes, containers, starts, grants, spools and evidence before separately
approving removal of retained active state.

Read named progress without starting or modifying anything:

```text
<approved-platform-python> -B -I -m ditto.coding_bounded_rollout \
  --inspect --approval-sha256 <exact-approved-SHA>
```

Status reports `process_liveness_verified=false`: a marker or lock cannot prove
a process is still running. Unknown record fields are not forwarded. Inspect the
actual service/process state before liveness or recovery decisions. Preserve
status and evidence on approved private paths. Local source-free status is not
remote Backroom host telemetry or a signed operational attestation.

Success remains `shadow_only=true`, `weight_eligible=false`. No receipt or test
result authorizes competitive emissions.
