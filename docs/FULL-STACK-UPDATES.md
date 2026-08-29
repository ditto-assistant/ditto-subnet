# Full-stack updater: trust and transaction model

Reference for the managed stack updater. The commands live in
[VALIDATOR.md](VALIDATOR.md#automatic-full-stack-updates-recommended); this
explains what the updater guarantees and where its boundaries are.

> **Operator boundary:** Install and operate the validator from a dedicated
> non-root account with passwordless access to Docker. The checkout, wallet
> directory, and updater state must be owned by that account and must not live
> below `/root`. Do not clone, migrate, adopt, bootstrap, recover, or run the
> Compose stack from a root login shell. `sudo` is reserved for the explicitly
> documented systemd-unit installation step.

## Trust model

A promoted stack descriptor binds all four services (`ditto-subnet`,
`dittobench-api`, `sandbox-docker`, `pylon`) to
immutable manifest digests. `ghcr.io/ditto-assistant/ditto-subnet-stack:compat-2`
is only a discovery tag: the updater resolves it once to a content-addressed
descriptor, verifies that digest's keyless Cosign certificate against this
repository's `release.yml` identity on `main`, and never uses the mutable tag
again during a transaction. The descriptor contains its own Compose file and
manifest; the updater rejects mutable component references, unexpected
repositories, missing or extra services, build contexts, and mismatched release
metadata — all before draining the validator.

The separate `candidate-compat-2` discovery tag exists only to reduce rollout
latency. The release workflow advances it after assembling and signing the
complete descriptor, while the remaining release smoke tests continue. The
prefetch timer resolves and authenticates the immutable descriptor digest, then
pulls and validates every component image. It never drains, recreates, or
resumes a service. Only the stable `compat-2` channel can start an update, and
that channel advances only after all release gates pass.

The host-side launcher, systemd unit, Docker Engine, wallet directory, and
`.env` are deliberately outside the signed bundle so a candidate workload can
never replace its own trust anchor. A separate timer keeps the host checkout's
launcher current by fast-forwarding a clean `main` checkout from the canonical
Ditto repository. It shares the transaction lock, rejects detached, dirty, or
divergent checkouts and noncanonical `origin` URLs, and does not change the
running descriptor-pinned stack. Any change to the updater protocol, Compose
schema, descriptor format, compatibility epoch, heartbeat protocol, or systemd
units is still a **supervised migration**. Within those gates, compatible patch
and minor releases in the same major version update automatically.

## First adoption

First adoption is always supervised: disable existing updater timers, fast-
forward this checkout to the reviewed release that published the descriptor
(keeping your existing `.env`), require a clean checkout and four healthy
services, then run `migrate <descriptor-digest>` — or `adopt
<descriptor-digest>` only when all four running services already match the
descriptor. Both require a fresh platform-accepted heartbeat and record the
installed stack atomically. Never substitute a mutable tag for the digest, and
enable the timer only after `status` looks right. A `managed-image.env` left
behind by the retired validator-only updater is not a full-stack adoption
record.

If `migrate` fails before a drain is reported and `status` shows no
transaction, the old stack was not touched: leave it running and capture the
release, digest, and updater log. If `status` reports a transaction, keep the
timers disabled and use `recover`.

### Greenfield bootstrap

A new host with no existing Compose services uses the same trust boundary
without first running a source-built validator. Before on-chain activation,
run `prepare <descriptor-digest>` to authenticate the exact descriptor and pull
all component images without starting anything. After the hotkey is registered
or swapped into place, run `bootstrap <the-same-descriptor-digest>`.

Bootstrap starts the exact release drained and requires the validator's signed
heartbeat, verified scorer probe, and every required component health/readiness
check to pass. It then records the managed release and resumes ticket intake.
It refuses mutable tags, a missing or different prepared digest, and existing
unmatched services. An interrupted bootstrap is retryable with the same digest:
a matching functionally ready drained stack resumes; an ambiguous or drifted
stack remains stopped for operator inspection.

## Transaction guarantees

Each automatic update: resolves the stable channel to a digest, validates the
descriptor, ensures every component is present without touching the running
stack (normally reusing the prefetch cache), drains the validator via `USR1`
(an active lease finishes first, and the platform must accept the drained
heartbeat), writes a durable journal, reconciles the full candidate stack,
requires healthy services plus a fresh platform-accepted heartbeat from the
candidate validator, records the descriptor atomically, and resumes lease
intake via `USR2`.

Any failure rolls **all four services** back to the retained previous
descriptor and resumes the old validator only once that stack is healthy; the
failed digest stays suppressed until the channel advances. Crashes recover
from the journal, and once `USR2` may have fired the updater refuses to
recreate the validator without a new drained acknowledgement. Named volumes
and the wallet bind survive the transaction; unrelated containers are never
touched; no managed update ever builds from source.

## Operations

- `status` is read-only and network-free; `recover` and `rollback` are
  supervised — keep the timer disabled while running them.
- The one-minute prefetch and stable update timers and the 15-minute updater
  checkout refresh share a lock. Prefetch and refresh defer to an update or
  interrupted transaction. Repeated release polls reuse the authenticated warm
  image cache.
- Keep enough free disk for two stacks concurrently, and never prune images
  referenced by the installed, previous, or pending descriptors.
- Source-built installs are never silently converted, and a registry outage
  leaves the current healthy stack running — the launcher never falls back to
  building source.
- For managed installs the release renderer injects the signed stack identity
  (version, revisions, protocols, component digests) into the validator
  environment; `.env` cannot override it. Benchmark work is accepted only when
  the scorer's runtime identity matches; an identity fault advertises no
  benchmark versions.
- Pylon is pinned to an amd64-only runtime; arm64 hosts need the documented
  binfmt/QEMU support.
