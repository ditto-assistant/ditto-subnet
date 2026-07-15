# Validator operations (SN118)

A validator leases miner submissions from the platform, scores them in an
isolated local sandbox, publishes signed results, and sets weights on Finney.
The supported production deployment is the root Docker Compose stack: one
`.env`, one `./scripts/validator-compose.sh up -d`, and no separate process
supervisor.

## Contents

- [What runs](#what-runs)
- [Requirements](#requirements)
- [First deployment](#first-deployment)
- [Verify health](#verify-health)
- [Upgrade and operate](#upgrade-and-operate)
- [Automatic validator updates (opt-in)](#automatic-validator-updates-opt-in)
- [How scoring and weights work](#how-scoring-and-weights-work)
- [Optional observability](#optional-observability)
- [Development](#development)

## What runs

Compose starts six services:

| Service | Purpose |
| --- | --- |
| `ditto-subnet` | Polls for work, signs scores, and computes weights. |
| `dittobench-api` | Scores submissions. |
| `sandbox-docker` | Provides an isolated nested Docker daemon for submission builds. |
| `model-relay` | Sends locked-model requests to Chutes without exposing the API key to sandboxes. |
| `ollama` | Serves `embeddinggemma` for memory scoring. |
| `pylon` | Uses the validator wallet to submit weights on chain. |

The validator is stateless. The queue and score ledger live on the platform,
while Pylon persists any in-flight weight submission in its named volume. A
restart does not lose scored work.

Screening is not part of this stack. The platform runs the pre-benchmark build
and health gate from
[`ditto-screener`](https://github.com/ditto-assistant/ditto-screener) before a
submission reaches validators.

## Requirements

- Linux x86-64 host with at least 4 vCPU, 16 GB RAM, and 80 GB free disk.
- Docker Engine with the Docker Compose plugin v2 or newer, including v5.
  Docker must start at boot.
- Git, used for the repository clone and verified `dittobench-api` build cache.
- A local Bittensor wallet whose hotkey is registered on Finney SN118 and has a
  validator permit.
- A Chutes API key for the locked `Qwen/Qwen3-32B-TEE` model.
- Outbound access to Finney, Chutes, and `https://platform-api.heyditto.ai`.

Python and `uv` are needed only for development or running components outside
Compose; they are not required for the production path below.

## First deployment

Clone the repository and create the one environment file Compose reads:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
cp .env.example .env
openssl rand -base64 32
```

Put the generated random value in `PYLON_TOKEN`, then fill these placeholders in
`.env`:

| Env | Value |
| --- | --- |
| `VALIDATOR_HOTKEY` | Public SS58 address of the permitted validator hotkey. |
| `VALIDATOR_WALLET_NAME` | Coldkey directory name under `~/.bittensor/wallets`. |
| `VALIDATOR_WALLET_HOTKEY` | Hotkey file name inside that wallet. |
| `PYLON_TOKEN` | Random token generated above. |
| `RELAY_API_KEY` | Chutes API key used only by `model-relay`. |

The example already selects the production platform and Finney; Compose
hardcodes SN118 for both the worker and Pylon. For a local chain, explicitly
replace `VALIDATOR_PLATFORM_API_URL` and `SUBTENSOR_NETWORK`; do not reuse the
production `.env`.

The wallet stays on the host. The `ditto-subnet` worker receives only the
configured hotkey file, and the bind mount is read-only; Pylon retains its own
read-only wallet mount for weight submission. The loaded wallet hotkey must
exactly match `VALIDATOR_HOTKEY`. Never put a mnemonic in `.env`, and never
commit `.env`.

Use the repository wrapper for every Compose command. It reads the reviewed
`dittobench-api` ref and checksum from `docker-compose.yml`, fetches that exact
commit into `~/.cache/ditto-subnet` when it is not already cached, refuses a ref
and checksum mismatch, and gives Compose a clean detached local build context.
This preserves the immutable source pin while avoiding the remote-context path
bug in Compose 2.40 and 5.0. The wrapper fails before startup if Docker, Buildx,
the Compose plugin, or the pinned source cannot be verified.

Validate the configuration and start the complete stack from the repository
root:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

Compose services use `restart: unless-stopped`, so Docker brings the validator
back after a host reboot. Do not also run it under PM2 or systemd, and do not run
two stacks with the same hotkey.

## Verify health

All six services should be `Up`; `ollama`, `sandbox-docker`, and
`dittobench-api` should also report `healthy`:

```sh
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs --since 10m ditto-subnet
curl -fsS https://platform-api.heyditto.ai/health
```

A healthy idle validator logs:

```text
scoring sweep complete: 0 agent(s)
```

Zero agents is normal when no submission is queued. During mining, successful
runs add `scored agent ... composite=...` lines. When an epoch is due, the
worker logs either a submitted weight count or that the ledger has no positive
scores.

Before each two-minute scoring sweep claims its first platform ticket, the
worker sends a real `embeddinggemma` request through
`http://sandbox-docker:11434/api/embed`. This is the same `socat` listener that
inner miner harnesses reach as `http://host.docker.internal:11434/api/embed`, so
the five-second probe covers the forwarder, Ollama API, loaded model, and a
non-empty embedding response. The `sandbox-docker` healthcheck probes that same
listener in addition to `docker info`.

If this preflight is unavailable or times out, the validator claims no ticket
for that sweep and continues the independent weight-setting path. If the route
fails after a benchmark starts, the failure is validator infrastructure rather
than a miner result: the sweep stops, the existing lease expires and reopens on
the platform, and the normal two-minute cadence prevents an immediate retry.
The validator also refuses to submit a score after its exact lease deadline.

Production acceptance is:

- the platform health response reports `db: ok` and `chain: ok`;
- sweeps complete without recurring platform, scorer, or Pylon errors;
- the configured hotkey is registered on SN118 and has a validator permit;
- the hotkey's on-chain last-update block advances after weights are submitted.
- `GET https://platform-api.heyditto.ai/api/v1/public/validators` lists the
  hotkey as online with its signed software version and source digest.

## Upgrade and operate

For a local-build validator that has not adopted automatic updates, pull and
reconcile in place. Taking the stack down first creates unnecessary downtime:

```sh
git pull --ff-only
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

After the registry image has been adopted below, the wrapper persists its exact
digest in `.validator-update/managed-image.env`. It rejects `up`, `down`,
`restart`, and other broad mutations that could silently replace that reviewed
image with the default local build. Sidecars must not be recreated during a
live benchmark either. Use the updater's cooperative drain around their
reconciliation:

```sh
git pull --ff-only
./scripts/validator-compose.sh config --quiet
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-auto-update.timer
sudo systemctl stop ditto-validator-auto-update.service
./scripts/validator-auto-update.sh reconcile-sidecars
./scripts/validator-compose.sh ps
```

Only after reconciliation reports that the validator resumed, re-enable the
opt-in timer:

```sh
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=true/' .env
sudo systemctl enable --now ditto-validator-auto-update.timer
```

The reconciliation command uses Compose `--wait` for all five sidecars before
it resumes the validator. If any sidecar build or health check fails, the
validator intentionally remains drained so it cannot accept a new lease against
a partial stack. Repair and verify the sidecars, then explicitly resume with
`./scripts/validator-auto-update.sh recover` while automatic updates and the
timer remain disabled.

The updater is the only normal path that recreates an adopted `ditto-subnet`
service or reconciles its sidecars. It waits for the same explicit drained
acknowledgement first; if work never drains, it resumes without changing a
sidecar. Do not bypass the wrapper with a direct `docker compose` command.

Useful commands:

```sh
./scripts/validator-compose.sh logs -f ditto-subnet
./scripts/validator-compose.sh logs --since 10m sandbox-docker
./scripts/validator-compose.sh logs --since 10m dittobench-api
./scripts/validator-compose.sh logs --since 10m pylon
```

Do not manually restart `ditto-subnet` while it may own a live lease. The
cooperative updater/rollback path is the benchmark-safe replacement mechanism.

If `sandbox-docker` exits, check its logs first. It must run privileged so its
nested daemon can build untrusted submissions, but the scorer never mounts or
controls the host Docker socket. On startup and every six hours, the nested
daemon automatically prunes unused containers, networks, images, and build
cache older than 24 hours, followed by volumes that no container references.
This bounds benchmark storage growth without touching unrelated host containers
or deleting active benchmark resources. Prune failures are warnings and retry
on the next cycle.

If the host reboots, verify both Docker and the stack rather than adding a
second supervisor:

```sh
systemctl is-enabled docker
systemctl is-active docker
./scripts/validator-compose.sh ps
```

## Automatic validator updates (opt-in)

Automatic updates are disabled by default. SN118 does not use Watchtower:
Watchtower is archived, does not recommend itself for production, cannot pull
the existing local-only validator build, and has no post-update health rollback.
Its lifecycle hooks are disabled by default, run inside an old target image that
must already contain a correct bounded-drain command, and do not solve that
rollback gap. It also needs a long-lived host Docker socket mount. Instead, this
repository provides a short-lived host updater run by a jittered systemd timer.

Two validator deployments were reviewed as design references at exact commits:

- SN44 TurboVision at
  [`3d8033cef740cd0c34a989183dcf3fa9f0c32467`](https://github.com/score-technologies/turbovision/tree/3d8033cef740cd0c34a989183dcf3fa9f0c32467)
  combines mutable `mikhaelscore/turbovision:latest` with `build: .`, inherits
  the update label through a common service block, and runs an unpinned
  `containrrr/watchtower` with a read-write Docker socket, 60-second polling,
  cleanup, and a 30-second stop grace. Its
  [publish workflow](https://github.com/score-technologies/turbovision/blob/3d8033cef740cd0c34a989183dcf3fa9f0c32467/.github/workflows/docker_push.yml)
  pushes `latest` and SHA tags to Docker Hub using repository credentials.
- SN97 Constantinople at
  [`ffdd0877d9b7124f99e4337ffbf6a7b86850a98a`](https://github.com/unconst/Constantinople/tree/ffdd0877d9b7124f99e4337ffbf6a7b86850a98a)
  uses mutable application `latest`, `pull_policy: always`, and unpinned
  Watchtower `latest` with a read-write socket, five-minute polling, cleanup,
  and rolling restart. Both validator and miner carry enable labels. Its
  [workflow](https://github.com/unconst/Constantinople/blob/ffdd0877d9b7124f99e4337ffbf6a7b86850a98a/.github/workflows/docker.yml)
  publishes GHCR tags with the built-in token, while the default
  [Compose file](https://github.com/unconst/Constantinople/blob/ffdd0877d9b7124f99e4337ffbf6a7b86850a98a/docker-compose.yml)
  names a Docker Hub image.

Label opt-in, bounded polling, and immutable release identifiers are useful
patterns. Their restart policies are not safe to reuse for SN118: neither
reference drains active work, gates the wire protocol and release line, or
automatically restores a failed application image. A restart can therefore cut
through SN118's 75-minute benchmark inside its 90-minute lease, lose the signed
score submission, and race the same-lease resume and monotonic progress rules.

The release workflow publishes only the validator worker to
`ghcr.io/ditto-assistant/ditto-subnet-validator`. It never publishes or updates
Pylon, `dittobench-api`, the model relay, Ollama, or `sandbox-docker`. Releases
has semantic-version and source-SHA tags for audit and discovery. `compat-2` is
the moving discovery tag; the updater resolves it to a registry digest before
draining anything, and that digest is the immutable deployment boundary.
The multi-architecture source-SHA manifest is built and pushed once, both amd64
and arm64 artifacts are smoke-tested from that registry digest, and only that
exact passing manifest is promoted to the semantic-version and `compat-2` tags.

Each candidate must carry the expected source, exact release version and
40-character revision, validator marker, heartbeat protocol, update protocol,
Compose schema, and compatibility epoch. A candidate that changes any
compatibility field, crosses the running major/minor release line, or is not
strictly newer fails closed. Minor and major upgrades require a supervised
migration even when a moving compatibility tag advances. A breaking platform
wire, consensus, required
configuration, wallet, or sidecar boundary change must increment the epoch and
requires a manual operator migration; the old timer remains on its previous
channel.

### Registry availability and legacy local builds

The release workflow publishes with its built-in `GITHUB_TOKEN`; no new
repository secret is required. An organization administrator must make the
GHCR package public after its first publication. Once public, validators pull
anonymously and need no registry credential or new secret. Until that one-time
visibility step is complete, `docker pull` fails and the updater leaves the
running validator untouched.

Existing v0.6.x validators are local builds and do not have the cooperative
drain or trusted image metadata. The updater intentionally refuses to signal or
replace them. Do not claim that enabling the timer upgrades such a container.
Coordinate one supervised maintenance window when the validator has no live
ticket, then perform the first registry-based deployment. After that migration,
all automatic updates use the bounded drain below. New installations can start
from the registry image directly.

Preflight the public image. On an existing local-build stack, coordinate a
maintenance window with no live ticket, then replace only the validator worker:

```sh
docker pull ghcr.io/ditto-assistant/ditto-subnet-validator:compat-2
IMAGE=ghcr.io/ditto-assistant/ditto-subnet-validator
DIGEST="$(docker image inspect --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
  "$IMAGE:compat-2" | awk -v prefix="$IMAGE@" 'index($0, prefix) == 1 { print; exit }')"
test -n "$DIGEST"
DITTO_SUBNET_IMAGE="$DIGEST" \
  ./scripts/validator-compose.sh up -d --no-deps --no-build --pull never \
  ditto-subnet
./scripts/validator-compose.sh logs --since 10m ditto-subnet
./scripts/validator-auto-update.sh adopt "$DIGEST"
./scripts/validator-auto-update.sh status
```

For a fresh host, start and verify the five non-validator services before the
digest-pinned validator; `--no-deps` is not a fresh-install command:

```sh
docker pull ghcr.io/ditto-assistant/ditto-subnet-validator:compat-2
IMAGE=ghcr.io/ditto-assistant/ditto-subnet-validator
DIGEST="$(docker image inspect --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
  "$IMAGE:compat-2" | awk -v prefix="$IMAGE@" 'index($0, prefix) == 1 { print; exit }')"
test -n "$DIGEST"
./scripts/validator-compose.sh up -d --build --wait --wait-timeout 180 \
  pylon sandbox-docker model-relay ollama dittobench-api
./scripts/validator-compose.sh ps
DITTO_SUBNET_IMAGE="$DIGEST" \
  ./scripts/validator-compose.sh up -d --no-deps --no-build --pull never \
  ditto-subnet
./scripts/validator-compose.sh logs --since 10m ditto-subnet
./scripts/validator-auto-update.sh adopt "$DIGEST"
./scripts/validator-auto-update.sh status
```

Do not use those commands to interrupt a live legacy benchmark. The first
migration is the precise boundary that cannot be automated safely because old
images have no drain control. `adopt` requires automatic updates to remain
disabled, verifies that the running labelled service exactly matches the
digest, validates all compatibility metadata, and requires a fresh accepted
platform heartbeat before writing managed mode. `status` must then show
`managed_image=$DIGEST`; otherwise, do not install the timer.

### Enable and verify

Set the opt-in only after `status` reports the immutable managed image, semantic
version, full revision, and operational update state. Set any non-default drain,
readiness, or polling values before installing:

```sh
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=true/' .env
sudo DITTO_VALIDATOR_UPDATE_USER="$USER" \
  ./scripts/install-validator-auto-update.sh
systemctl list-timers ditto-validator-auto-update.timer
sudo systemctl status ditto-validator-auto-update.timer
sudo journalctl -u ditto-validator-auto-update.service --since today
./scripts/validator-auto-update.sh status
```

The timer checks 15 minutes after boot and every 15 minutes thereafter, with up
to five minutes of jitter so validators do not restart together. The service
runs as the non-root Docker-capable operator with systemd hardening. Docker
socket access is still host-root-equivalent authority, but it is not exposed on
a TCP port or mounted into a long-lived container; the updater process exists
only for one check.

The installer reads the three timing values from `.env`, derives the matching
systemd start/stop budgets, and pins all five values into the unit. This prevents
a later `.env` edit from making the updater's runtime budget exceed systemd's
cleanup budget. After changing any
`VALIDATOR_AUTO_UPDATE_{DRAIN_TIMEOUT_SECONDS,READY_TIMEOUT_SECONDS,CHECK_SECONDS}`
setting, reinstall the unit before re-enabling it:

```sh
sudo systemctl disable --now ditto-validator-auto-update.timer
sudo DITTO_VALIDATOR_UPDATE_USER="$USER" \
  ./scripts/install-validator-auto-update.sh
```

The host must provide `flock` from util-linux; its kernel-held lock is released
automatically on exit or crash and prevents concurrent timer/manual runs.

For an available update, the script:

1. pulls the compatibility channel and resolves it to an immutable digest;
2. validates all image and service-scope labels before sending a signal;
3. asks the worker to stop claiming new tickets;
4. lets any claimed benchmark finish through signed score submission;
5. waits for an explicit local `drained` acknowledgement, not merely an empty
   `active_agent_id` (unticketed re-scores are also protected);
6. recreates only the labelled `ditto-subnet` service with `--no-deps`, in a
   quiescent bootstrap mode that cannot claim, re-score, or set weights;
7. waits for a compatibility-matched state backed by an accepted signed
   platform heartbeat;
8. commits the new digest and explicitly resumes work; and
9. retains the previous image under a local rollback tag.

A candidate that fails readiness has never been allowed to claim a ticket. The
same quiescent handshake applies while restoring a rollback image, preventing a
readiness decision or interrupted updater from cutting through new work. Resume
is persisted before work is allowed in the narrow
`validator-update-bootstrap` Compose volume; the root filesystem and wallet
mount remain read-only. A unique deployment token makes the marker valid for
container restarts but not a different candidate or rollback recreation, and
old markers are pruned. The token is a fresh 128-bit value from the host CSPRNG;
it is state coordination, not a wallet or registry credential. A subsequent
timer also recovers any quiescent commit
left by a power loss before considering candidate suppression. An atomic
`.validator-update/transaction.env` journal is written before the old container
stops; after a crash or reboot, the next run restores the retained image for any
uncommitted phase, or finishes resuming and recording an already committed
candidate. If a committed candidate cannot resume, rollback intent is journaled
before the previous image is recreated, and that failed digest is suppressed
after the previous image is safely resumed.

USR2 delivery is treated as ambiguous if the Docker client loses its response.
Once work may have resumed, the updater preserves the journal and refuses to
recreate either image without a new explicit quiescent proof. This can require
operator verification after a daemon/API failure, but it cannot silently trade
benchmark safety for automatic recovery.

If readiness fails, the failed candidate digest is recorded and suppressed;
later timer runs do not repeat that replacement. A different digest on the
compatibility channel clears the suppression after it passes readiness.

The default drain deadline is 4,800 seconds: five minutes beyond the 75-minute
benchmark cap and inside the platform's 90-minute lease. If work never drains,
the updater sends resume, performs no stop or replacement, and retries on the
next timer. It never force-kills a benchmark. The Compose service also has an
80-minute stop grace period as a defensive boundary for ordinary ticket/manual
operations. Do not use it as the update gate: a three-seed stale re-score can
exceed 80 minutes, so only the cooperative drain acknowledgement is safe.

### Emergency disable and rollback

Disable both future timer runs and any in-flight updater. Stopping the oneshot
invokes its phase-aware cleanup: it resumes a pending drain or restores the
retained image if replacement had already stopped the old container.

```sh
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-auto-update.timer
sudo systemctl stop ditto-validator-auto-update.service
./scripts/validator-auto-update.sh status
```

`status` prints `TRANSACTION_PHASE` when recovery is pending. If the container
and required sidecars are healthy but the journal remains after an ambiguous
resume or interrupted cleanup, keep automatic updates disabled and run the
bounded recovery explicitly:

```sh
./scripts/validator-compose.sh ps
./scripts/validator-auto-update.sh recover
./scripts/validator-auto-update.sh status
```

Do not run `recover` merely to clear a warning: it can resume lease intake. Use
it only after the validator image and all required sidecars have been verified.

If a replacement does not reach readiness, the updater automatically restores
the retained previous image and exits nonzero. For a later manual rollback,
keep automatic updates disabled and use the same cooperative drain path:

```sh
sudo systemctl disable --now ditto-validator-auto-update.timer
sudo systemctl stop ditto-validator-auto-update.service
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=false/' .env
./scripts/validator-auto-update.sh rollback
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs --since 10m ditto-subnet
```

Old images are not automatically deleted. After an observation window, an
operator may remove superseded rollback tags manually. Never enable broad image
cleanup or delete the currently recorded `PREVIOUS_IMAGE` in
`.validator-update/last-update.env` until rollback is no longer needed.

The updater uses the repository Compose wrapper and works with the supported
Compose v2 and v5 lines. It leaves the immutable reviewed `dittobench-api`
ref/checksum, scorer/relay pins, wallet mounts, Pylon data, and all other service
definitions untouched.

## How scoring and weights work

The platform leases at most three live scoring tickets per submission. Three
independent validators publish signed scores, and the platform finalizes the
median. Each ticket pins the dataset seed, dataset hash, and `full` run size, so
validators evaluate the same workload. An expired ticket reopens automatically.

Each validator reads the same public median-aggregated ledger and applies the
deterministic king-of-the-hill fold in `ditto/validator/weights.py`. A challenger
must clear both the relative margin and statistical error band. The champion
gets 90% of weight and the next four distinct miners split 10%; Yuma consensus
combines validators' on-chain vectors. That miner vector receives 20% of miner
emission; the other 80% is routed to SN118's owner-associated burn hotkey and
burned by Subtensor. With no eligible miners, 100% is burned. These consensus
values are frozen in code, not configurable through env.

The worker sends its vector to its co-located Pylon identity on an independent
timer, so a long scoring queue cannot delay the on-chain cadence. The timer
honors the larger of the configured interval, chain rate limit, and subnet tempo,
and waits for the validator's previous on-chain update window after a restart.
Pylon performs UID resolution, normalization, commit-reveal handling, retries,
and the final `put_weights` extrinsic. One `PYLON_TOKEN` protects both the
worker's permit check and identity writes.

## Optional observability

Add the shared `WANDB_API_KEY` provided by Ditto to `.env` (never commit it), or
set `WANDB_MODE=disabled` to opt out of aggregate telemetry.

W&B reports Pylon request acceptance separately from on-chain evidence. The
`weights/pylon_accepted` metric means the durable asynchronous request was
accepted; it is not finality. `weights/onchain_last_update_block` and
`weights/onchain_age_blocks` show the latest update actually observed on
Finney, including normal commit-reveal delay.

The worker also posts a signed public heartbeat with its software identity,
current phase/work id, and an optional coarse system-health sample. CPU, memory,
and root-disk utilization are rounded to five-point buckets; Docker contributes
only aggregate availability and running/unhealthy counts. It never reports host
or container identity, paths, images, env values, or secrets. Long benchmark
runs refresh `running_benchmark` every two minutes. Collection is automatic and
requires no new secret or operator setting; when Docker is inaccessible it is
reported as unavailable rather than failing the validator.

For a live scoring ticket, heartbeat protocol v4 also reports one allowlisted
benchmark stage (`preparing`, `building_harness`, `starting_harness`,
`running_benchmark`, `finalizing`, `submitting_result`, or
`failed_retrying`). During the running stage it may include only aggregate
completed/total check counts; the platform derives a coarse percentage. Stage
changes publish promptly, while same-stage count changes are limited to one per
minute and a five-percent bucket change. The signed progress is bound to the
active public agent and exact private ticket deadline. It never carries case
IDs or order, prompts, expected answers, tool names, memory or dataset content,
seeds or hashes, partial scores, per-case latency, model output, run/container
IDs, paths, logs, or error bodies. Progress and heartbeat failures remain
best-effort and cannot fail scoring or result submission. Older validators
without progress continue to report an unknown-progress active state.

## Development

For local code work outside Compose:

```sh
uv sync
make lint typecheck test
```

The worker entry point is `uv run python -m ditto.validator`. Point it at local
platform, scorer, and Pylon services, and set `VALIDATOR_DITTOBENCH_MOCK=true`
only when testing plumbing without a real benchmark.

Code references: `ditto/validator/{__main__,config,worker,weights,signing}.py`,
`ditto/chain/client.py`, and the root `docker-compose.yml`.
