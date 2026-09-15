# Default-off native-v2 public control signer startup

Platform can now provision the existing `/api/v1/validator/coding-hosted/control`
status/result signer through its ordinary API lifespan. This does not create an
assignment, register a release, start a worker, unwrap a private object, or enable
scoring. Existing admission, chain-permit, replay, durable evidence and result
acknowledgement checks remain the authority for each request.

## Explicit configuration

`ApiServerConfig.coding_hosted_signer` is an internal frozen dataclass, not a
public wire model. Its environment inputs are:

| Setting | Meaning |
| --- | --- |
| `DITTO_CODING_HOSTED_CONTROL_ENABLED` | Default `false`; only `true`/`1` enables. `false`/`0` disables; other values fail config validation. |
| `DITTO_CODING_HOSTED_SIGNER_SEED_FILE` | Absolute path to a separately provisioned raw 32-byte SR25519 seed. No hex text, mnemonic, seed URI or wallet fallback. |
| `DITTO_CODING_HOSTED_SIGNER_HOTKEY` | Independently approved expected Platform SS58 address; derived identity must match exactly. |

The seed must be a nonzero, single-link, owner-owned regular file, mode `0600`,
in a canonical owner-owned `0700` directory. Symlinks, special files, unsafe
ancestors, oversized/truncated files and mismatched hotkeys fail closed. Reuse
the established native runtime protected-file checks. Files and configuration
must remain operator-controlled and unchanged during startup; same-UID/root
compromise is outside these checks.

Disabled startup does not inspect staged paths. A relay process never reads
the seed, even when it shares an enabled, structurally valid configuration.
There is no automatic lookup of a validator, screener, payment or curator wallet.
Operators must provision a dedicated Platform control key, keep it off candidate
and validator hosts/images, and distribute the approved verification hotkey
through their independent validator trust configuration. No key is generated,
uploaded or enabled by this change. Key rotation needs coordinated trust-config
updates and process restart; there is no hotkey fallback.

### Host wiring

The `platform_app` Ansible role renders these settings, default off. When
enabled it renders the fixed seed path
`/etc/ditto-platform/coding-hosted-signer/seed` and the reviewed public hotkey.
Enabling also requires two reviewed isolation switches. The first runs
`ditto-api` as the dedicated `ditto-api` user from sealed root-owned releases
through `ditto-platform-api.service`. The second moves Pylon to a root-owned
unit so `deploy` leaves the `docker` group; because running processes keep old
groups, the converge guard, the release installer and `update.sh` also check the
live host and refuse while any `deploy` process can still reach the Docker
daemon.

Right after the role's preflight, the converge stat-verifies without reading
that the seed and its `0700` directory are owned by `ditto-api`. The metadata
entry point `python -m ditto.api_server.coding_hosted_signer_preflight --check-metadata`
runs as `ditto-api`, from the sealed release, before `scripts/update.sh`
activates it and again in the unit's launcher before every start. It applies `read_private`'s
location and file checks through `lstat` and never opens the seed, so only
startup detects a hotkey mismatch. `update.sh` itself never checks or opens the
seed, and it refuses to deploy an enabled signer while `ditto-api` would still
run under `deploy`'s pm2.

`read_private` compares owners with the process's effective UID, so the relays,
the image-cleanup job, deploy tooling and anyone acting as `deploy` cannot use
the placement. Root still can. The implemented design, its residual risks, the
fixed placement, enabling order, rotation and revocation are in
`infra/docs/coding-hosted-control-signer-v2.md`. Seed creation, backup,
placement and production activation are a separate protected ceremony. The
online control-signer seed and the offline curator key stay completely separate,
and neither ever enters CI, Git, Telegram, workflow artifacts, command arguments
or logs.

## Signing and lifecycle boundary

The loader checks the actual derived key and a sign/verify startup challenge
before dependency startup. Its public wrapper signs only canonical, bounded
native-v2 status/result envelopes that name its configured Platform identity.
It rejects request envelopes, arbitrary messages, unknown fields, noncanonical
bytes and altered shadow/weight gates. This is not a generic signing service.
The existing endpoint remains responsible for database-bound authority and
post-signature verification; a signature alone never proves correct execution.

`app.state.coding_hosted_control` starts absent and is installed only after all
ordinary API dependencies have started successfully. Startup failure unwinds
the signer alongside the other dependencies. Lifespan exit clears application
state and closes the wrapper; even a retained wrapper refuses later signatures.
Secret paths/seed bytes are absent from configuration/signer representations
and fixed signer errors. Releasing references and clearing the temporary buffer
does **not** guarantee memory zeroization in Python or native crypto libraries;
the Platform process and its host administrators remain trusted key holders.

No key-custody helper, host/package provisioning, private evaluation, candidate
egress exception, release registration, scoring, weight or emission gate is
enabled. Hardware/remote signing custody, if required, needs its own reviewed
adapter and grant policy rather than a blind generic signing subprocess.

## Validation

Tests use an explicitly public synthetic seed. They cover disabled/relay no-read
behavior, strict activation/config parsing, protected file rejection, key binding,
real status/result signatures, signing-domain restrictions, startup failure,
normal lifecycle cleanup, and real HTTP admission against PostgreSQL through the
startup-loaded signer. External chain/provider/storage services remain mocked;
these tests do not establish a deployed private shadow canary.
