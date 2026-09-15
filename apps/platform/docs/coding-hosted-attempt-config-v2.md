# Hosted-v2 per-attempt runtime config

`ditto.coding_hosted_attempt_config` writes the one
[`HostedPlatformRuntimeInput`](coding-hosted-platform-runtime-v2.md) document
for a single admitted hosted-v2 assignment on `ditto-coding-hosted-v2`. It
derives authority from Platform and selects staged public authorities by
operator pins that must equal the assignment's own digests. It references
existing owner-only credential files by fixed path. It is default-off and
explicit. It never starts a unit, creates or admits an assignment, issues a
grant, contacts a provider or writes to PostgreSQL. The runtime rechecks
everything under row locks at start.

```text
runuser -u ditto-coding-hosted -- /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  /opt/ditto-coding-hosted/<rev>/apps/platform/.venv/bin/python -B -I -m ditto.coding_hosted_attempt_config \
  --materialize-attempt-config --evaluation-id <uuid> --runtime-revision <rev> \
  --assignment-sha256 <hex> --execution-profile-sha256 <hex> --grading-profile-sha256 <hex> \
  --inference-policy-sha256 <hex> --budget-profile-sha256 <hex> \
  --probe-receipt-sha256 <hex> --evidence-wrapping-key-sha256 <hex>
```

The default-off `coding_hosted_attempt_config` role
(`infra/ansible/playbooks/gcp-coding-hosted-attempt-config.yml`) wraps this
command. See [the wrapper](#wrapper-role).

## Inputs

Only these pins are accepted. There is no path, host value or output option.

| Pin | Source | Must equal |
| --- | --- | --- |
| `--evaluation-id` | Admin create response | The assignment row |
| `--runtime-revision` | The installed runtime revision the worker unit runs (`coding_hosted_worker_python`) | The revision of the interpreter running the materializer |
| `--assignment-sha256` | The same response | The recomputed authority digest and the stored row |
| `--execution-profile-sha256`, `--grading-profile-sha256`, `--inference-policy-sha256` | Reviewed input file SHA-256 | The assignment's own digests, compared before any input is selected |
| `--budget-profile-sha256` | Reviewed input file SHA-256 | The verified policy's `runtime_profile_sha256`, compared before the budget is selected |
| `--probe-receipt-sha256` | Receipt file SHA-256 from the `coding-hippius-probe.yml` run summary | The staged receipt |
| `--evidence-wrapping-key-sha256` | The independently reviewed SPKI fingerprint of the evidence public key. Nothing in Platform or on the host records it | The key's fingerprint |

The config records the worker and interpreter paths of the revision that ran
the materializer. The revision pin keeps them tied to the revision the worker
unit starts. The receipt records it as `runtime_revision`.

## Host layout

`<home>` is `/var/lib/ditto-coding-hosted`, the worker account's home. Every
worker file is a regular single-link `0600` file owned by the worker, below
canonical `0700` directories. These are the runtime's own rules. `<inputs>` is
`/var/lib/ditto-coding-hosted-attempt-inputs`. It is a root-owned sibling of the
home, not below it.

| Path | Class | Provisioned by | Checked here |
| --- | --- | --- | --- |
| `<home>/private/postgres-environment.json` | Secret | #1888 role | Metadata. Parsed in memory only to open the read-only connection |
| `<home>/private/hippius-environment.json` | Secret | Owner (not yet automated) | Metadata. Parsed in memory, as the runtime parses it, to derive the storage authorities. Only the two digests are kept |
| `<home>/private/image-storage.json` | Secret | Owner (not yet automated) | Metadata only; never opened |
| `<home>/private/provider-key` | Secret | Owner (not yet automated) | Metadata only; never opened |
| `<home>/custody/unwrap` | Helper | #1860 custody install | Protected helper, distinct from the worker |
| `<inputs>/execution-profile-<sha>.json` | Authority | Wrapper role, root `0440` | Pin equals the assignment, SHA, canonical, patch bound equals the task |
| `<inputs>/grading-profile-<sha>.json` | Authority | Wrapper role, root `0440` | Pin equals the assignment, SHA, canonical |
| `<inputs>/inference-policy-<sha>.json` | Authority | Wrapper role, root `0440` | Pin equals the assignment, canonical policy, digest |
| `<inputs>/budget-profile-<sha>.json` | Authority | Wrapper role, root `0440` | Pin equals the policy's `runtime_profile_sha256`; bound, valid now and through the deadline |
| `<inputs>/probe-receipt-<sha>.json` | Authority | Wrapper role, root `0440` | Pinned SHA, at most 64 KiB (the runtime's read bound), canonical and ready, fresh through the last publication, storage authorities equal the release and Hippius environment |
| `<home>/authority/evidence-public.pem` | Authority | Owner (not yet automated) | RSA key fingerprint equals the pin |
| `<home>/release/<registration_sha256>/{transport-manifest,payload-authority,publication-receipt}.json`, `curator-public.pem` | Authority | Owner (not yet automated) | Full network-free release verification and catalog membership for the task |
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.json` | Host record | #1899 role, root `0444` | Closed record; supplies `router_listen`, `egress_proxy`, `egress_network`, candidate UID/GID |
| `/usr/bin/docker`, `/run/ditto-coding-hosted/docker.sock` | Host | Daemon role | Root-owned, not writable, executable by the worker (`access(X_OK)`); worker-owned `0600` socket |
| `/opt/ditto-coding-hosted/<rev>/` | Runtime | Runtime install | The running interpreter's prefix, equal to the pin; installed worker admitted once |

Staged inputs must be owned by root, not group- or world-writable and single
link, below root-owned ancestors. The materializer reads them whole and copies
the verified bytes into the worker-owned attempt directory. File bounds are the
runtime's own constants from `coding_hosted_runtime_config`.

`executor_repository` is the single `coding-runtime.invalid/<language>/runtime`
in the isolated daemon whose `RepoDigests` contain both profiles'
`image_digest`. One read-only `docker image inspect` covers every candidate
reference under one timeout. Zero or several matches are refused. Seccomp and
AppArmor stay empty, which keeps Docker's default seccomp.

## Authority source

Authority is read directly from Platform PostgreSQL. It uses the worker's own
environment file in one `REPEATABLE READ, READ ONLY` transaction, without
locks. PostgreSQL rejects any write or `FOR UPDATE` in that transaction. No API
exposes private assignment authority, and reaching it through the admin API
would put a stronger credential on the host. The runtime already holds this
database credential. The snapshot is read again just before the write and must
be identical.

The snapshot applies the runtime's own lock-agnostic predicates. `inspect_launch`
and `_lock_authorities` apply the same predicates to rows read for update:

- `authority_bound`: the stored projection, recomputed digest, stored digest and
  every pin agree, including every denormalized column and the deadline.
- `launch_pending`: the assignment is admitted, unstarted and has no worker. It
  has at most 3600 seconds left by the database clock. The materializer also
  requires at least 300.
- `release_available`: the release row matches the registration, is
  shadow-only and has no quarantine or retirement event.
- `artifact_available` and `screened_image_ready`: the agent still has the
  artifact and screened image, a scoreable status, a verified image and a
  current screening policy.
- `task_open`: the private task exists, is not closed or frozen, and its
  selection matches the assignment.

## Storage authorities

At start, the runtime requires the probe receipt's
`private_input_authority_sha256` and `sealed_evidence_authority_sha256` to equal
the authorities derived from its Hippius environment. It checks this only after
writing `platform-consumed`, so a mismatch there burns the attempt. The
materializer runs the same check before any write. It parses the Hippius
environment with the runtime's `hippius_configs`, keeps only the two digests,
and requires the reader authority to equal the probe and the release.

Both digests depend on non-secret fields (endpoint, region, buckets and access
key IDs), but those fields share the owner-only file with the secret keys. The
materializer runs as the worker, which already owns that file, and never writes
or prints its contents. The alternatives were weaker. An operator pin checked
against the receipt cannot detect host-file drift. Calling `load_runtime_config`
needs the attempt directory to exist first, so a refusal would leave a reserved
attempt behind.

## Probe freshness

Every evidence publisher (`_check_probe` in `coding_hosted_evidence`, `_fresh` in
the authoring and grading publishers) requires a probe receipt younger than
`PROBE_RECEIPT_MAX_AGE_SECONDS` (24 hours) at the moment it publishes. The
latest publication is derived from the runtime's constants:

- `WORKER_FINALIZATION_SECONDS` (3600): the Go child's timeout past the
  deadline.
- `WORKER_SHUTDOWN_GRACE_SECONDS` (1800): its SIGTERM grace.
- Two `SHUTDOWN_OPERATION_SECONDS` (30 each): the close and abort drain of the
  one bound attempt in `HostedAuthoringControl.shutdown`.

That gives `EVIDENCE_PUBLICATION_SECONDS` of 5460. The materializer requires
`checked_at + 86400 > deadline + 5460` and a receipt that is not future-dated.
On success, the 20-second result check runs instead of the grace, so it never
extends the bound. Process start and handler cancellation add only seconds and
have no constant of their own.

## Admission ordering

A config is written only for an admitted assignment. The runtime refuses an
unadmitted one at start, and the materializer generates the worker UUID that
custody `prepare` needs. Materializing after validator admission keeps one
order. Nothing is staged ahead of the validator's signed consent.

## Refusals

Each is a fixed stage on stderr (`hosted attempt config refused: <stage>`),
without a path or value. All refusals happen before any write:

- Malformed pins, or a runtime revision other than the running interpreter's.
- The wrong host, account or interpreter prefix, or an unsafe installed worker.
- Unsafe home or attempts directories, unwrap helper, Docker executable (not
  root-owned, writable or not executable by the worker) or socket.
- A host-prerequisites record that is missing, writable or not closed, or one
  with public, loopback, mismatched or `ditto-job-` values.
- A live `ditto-coding-hosted-worker.service` or `ditto-coding-custody@*.service`,
  or an existing custody socket. Only `inactive` or `failed` units pass. A
  short or unparseable `systemctl` line is refused. This is the one liveness
  rule; the wrapper does not duplicate it.
- Credential files with the wrong owner or mode, a symlink, extra links, a
  shared directory or a missing file.
- A Hippius environment the runtime would refuse.
- An unavailable assignment, or an authority or pin mismatch. Also an assignment
  that is not admitted, already started, expired or near expiry.
- A quarantined or retired release, an unavailable artifact, or a closed, frozen
  or mismatched task.
- `<home>/attempts/<attempt_id>` already exists, reported as "already consumed"
  when a runtime or Go consumed marker is present.
- A staged input that is not root-owned, is writable by group or others, or has
  extra links.
- A missing, mismatched or non-canonical execution or grading profile, or a
  patch bound that differs from the task.
- A policy digest mismatch or unbound policy. A budget pin other than the
  policy's, or a budget that does not bind, is not yet valid or expires before
  the deadline.
- A probe receipt with the wrong SHA, over 64 KiB, not ready or not canonical.
  Also one that is future-dated or not fresh through the last possible
  publication.
- A private-input authority that differs among the probe, the release and the
  Hippius environment, or a sealed-evidence authority that differs between the
  probe and the Hippius environment.
- An evidence key fingerprint that differs from the pin.
- Release authorities that fail verification or do not contain the task index.
- A missing or ambiguous executor image.
- Authority that changed between the two snapshots.

## Output

The worker creates `<home>/attempts` if missing, then writes the config under
`<home>/attempts/<attempt_id>/`:

```text
runtime.json   config, published by no-clobber link after fsync
authority/     copies of the verified profile, policy, budget, probe and key bytes
runtime/       empty runtime_root
unwrap/        empty unwrap_work_root
```

The directory is created exclusively, so a rerun for the same attempt is
refused. A crash mid-write leaves a partial directory that must be reviewed and
is never reused. Copying authorities pins the verified bytes against later
staging changes; credentials and release authorities are referenced in place.
Stdout is one JSON receipt: identities, deadline, runtime revision, authority
digests, the config path and SHA-256, the runtime root, `admitted=true`,
`services_started=false`, `shadow_only=true` and `weight_eligible=false`. The
catalog index and executor language are not printed.

## Per-attempt sequence

Every step fits inside the assignment's deadline, which is at most one hour
after creation:

1. The admin creates the assignment (#1823). The operator pins
   `evaluation_id` and `assignment_sha256`.
2. The validator control command sends the signed evaluate, and Platform admits
   the assignment.
3. The operator runs this materializer and records `worker_id`, the config path
   and `config_sha256`.
4. Custody runs `prepare <worker_id>` and `systemctl start ditto-coding-custody@<worker_id>` (#1860).
5. The connectivity role installs a profile issued within five minutes of
   start, with `coding_hosted_worker_config` set to the config path and the same
   `coding_hosted_worker_python` as step 3. The egress proxy starts (#1899).
6. `systemctl start ditto-coding-hosted-worker.service`. The runtime reloads and
   rechecks under locks, writes `platform-consumed`, and the Go helper commits
   the irreversible start.

If the deadline passes before step 6, discard custody and create a new
assignment. The old attempt directory is retained, not reused.

## Wrapper role

`coding_hosted_attempt_config` defaults off. It requires:

- the exact confirmation
  `MATERIALIZE HOSTED CODING ATTEMPT CONFIG <evaluation_id> <assignment_sha256>`;
- an installed runtime revision, whose interpreter path must equal the
  `coding_hosted_worker_python` passed for the connectivity role;
- the evaluation, assignment and evidence-key pins; and
- exactly five nonsecret inputs (`probe_receipt`, `execution_profile`,
  `grading_profile`, `inference_policy`, `budget_profile`), each an absolute
  controller path plus a reviewed file SHA-256.

It refuses check mode, controller files that differ from their SHA, an
unprotected interpreter, and an unsafe existing staging directory. Root writes
only into `<inputs>`, a root-owned `0750` directory whose ancestors are all
root-owned. It never writes into or changes directory below the worker-owned
home, so no root write follows a worker-controlled path. Inputs are staged
root-owned `0440` (group `ditto-coding-hosted`) under their digest names,
without following links or replacing existing bytes. Owner, group, mode, link
count and SHA are then verified on the host. The role runs the materializer as
the worker with every reviewed SHA-256 as a pin. It prints only the receipt.
The materializer, not the role, refuses live worker or custody units. The role
takes no credential and starts nothing.

## Validation

From the repository root:

```bash
(cd apps/platform && uv run pytest -q -n 0 ditto/tests/api_server/test_coding_hosted_attempt_config.py)
uv run pytest -q ditto/tests/test_coding_hosted_attempt_config_role.py
(cd infra/ansible && uvx --from ansible-core==2.21.2 ansible-playbook --syntax-check -i inventory/validator-static.yml playbooks/gcp-coding-hosted-attempt-config.yml)
(cd infra/ansible && uvx --from ansible-core==2.21.2 ansible-playbook --check -i localhost, tests/coding-hosted-attempt-config.yml)
```

The Platform tests use real PostgreSQL and a synthetic signed release. The
happy path produces a config that `load_runtime_config` accepts, and the
runtime's locked `inspect_launch` accepts it unchanged. The storage authorities
it derives equal the probe's. Image storage and provider credentials are never
opened, and a rerun is refused. Refusal tests also assert that the runtime's
locked inspection refuses the same assignment state. The probe freshness bound
is tested at its edge. The key guards were mutation-checked. Nothing here proves
a live host, a staged credential or a canary.
