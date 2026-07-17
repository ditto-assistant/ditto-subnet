# Validator operations (SN118)

A validator leases miner submissions, scores them in an isolated local sandbox,
publishes signed results, and sets weights on Finney. The preferred production
setup runs the validator from an immutable GHCR digest and uses the repository's
cooperative updater for patch releases. Building the validator from source is a
fallback when the release channel is unavailable.

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

The root Docker Compose stack starts six services:

| Service | Purpose |
| --- | --- |
| `ditto-subnet` | Leases work, signs scores, and computes weights. |
| `dittobench-api` | Loads screened images and scores submissions. |
| `sandbox-docker` | Loads and runs screened images; builds only legacy records. |
| `model-relay` | Reaches the locked Chutes model without exposing its key. |
| `ollama` | Serves the embedding model used for memory scoring. |
| `pylon` | Submits weights with the validator wallet. |

The platform owns the queue and score ledger. Pylon keeps in-flight weight
state in a named volume. Screening happens on the platform before work reaches
validators. Passing screens include a content- and image-ID-pinned Docker image
archive, so validators avoid recompiling the Rust crate. The source tarball is
still downloaded for structural anti-copy evidence, and records created before
this handoff retain the build fallback.

The staged default is **prefer screened image**:
`DITTOBENCH_ALLOW_SCREENED_IMAGES=1` and
`DITTOBENCH_REQUIRE_SCREENED_IMAGE=0`. The scorer loads a verified image when
one is present and otherwise retains the existing source build. After platform
coverage and mixed-fleet telemetry are healthy, operators can set
`DITTOBENCH_REQUIRE_SCREENED_IMAGE=1` to enter screened-only mode.

The scorer enables only its screened-image download path. Its broader private
harness URL bypass remains disabled in production, so validator-controlled
arbitrary private or loopback artifact URLs are not accepted.

Heartbeat protocol v7 signs the effective screened-image/fallback mode,
executor isolation, managed-stack state, and identities for `ditto-subnet` plus
all five sidecars. The platform uses this for compatible ticket routing during
gradual rollout. It is not remote host attestation; the immutable descriptor
and component digests prove release selection, while host compromise remains a
separate executor-boundary risk.

The scorer source is pinned to an exact commit on `dittobench-api` `main`.
During a coordinated scorer rollout, merge the scorer change first and then
update the `docker-compose.yml` checksum to its actual post-merge `main` SHA.
For a supervised smoke before updating the committed pin, pass the same
immutable main ref and checksum explicitly:

```sh
DITTOBENCH_BUILD_CONTEXT='https://github.com/ditto-assistant/dittobench-api.git?ref=refs/heads/main&checksum=<40-character-post-merge-main-sha>' \
  ./scripts/validator-compose.sh build model-relay dittobench-api
```

Before its first build, the wrapper verifies the checksum is in `main` history;
an unmerged PR head cannot masquerade as a `main` pin. That evidence is cached
beside the immutable checkout, so later restarts and read-only Compose commands
do not depend on GitHub availability and the pin may safely lag a newer `main`.

## Requirements

- Linux x86-64 with at least 4 vCPU, 16 GB RAM, and 80 GB free disk.
- Docker Engine, Buildx, and the Docker Compose plugin v2 or newer, including
  v5. Docker must start at boot.
- Git and `flock` from util-linux.
- A local Bittensor wallet whose hotkey is registered on Finney SN118 and has a
  validator permit.
- A Chutes API key for the locked `Qwen/Qwen3-32B-TEE` model.
- Outbound access to Finney, Chutes, the Ditto platform, and GHCR.
- Anonymous pull access to the public
  `ghcr.io/ditto-assistant/ditto-subnet-validator` package.

Python and `uv` are only required for development or running components outside
Compose.

## First deployment

Clone the repository and create the one environment file used by Compose:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
cp .env.example .env
openssl rand -base64 32
```

Put the generated value in `PYLON_TOKEN`, then fill these values in `.env`:

| Env | Value |
| --- | --- |
| `VALIDATOR_HOTKEY` | Public SS58 address of the permitted validator hotkey. |
| `VALIDATOR_WALLET_NAME` | Coldkey directory under `~/.bittensor/wallets`. |
| `VALIDATOR_WALLET_HOTKEY` | Hotkey file inside that wallet. |
| `PYLON_TOKEN` | Random token generated above. |
| `RELAY_API_KEY` | Chutes API key used only by `model-relay`. |

The example selects Finney, SN118, and the production platform. For local
testing, change both the platform and chain settings and use a separate `.env`.

The wallet remains on the host and is mounted read-only. The loaded hotkey must
exactly match `VALIDATOR_HOTKEY`. Never put a mnemonic in `.env`, never commit
`.env`, and never run two validator stacks with the same hotkey.

Resolve the release channel to the exact digest that will run:

```sh
IMAGE=ghcr.io/ditto-assistant/ditto-subnet-validator
docker pull "$IMAGE:compat-2"
DIGEST="$(
  docker image inspect \
    --format '{{ range .RepoDigests }}{{ println . }}{{ end }}' \
    "$IMAGE:compat-2" |
    awk -v prefix="$IMAGE@" 'index($0, prefix) == 1 { print; exit }'
)"
test -n "$DIGEST"
printf '%s\n' "$DIGEST"
```

The public compatibility channel is a multi-architecture manifest promoted only
after its Linux amd64 and arm64 images pass registry smoke tests. Stop if the
pull or digest check fails. Do not substitute a mutable tag or an unpromoted
source-SHA image; use the [source-build fallback](#development) until the
channel is available.

Start and verify the five sidecars, then start only the digest-pinned validator:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build --wait --wait-timeout 180 \
  pylon sandbox-docker model-relay ollama dittobench-api
DITTO_SUBNET_IMAGE="$DIGEST" \
  ./scripts/validator-compose.sh up -d --no-deps --no-build --pull never \
  ditto-subnet
./scripts/validator-compose.sh logs --since 10m ditto-subnet
```

After the validator reports a fresh platform-accepted heartbeat, adopt the
running digest into managed mode:

```sh
./scripts/validator-auto-update.sh adopt "$DIGEST"
./scripts/validator-auto-update.sh status
```

`adopt` fails closed unless the running service exactly matches the digest and
all release, source, protocol, Compose, and compatibility labels match. First
adoption is always supervised and automatic updates must remain disabled until
`status` shows the expected `managed_image`, version, revision, and operational
state.

For an existing source-built validator, schedule the same first adoption during
a supervised maintenance window. Confirm it has no live ticket before replacing
only `ditto-subnet` with the digest-pinned command above, then run `adopt`. Never
interrupt a legacy benchmark to enter managed mode.

## Verify health

```sh
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs --since 10m ditto-subnet
curl -fsS https://platform-api.heyditto.ai/health
./scripts/validator-auto-update.sh status
```

All six services should be `Up`; `ollama`, `sandbox-docker`, and
`dittobench-api` should be healthy. An idle validator may log
`scoring sweep complete: 0 agent(s)`. That is normal when the queue is empty.

Production acceptance also requires:

- platform health reports `db: ok` and `chain: ok`;
- the hotkey has a validator permit on SN118;
- sweeps complete without recurring platform, scorer, or Pylon errors;
- the on-chain last-update block advances after a weight submission; and
- the public validators endpoint lists the hotkey online with its signed
  version and source digest.

## Upgrade and operate

With automatic updates enabled, use the updater for the validator service. Do
not use direct `docker compose`, a second supervisor, or manual validator
restarts; those paths can replace a reviewed digest or interrupt leased work.

Useful commands:

```sh
./scripts/validator-compose.sh ps
./scripts/validator-compose.sh logs -f ditto-subnet
./scripts/validator-compose.sh logs --since 10m sandbox-docker
./scripts/validator-compose.sh logs --since 10m dittobench-api
./scripts/validator-compose.sh logs --since 10m pylon
./scripts/validator-auto-update.sh status
```

Repository scripts and the five sidecars remain supervised. To update them,
disable the updater, pull the reviewed repository change, and use the updater's
drained reconciliation:

```sh
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-auto-update.timer
sudo systemctl stop ditto-validator-auto-update.service
git pull --ff-only
./scripts/validator-compose.sh config --quiet
./scripts/validator-auto-update.sh reconcile-sidecars
./scripts/validator-compose.sh ps
```

If reconciliation succeeds, set `VALIDATOR_AUTO_UPDATE=true` again and
re-enable the timer. If a sidecar fails to build or become healthy, the
validator remains drained. Repair the sidecars, verify them, then run
`./scripts/validator-auto-update.sh recover` while the timer stays disabled.

### Troubleshooting

- **GHCR pull fails:** confirm outbound access to `ghcr.io` and that `compat-2`
  exists. Do not guess a digest or fall back to a mutable tag.
- **No work is scored:** zero queued agents is normal. Otherwise inspect the
  validator, sandbox, scorer, relay, and Ollama health before restarting
  anything.
- **Updater reports a transaction:** keep the timer disabled, verify the
  validator and all sidecars, then use `recover`. It may resume lease intake.
- **Host rebooted:** verify Docker is enabled and active, then check Compose and
  updater status. Do not add PM2 or another systemd service for the stack.
- **Disk use grows:** inspect `sandbox-docker`. Its nested daemon prunes unused
  benchmark data; do not run broad cleanup against the host Docker daemon.

## Automatic full-stack updates (opt-in)

Managed installations update the complete immutable Compose stack. The
one-time migration, transaction boundaries, rollback guarantees, Cosign trust
policy, and gradual screened-image rollout are documented in
[FULL-STACK-UPDATES.md](FULL-STACK-UPDATES.md).

First disable the legacy updater, install Cosign from its verified upstream
release, and resolve the signed stack channel to an immutable digest. With the
timer still disabled, `migrate` waits for the current validator to drain,
installs all six exact services, starts the validator quiescent, verifies a
fresh accepted heartbeat, and records the stack:

```sh
sed -i 's/^VALIDATOR_AUTO_UPDATE=.*/VALIDATOR_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-auto-update.timer 2>/dev/null || true
STACK=ghcr.io/ditto-assistant/ditto-subnet-stack
docker pull "$STACK:compat-2"
DIGEST="$(
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "$STACK:compat-2" |
  awk -v prefix="$STACK@" 'index($0, prefix) == 1 { print; exit }'
)"
test -n "$DIGEST"
./scripts/validator-stack-auto-update.sh migrate "$DIGEST"
./scripts/validator-stack-auto-update.sh status
```

If all six services already match the descriptor, `adopt "$DIGEST"` records
them without replacement. Both commands fail closed if the descriptor's
keyless signature is not from this repository's `release.yml` on `main`.

Enable the new timer only after migration or adoption succeeds:

```sh
if grep -q '^VALIDATOR_STACK_AUTO_UPDATE=' .env; then
  sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=true/' .env
else
  printf '\nVALIDATOR_STACK_AUTO_UPDATE=true\n' >>.env
fi
sudo DITTO_VALIDATOR_UPDATE_USER="$USER" \
  ./scripts/install-validator-stack-auto-update.sh
systemctl list-timers ditto-validator-stack-auto-update.timer
sudo systemctl status ditto-validator-stack-auto-update.timer
./scripts/validator-stack-auto-update.sh status
```

The timer checks `compat-2`, authenticates and resolves it once, then validates
and pre-pulls every exact component before asking the worker to drain. An active
benchmark finishes through signed result submission. If the drain deadline
expires, the worker resumes and no service is replaced.

A newer patch or minor release in the same major line can install automatically
only while the compatibility epoch, updater protocol, Compose schema, and
descriptor format remain accepted by the stable host launcher. Major or schema
changes require supervised migration. Every compatible update replaces Pylon,
the scorer, relay, Ollama, sandbox daemon, and validator as one transaction.

After replacement, the candidate starts unable to accept work until its exact
digest and compatibility state are verified through a fresh accepted
heartbeat. A failed candidate is suppressed and the complete retained previous
stack is restored. Recovery and rollback also require a drained validator. The
updater fails closed when it cannot prove a safe state.

To disable updates or inspect an interrupted run:

```sh
sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-stack-auto-update.timer
sudo systemctl stop ditto-validator-stack-auto-update.service
./scripts/validator-stack-auto-update.sh status
```

If the validator and all sidecars are healthy but `status` shows
`transaction_phase`, run `./scripts/validator-stack-auto-update.sh recover`
only after verifying that resuming lease intake is safe.

For a later manual rollback, keep updates disabled and use the same cooperative
drain path:

```sh
./scripts/validator-stack-auto-update.sh rollback
./scripts/validator-stack-auto-update.sh status
```

## How scoring and weights work

The platform leases each submission to independent validators and finalizes the
median signed score. Each ticket pins the workload and deadline; expired work
reopens automatically. The validator computes the deterministic weight vector
from the public finalized ledger, and Pylon handles UID resolution,
commit-reveal, retries, and the on-chain extrinsic on an independent cadence.
Weight scheduling honors the configured interval, chain rate limit, subnet
tempo, and the validator's previous on-chain update after a restart.

## Optional observability

Add the shared `WANDB_API_KEY` supplied by Ditto to `.env`, or set
`WANDB_MODE=disabled`. Never commit the key. W&B distinguishes Pylon request
acceptance from an update observed on chain.

The validator also sends a signed public heartbeat with its version, source
digest, phase, work ID, and coarse health. It does not send secrets, prompts,
expected answers, model output, dataset contents, or host/container identity.

## Development

The source-build path is a fallback when the reviewed GHCR compatibility
channel is unavailable. It does not enter managed updater mode:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

Upgrade a source-built validator only during a supervised window with no live
lease:

```sh
git pull --ff-only
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

Do not enable the automatic updater until a supervised digest-pinned migration
and `adopt` have succeeded.

For local code work outside Compose:

```sh
uv sync
make lint typecheck test
```

The worker entry point is `uv run python -m ditto.validator`.

The committed production context pins the scorer's reviewed post-merge `main`
commit. The wrapper verifies that immutable checksum is still a `main`
ancestor before any fresh remote build. The explicit unmerged-smoke override is
reserved for local contributor testing and must never be used on Finney.
