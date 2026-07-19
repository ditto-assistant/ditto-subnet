# Validator operations (SN118)

A validator leases miner submissions, scores them in an isolated local sandbox,
publishes signed results, and sets weights on Finney. Production runs the
validator from an immutable GHCR digest with the repository's cooperative
updater; building from source is a fallback when the release channel is
unavailable.

## Contents

- [What runs](#what-runs)
- [Requirements](#requirements)
- [First deployment](#first-deployment)
- [Verify health](#verify-health)
- [Upgrade and operate](#upgrade-and-operate)
- [Automatic full-stack updates (recommended)](#automatic-full-stack-updates-recommended)
- [How scoring and weights work](#how-scoring-and-weights-work)
- [Optional observability](#optional-observability)
- [Development](#development)

## What runs

The root Docker Compose stack starts six services:

| Service | Purpose |
| --- | --- |
| `ditto-subnet` | Leases work, signs scores, and computes weights. |
| `dittobench-api` | Scores submissions. |
| `sandbox-docker` | Isolated nested Docker daemon that runs miner containers. |
| `model-relay` | Reaches the locked model on the selected provider without exposing its key. |
| `ollama` | Serves the embedding model used for memory scoring. |
| `pylon` | Submits weights with the validator wallet. |

The validator is stateless: the queue and score ledger live on the platform,
and Pylon keeps in-flight weight state in a named volume. The platform screens
every submission before it reaches validators and ships a verified pre-built
Docker image with it, so your host normally does not compile miner code.

The scorer admits one full run at a time, and every miner container runs with
strict CPU, memory, PID, capability, seccomp, and egress limits. Do not
increase scorer concurrency on the same host; add validators on separate
capacity instead.

## Requirements

- Linux x86-64 with at least 4 vCPU, 16 GB RAM, and 80 GB free disk.
- Docker Engine, Buildx, and the Docker Compose plugin v2 or newer, including
  v5. Docker must start at boot.
- Git and `flock` from util-linux.
- A local Bittensor wallet whose hotkey is registered on Finney SN118 and has a
  validator permit.
- An API key for the locked Qwen3-32B model on ONE certified provider: Chutes
  (`Qwen/Qwen3-32B-TEE`, the default) or OpenRouter (`qwen/qwen3-32b`). Select
  with `RELAY_PROVIDER`. The two are certified as comparable, and the fleet is
  meant to split across them to keep throughput up.
- Outbound access to Finney, the selected model provider, the Ditto platform,
  and GHCR (anonymous pull of the public
  `ghcr.io/ditto-assistant/ditto-subnet-validator` package).

Python and `uv` are only required for development.

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
| `DITTOBENCH_CAPABILITIES_TOKEN` | Another random per-host token (`openssl rand -hex 32`). |
| `RELAY_PROVIDER` | `chutes` (default) or `openrouter`. |
| `RELAY_API_KEY` | API key for the selected provider, used only by `model-relay`. |

The example selects Finney, SN118, and the production platform. For local
testing, change both the platform and chain settings in a separate `.env`.

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

Stop if the pull or digest check fails. Do not substitute a mutable tag or an
unpromoted image; use the [source-build fallback](#development) until the
channel is available.

Start the five sidecars, then start only the digest-pinned validator:

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

`adopt` fails closed unless the running service exactly matches the digest.
First adoption is always supervised; keep automatic updates disabled until
`status` shows the expected `managed_image`, version, and operational state.

For an existing source-built validator, do the same first adoption during a
supervised maintenance window with no live ticket. Never interrupt a running
benchmark to enter managed mode.

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
- the public validators endpoint lists the hotkey online.

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
re-enable the timer. If a sidecar fails, the validator remains drained: repair
and verify the sidecars, then run `./scripts/validator-auto-update.sh recover`
while the timer stays disabled.

### Troubleshooting

- **GHCR pull fails:** confirm outbound access to `ghcr.io` and that `compat-2`
  exists. Do not guess a digest or fall back to a mutable tag.
- **No work is scored:** zero queued agents is normal. Otherwise inspect the
  validator, sandbox, scorer, relay, and Ollama health before restarting
  anything.
- **Runs fail with `tool_endpoint advertised but unreachable`:** one failure
  can be a non-compliant miner harness; recurring failures across different
  agents mean your sandbox networking (`host.docker.internal` routing) is
  broken — fix it before the reopened tickets expire. No zeroed score is
  signed either way.
- **Logs show `transcript publication failed`:** the accepted score already
  stands. Check `dittobench-api` health and platform reachability so future
  runs publish their transcripts.
- **Updater reports a transaction:** keep the timer disabled, verify the
  validator and all sidecars, then use `recover`.
- **Host rebooted:** verify Docker is enabled and active, then check Compose
  and updater status. Do not add PM2 or another systemd service for the stack.
- **Disk use grows:** inspect `sandbox-docker`. Its nested daemon prunes unused
  benchmark data; do not run broad cleanup against the host Docker daemon.
- **Sandbox resource failure (`sandbox_oom`, `sandbox_tmpfs_exhausted`):**
  these are validator-infrastructure classifications, not miner verdicts; the
  worker stops claiming work and the ticket expires safely. Fix the resource
  issue, then ask the Ditto team for the audited single-agent retry; never
  bulk retry or alter accepted scores.

## Automatic full-stack updates (recommended)

Enable the managed stack updater unless you run your own update automation.
Patch releases ship often, and the platform routes work by advertised
compatibility, so a validator that lags the release channel falls out of
ticket routing; the updater keeps the complete immutable Compose stack current
as one transaction with automatic rollback. If
you maintain your own updater instead, that is fine — it must track the
`compat-2` channel promptly, pin exact digests, and drain the validator before
replacing services.

The migration preflight, transaction boundaries, rollback guarantees, and
trust policy are documented in
[FULL-STACK-UPDATES.md](FULL-STACK-UPDATES.md); read it before the first
cutover.

Update this checkout to the exact reviewed release, install Cosign from its
verified upstream release, disable the legacy updater, and migrate. `migrate`
waits for the validator to drain, installs all six exact services, and verifies
a fresh accepted heartbeat before recording the stack:

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
signature is not from this repository's release workflow.

Enable the timer only after migration or adoption succeeds:

```sh
if grep -q '^VALIDATOR_STACK_AUTO_UPDATE=' .env; then
  sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=true/' .env
else
  printf '\nVALIDATOR_STACK_AUTO_UPDATE=true\n' >>.env
fi
sudo DITTO_VALIDATOR_UPDATE_USER="$USER" \
  ./scripts/install-validator-stack-auto-update.sh
systemctl list-timers ditto-validator-stack-auto-update.timer
./scripts/validator-stack-auto-update.sh status
```

Compatible patch and minor releases then install automatically: the updater
drains the validator (an active benchmark finishes first), replaces all six
services as one transaction, and rolls the complete previous stack back if the
new one fails verification. Major or schema changes require supervised
migration.

To disable updates, inspect an interrupted run, or roll back manually:

```sh
sed -i 's/^VALIDATOR_STACK_AUTO_UPDATE=.*/VALIDATOR_STACK_AUTO_UPDATE=false/' .env
sudo systemctl disable --now ditto-validator-stack-auto-update.timer
sudo systemctl stop ditto-validator-stack-auto-update.service
./scripts/validator-stack-auto-update.sh status
./scripts/validator-stack-auto-update.sh rollback   # manual rollback only
```

If everything is healthy but `status` shows `transaction_phase`, run
`./scripts/validator-stack-auto-update.sh recover` only after verifying that
resuming lease intake is safe.

## How scoring and weights work

The platform leases each submission to independent validators and finalizes the
median signed score. Each ticket pins the workload and deadline; expired work
reopens automatically, and every benchmark run must originate from a live
platform ticket. Each scored run starts with a reachability preflight that
requires the miner harness to call the mock tool endpoint; if the probe is
never observed the run fails and the ticket reopens — a zeroed report is never
signed. After the platform accepts a score, the worker publishes the run's
graded transcript for public verification.

The validator computes the deterministic weight vector from the public
finalized ledger, and Pylon handles UID resolution, commit-reveal, retries, and
the on-chain extrinsic on an independent cadence that honors the chain rate
limit and subnet tempo.

## Optional observability

Add the shared `WANDB_API_KEY` supplied by Ditto to `.env`, or set
`WANDB_MODE=disabled`. Never commit the key.

The validator also sends a signed public heartbeat with its version, source
digest, phase, and coarse health; the platform uses it to route compatible
work. It does not send secrets, prompts, expected answers, model output, or
host identity.

### Per-component stack health

Heartbeat protocol 9 adds a signed health entry for each of the six Compose
components. Three ideas are reported separately and must not be conflated:

- **Configured identity** — what Compose intends to run (the pinned image
  digest / source revision already reported under `stack`). It proves intent,
  not the running artifact.
- **Observed identity** — what a live probe could independently verify (for
  example the scorer's `/v1/capabilities` revision). When nothing can be
  observed safely the field stays unset; the validator never copies the
  configured pin into an observed field.
- **Functional readiness** — whether the component answered a real request
  just now (`ready`, plus `model_ready` for the embedding model / model
  route), with its own `observed_at`, so a stale probe is distinguishable from
  a stale heartbeat.

Each component reports `healthy`, `degraded`, `unreachable`,
`identity_mismatch`, or `unknown`. Probes run from the validator over the
private Compose network — no Docker socket is mounted for telemetry — and are
bounded so a wedged sidecar never stalls the heartbeat. Optional env:

- `VALIDATOR_SANDBOX_DOCKER_PROBE_URL` / `VALIDATOR_MODEL_RELAY_PROBE_URL` —
  internal readiness endpoints; unset components report `unknown`.
- `VALIDATOR_PYLON_PROBE_URL` — defaults to `PYLON_URL`.
- `VALIDATOR_STACK_PROBE_TIMEOUT_SECONDS` (default 2) and
  `VALIDATOR_STACK_HEALTH_CACHE_SECONDS` (default 60).

Probe URLs are configuration only; the public payload carries health states,
timestamps, booleans, and verified identities — never URLs, hostnames,
container ids, or paths.

## Development

The source-build path is a fallback when the reviewed GHCR compatibility
channel is unavailable. It does not enter managed updater mode:

```sh
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

Upgrade a source-built validator only during a supervised window with no live
lease: `git pull --ff-only`, then the same three commands.

The wrapper builds `dittobench-api` from the exact reviewed commit pinned in
`docker-compose.yml` and refuses a checksum that is not in that repository's
`main` history.

For local code work outside Compose:

```sh
uv sync
make lint typecheck test
```

The worker entry point is `uv run python -m ditto.validator`.
