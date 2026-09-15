# Native-v2 custody service lifecycle

This layer installs the native-v2 custody service on `ditto-coding-hosted-v2`
without starting it, and provides the fixed per-run lifecycle around it. It does
not bootstrap or rotate the RSA key (see `coding-hosted-custody-key-v2.md`),
materialize PostgreSQL credentials, write the worker's runtime configuration,
start a worker, issue an assignment, or enable scoring, weights or emissions.
The service itself is `ditto.coding_private_v2_custody --serve-custody`,
documented in `apps/platform/docs/coding-private-v2-custody.md`.

## Stable install, once per runtime revision and release

The `coding_hosted_custody_service` role defaults off. It requires:

- the exact confirmation `INSTALL NATIVE CODING CUSTODY SERVICE`;
- the reviewed 40-character source revision;
- an already installed `coding_hosted_runtime` revision;
- the Platform PostgreSQL private IP from the reviewed
  `coding_hosted_postgres_access` output;
- the registered release ID and its reader authority SHA-256; and
- five release inputs (registration, transport manifest, payload authority,
  publication receipt, curator public key), each as a controller path plus a
  reviewed SHA-256.

It refuses to run while any custody instance is live or a per-run configuration
is left over. It installs:

| Path | Owner, mode | Purpose |
|---|---|---|
| `/var/lib/ditto-coding-custody/release/<release>/` | custodian, `0700`/`0600` | Release inputs, digest-verified on the controller and again on the host. Existing different bytes are never replaced; reconcile explicitly |
| `/var/lib/ditto-coding-custody/runs/` | custodian, `0700` | Empty; holds at most one per-run configuration |
| `/var/lib/ditto-coding-custody/private/` | custodian, `0700` | Reserved for the custodian's own PostgreSQL environment copy. It is materialized separately and never by this role |
| `/etc/ditto-coding-custody/base.json` | root, `0600` | Every stable service field. It has no `worker_id` |
| `/usr/local/lib/ditto-coding-custody/custody-run.py` | root, `0444` | Fixed per-run lifecycle helper |
| `/etc/systemd/system/ditto-coding-custody@.service` | root, `0644` | Locked unit template. There is no `[Install]` section and it is never enabled or started |
| `/var/lib/ditto-coding-hosted/custody/unwrap` | worker, `0500` in `0700` | The worker's argument-free unwrap proxy (`--proxy-once`) pinned to the socket and custodian UID. It passes the worker's `protected_helper` admission |

The RSA key stays where the bootstrap put it. Changing the runtime revision, the
PostgreSQL IP or the release means rerunning this install between runs.

## Per run

Each hosted run gets one owner-only configuration, bound to the exact assigned
worker UUID, and one transient unit instance whose name is that UUID. The worker
UUID comes only from the root-run argv and the instance name, never from request
data. The service's grant store also refuses any grant whose assignment belongs
to a different worker.

```text
sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-custody/custody-run.py prepare <worker-uuid>
sudo systemctl start ditto-coding-custody@<worker-uuid>.service
#   start the worker, whose private config uses the same worker_id and
#   unwrap_executable=/var/lib/ditto-coding-hosted/custody/unwrap
sudo systemctl stop ditto-coding-custody@<worker-uuid>.service
```

- `prepare` accepts only a canonical non-zero lowercase UUID. It refuses when
  another run exists, any custody instance is live, or the socket exists. It
  writes `base.config + worker_id` exclusively without following links, and
  prints a receipt with no paths or private values.
- `ExecStartPre` runs `check` as root before the service starts. The only run
  present must be this UUID, and the configuration must be custodian-owned,
  `0600`, single-link, byte-identical to the canonical base plus this worker,
  with no socket and no other live instance.
- The service runs as `ditto-coding-custody`:
  - It uses the installed runtime interpreter.
  - It has a `RuntimeDirectory` socket directory of exactly `0755`.
  - It has no capabilities, runs under a strict read-only filesystem, and has
    no access to worker or image paths.
  - Its only address families are `AF_UNIX` and `AF_INET`, and its only IP
    peer is the PostgreSQL `/32`. The environment's `POSTGRES_HOST` must be
    that IP literal.
  - Its output is discarded.
  - `PrivateUsers` is deliberately absent, because `SO_PEERCRED` must see the
    real worker UID.
- `ExecStopPost` runs `release`. However the instance ends (stop, crash,
  failed `check`, or the two-hour `RuntimeMaxSec` backstop), the per-run
  configuration is removed and systemd removes the socket directory, so the
  service's own "never unlink an existing socket" rule cannot block the next
  run.
- `discard <worker-uuid>` removes a prepared configuration that was never
  started. It requires every custody instance to be stopped.

Grants expire within an hour of launch, so an instance cannot unwrap past its
assignment even before it is stopped. One instance serves one worker UUID and
the socket path is fixed. This lifecycle therefore supports one hosted worker
at a time, not bounded-rollout cohorts with distinct worker UUIDs.

Running these per-run operations from the protected `coding-hosted-operate`
workflow, and materializing the custodian's and worker's PostgreSQL
environment copies, are separate reviewed changes.

## Validation

`ditto/tests/test_coding_hosted_custody_service.py` exercises the lifecycle
helper against a synthetic layout. It covers:

- UUID canonicality;
- exclusive preparation;
- instance binding;
- config, mode, symlink and socket drift;
- release and discard;
- base validation;
- that the lifecycle's field set equals exactly what `serve()` reads.

It also pins the unit template, proxy and install tasks.

`infra/ansible/tests/coding-hosted-custody-service.yml` proves the role is off by
default. It then renders the base, unit and proxy from synthetic authorities
and checks they agree on runtime, socket, database IP and custodian UID. None
of this starts a unit, contacts PostgreSQL or reads key material. A live start
still requires the separately qualified host, a materialized PostgreSQL
environment and an issued assignment.
