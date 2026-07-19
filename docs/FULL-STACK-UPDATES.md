# Full-stack updater: trust and transaction model

Reference for the managed stack updater. The commands live in
[VALIDATOR.md](VALIDATOR.md#automatic-full-stack-updates-recommended); this
explains what the updater guarantees and where its boundaries are.

## Trust model

A promoted stack descriptor binds all six services (`ditto-subnet`,
`dittobench-api`, `model-relay`, `sandbox-docker`, `pylon`, `ollama`) to
immutable manifest digests. `ghcr.io/ditto-assistant/ditto-subnet-stack:compat-2`
is only a discovery tag: the updater resolves it once to a content-addressed
descriptor, verifies that digest's keyless Cosign certificate against this
repository's `release.yml` identity on `main`, and never uses the mutable tag
again during a transaction. The descriptor contains its own Compose file and
manifest; the updater rejects mutable component references, unexpected
repositories, missing or extra services, build contexts, and mismatched release
metadata — all before draining the validator.

The host-side launcher, systemd unit, Docker Engine, wallet directory, and
`.env` are deliberately outside the signed bundle so a candidate workload can
never replace its own trust anchor. Any change to the updater protocol, Compose
schema, descriptor format, compatibility epoch, or heartbeat protocol is
therefore a **supervised migration**, never an automatic update. Within those
gates, compatible patch and minor releases in the same major version update
automatically.

## First adoption

First adoption is always supervised: disable existing updater timers, fast-
forward this checkout to the reviewed release that published the descriptor
(keeping your existing `.env`), require a clean checkout and six healthy
services, then run `migrate <descriptor-digest>` — or `adopt
<descriptor-digest>` only when all six running services already match the
descriptor. Both require a fresh platform-accepted heartbeat and record the
installed stack atomically. Never substitute a mutable tag for the digest, and
enable the timer only after `status` looks right. A validator-only
`managed-image.env` is not a full-stack adoption record.

If `migrate` fails before a drain is reported and `status` shows no
transaction, the old stack was not touched: leave it running and capture the
release, digest, and updater log. If `status` reports a transaction, keep the
timers disabled and use `recover`.

## Transaction guarantees

Each automatic update: resolves the channel to a digest, validates the
descriptor, pre-pulls every component without touching the running stack,
drains the validator via `USR1` (an active lease finishes first, and the
platform must accept the drained heartbeat), writes a durable journal,
reconciles the full candidate stack, requires healthy services plus a fresh
platform-accepted heartbeat from the candidate validator, records the
descriptor atomically, and resumes lease intake via `USR2`.

Any failure rolls **all six services** back to the retained previous
descriptor and resumes the old validator only once that stack is healthy; the
failed digest stays suppressed until the channel advances. Crashes recover
from the journal, and once `USR2` may have fired the updater refuses to
recreate the validator without a new drained acknowledgement. Named volumes
and the wallet bind survive the transaction; unrelated containers are never
touched; no managed update ever builds from source.

## Operations

- `status` is read-only and network-free; `recover` and `rollback` are
  supervised — keep the timer disabled while running them.
- Keep enough free disk for two stacks concurrently, and never prune images
  referenced by the installed, previous, or pending descriptors.
- Source-built installs are never silently converted, and a registry outage
  leaves the current healthy stack running — the launcher never falls back to
  building source.
- For managed installs the release renderer injects the signed stack identity
  (version, revisions, protocols, component digests) into the validator
  environment; `.env` cannot override it, and benchmark-v3 support is accepted
  only when the scorer's runtime identity matches. Anything else stays
  conservatively benchmark v2.
- Pylon is pinned to an amd64-only runtime; arm64 hosts need the documented
  binfmt/QEMU support.
