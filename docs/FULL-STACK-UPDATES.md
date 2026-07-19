# Immutable full-stack validator updates

> **Do not adopt v0.10.0.** Its bootstrap validates a descriptor before moving
> it into updater-owned canonical state, so the hardened Compose wrapper rejects
> every first migration before the validator drains. Use v0.10.1 or newer.

The production GHCR channel is a release of the complete SN118 validator
stack, not just the Python validator process. Each promoted stack descriptor
binds these six services to immutable manifest digests:

- `ditto-subnet`
- `dittobench-api`
- `model-relay`
- `sandbox-docker`
- `pylon`
- `ollama`

The first four images are built and smoke-tested for Linux amd64 and arm64 from
the exact release sources. Pylon and Ollama remain reviewed third-party digest
pins. They are still part of the descriptor and therefore part of every
transaction and rollback.

## Trust and bootstrap model

`ghcr.io/ditto-assistant/ditto-subnet-stack:compat-2` is only a discovery tag.
The updater resolves it to a content-addressed descriptor image and never uses
the mutable tag again during a transaction. It verifies the exact digest's
keyless Cosign certificate against this repository's `release.yml` identity on
`main`. The descriptor image contains its own Compose file and manifest. Its
digest binds both files, and the manifest and Compose file independently
contain the exact component digests.
The updater rejects mutable component references, unexpected repositories,
missing or extra services, build contexts, and mismatched release metadata
before it drains the validator.

The host-side launcher, systemd unit, Docker Engine, wallet directory, and
`.env` are deliberately outside the release bundle. This avoids allowing a
candidate workload to replace its own trust anchor. A new updater protocol,
Compose schema, required host capability, wallet layout, or compatibility
epoch is therefore a **supervised migration**.

An existing validator-only updater cannot update its own host scripts or stale
Compose checkout. Moving to full-stack managed mode is also a one-time
supervised migration. Do not treat a validator-only `managed-image.env` as a
full-stack adoption record.

## Supervised first adoption

1. Disable and stop any existing validator updater timer.
2. Fast-forward the repository to a reviewed release containing the full-stack
   launcher and reinstall the systemd unit.
3. Resolve the stack channel to an immutable descriptor digest.
4. Run `migrate <descriptor-digest>` to drain and replace an existing source or
   validator-only stack. Use `adopt <descriptor-digest>` only when all six
   running services already match the descriptor. Both paths require a fresh
   platform-accepted validator heartbeat and record the installed stack
   atomically.
5. Check `status`, then enable the timer.

Use the exact commands in [VALIDATOR.md](VALIDATOR.md) and run
`scripts/install-validator-stack-auto-update.sh` only after migration. Never substitute a
mutable tag for the adoption digest.

### Preflight and failure boundary

The host launcher is outside the signed bundle and cannot update itself. Before
`migrate`, use the exact reviewed release that published the descriptor, keep
the existing `.env` in that checkout, and verify both updater timers are off:

```sh
git status --short
git describe --tags --always
test -f .env
systemctl is-active ditto-validator-auto-update.timer || true
systemctl is-active ditto-validator-stack-auto-update.timer || true
./scripts/validator-compose.sh ps
```

Require a clean checkout and six healthy services. Do not copy `.env.example`,
use a second checkout, or install the new timer before migration succeeds.

Descriptor, digest, and Compose validation happen before drain. If migration
fails before a drain is reported and `status` shows no transaction, the old
stack was not replaced; leave healthy services running and capture the release,
descriptor digest, and updater log:

```sh
./scripts/validator-stack-auto-update.sh status
./scripts/validator-compose.sh ps
systemctl status ditto-validator-auto-update.timer \
  ditto-validator-stack-auto-update.timer --no-pager || true
```

Never edit extracted state or bypass descriptor checks. If `status` reports a
transaction, keep both timers disabled and follow `recover`; a
`migration_started` transaction deliberately requires supervised repair.

Adoption does not interrupt active work. If the currently running services do
not already match the descriptor, perform the migration in a maintenance
window after the validator has cooperatively drained.

## Automatic transaction

For a compatible candidate, the launcher:

1. resolves the channel to an immutable descriptor digest;
2. extracts and validates the release-owned manifest and Compose file;
3. pre-pulls and verifies every exact component digest without changing the
   running stack;
4. renders the candidate Compose configuration with builds disabled;
5. asks the validator to drain through `USR1` and waits for any active lease to
   finish and for the platform to accept the drained heartbeat;
6. records a durable transaction journal;
7. reconciles the complete candidate dependency graph and starts its validator
   quiescent with a fresh bootstrap token;
8. checks every service and requires a fresh platform-accepted heartbeat from
   the exact candidate validator;
9. atomically records the installed descriptor; and
10. resumes lease intake through `USR2`.

If any pull, metadata, Compose, health, or heartbeat check fails, the updater
reconciles **all six services** from the retained previous descriptor. It
resumes the old validator only after the old stack is healthy. The failed
descriptor digest remains suppressed until the channel advances. A crash or
termination is recovered from the durable phase journal; once `USR2` may have
taken effect, the updater refuses to recreate the validator without a new
drained acknowledgement.

No managed update builds a Git context or local source tree. Named data volumes
and the wallet bind remain stable across the transaction. The updater never
mutates unrelated containers.

## Compatibility policy

Automatic updates are allowed only when all of these remain compatible:

- compatibility epoch;
- updater protocol;
- Compose schema;
- stack descriptor format; and
- validator heartbeat protocol policy.

Within those gates, a newer release in the same major version may update the
complete stack, including a minor release whose descriptor says it is an
ordinary compatible rollout. A major release, schema/protocol change, or
explicit migration marker requires supervised adoption. This is intentionally
stricter than trusting semantic version numbers alone.

## Gradual fleet rollout and screened images

The platform and screeners can deploy before validators because legacy
validators ignore the additional artifact fields and retain source-build
fallback. Full-stack managed adoption is also per-validator; mixed source-built,
validator-only, and full-stack-managed operators can coexist during migration.

Use these rollout stages:

1. deploy platform support;
2. deploy screeners and observe verified image coverage;
3. adopt the full-stack updater across validators;
4. deploy validators in **prefer image** mode while coverage is measured; and
5. enable **require image** only after coverage and rollback telemetry meet the
   release gate.

The currently proposed prebuilt-image subnet change hardcodes
`DITTOBENCH_REQUIRE_SCREENED_IMAGE=1`. That release skips the prefer stage for
any operator who pulls and rebuilds it. This updater does not weaken or hide
that setting: it reports the installed stack descriptor and changes all scorer
and sandbox components together. Before promoting that release to the managed
channel, either verify complete artifact coverage and accept the strict cutover,
or change the prebuilt-image release to make the prefer-to-require transition
explicit.

## Source-built fallback

Source operators can continue the manual `git pull` and
`validator-compose.sh up -d --build` workflow while automatic updates are
disabled. The managed launcher never silently converts a source-built install
and never falls back to building source if a registry release is unavailable.
Registry outage leaves the current healthy stack running.

Capability heartbeats report source installs conservatively as `source` with
no managed release identity. For managed installs, the release renderer places
the signed manifest's version, revisions, Compose/update protocols, and all six
exact component digests directly into the validator environment. The host
wrapper supplies only the immutable descriptor digest, after matching the
root-owned extracted descriptor against installed or in-flight transaction
state. Operator `.env` values cannot override these claims, and the validator
does not receive the Docker socket.

The renderer also supplies `DITTOBENCH_SOFTWARE_VERSION` and
`DITTOBENCH_SOURCE_SHA` to the scorer as descriptor-controlled literals. The
validator accepts benchmark-v3 support only when the scorer capability
response's runtime identity matches the signed stack identity. The endpoint
reports only public release metadata and protocol numbers, so it needs no
operator secret; a shared bearer token held by the scorer could not authenticate
that scorer to the validator. An older scorer, an unreachable endpoint, a
malformed response, or an identity mismatch remains conservatively benchmark
v2. The scorer port is not published on the host, and neither service receives
the host Docker socket. This keeps old/new scorer-validator combinations safe
without an `.env` cutover.

## Operations

`status` is read-only and network-free. It reports the installed descriptor,
version, revision, compatibility fields, transaction phase, and all six exact
component references. Use `recover` after an interrupted transaction and
`rollback` for a supervised whole-stack rollback. Keep the updater disabled
while performing either manual operation.

Retain enough free disk for the candidate and previous stack concurrently.
Images referenced by the installed, previous, or pending descriptors must not
be pruned. Cleanup may remove older unreferenced releases only after a complete
transaction has committed.

Before the first release, the four new first-party sidecar/descriptor packages
must be created by the release workflow and configured for validator-host read
access. If operators are expected to pull anonymously, verify an unauthenticated
exact-digest pull for every package before advertising the channel. Otherwise,
provision a durable least-privilege GHCR read credential. A successful Actions
push does not by itself prove that validator hosts can pull the packages.

Pylon remains pinned to an amd64-only runtime in the production Compose model.
Arm64 hosts therefore still need the documented binfmt/QEMU support even though
all Ditto-owned images are published for both amd64 and arm64.
