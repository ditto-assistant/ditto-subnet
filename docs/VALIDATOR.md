# Validator operations (SN118)

A validator leases miner submissions from the platform, scores them in an
isolated local sandbox, publishes signed results, and sets weights on Finney.
The supported production deployment is the root Docker Compose stack: one
`.env`, one `docker compose up -d`, and no separate process supervisor.

## Contents

- [What runs](#what-runs)
- [Requirements](#requirements)
- [First deployment](#first-deployment)
- [Verify health](#verify-health)
- [Upgrade and operate](#upgrade-and-operate)
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
and health gate on a dedicated host before a submission reaches validators.

## Requirements

- Linux x86-64 host with at least 4 vCPU, 16 GB RAM, and 80 GB free disk.
- Docker Engine with Docker Compose v2. Docker must start at boot.
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

The wallet stays on the host and is mounted read-only. The loaded wallet hotkey
must exactly match `VALIDATOR_HOTKEY`. Never put a mnemonic in `.env`, and never
commit `.env`.

Validate the configuration and start the complete stack from the repository
root:

```sh
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Compose services use `restart: unless-stopped`, so Docker brings the validator
back after a host reboot. Do not also run it under PM2 or systemd, and do not run
two stacks with the same hotkey.

## Verify health

All six services should be `Up`; `ollama`, `sandbox-docker`, and
`dittobench-api` should also report `healthy`:

```sh
docker compose ps
docker compose logs --since 10m ditto-subnet
curl -fsS https://platform-api.heyditto.ai/health
```

A healthy idle validator logs:

```text
sweep complete: 0 agent(s)
```

Zero agents is normal when no submission is queued. During mining, successful
runs add `scored agent ... composite=...` lines. When an epoch is due, the
worker logs either a submitted weight count or that the ledger has no positive
scores.

Production acceptance is:

- the platform health response reports `db: ok` and `chain: ok`;
- sweeps complete without recurring platform, scorer, or Pylon errors;
- the configured hotkey is registered on SN118 and has a validator permit;
- the hotkey's on-chain last-update block advances after weights are submitted.

## Upgrade and operate

Pull and reconcile in place; taking the stack down first creates unnecessary
downtime and is not required:

```sh
git pull --ff-only
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Useful commands:

```sh
docker compose logs -f ditto-subnet
docker compose logs --since 10m sandbox-docker
docker compose logs --since 10m dittobench-api
docker compose logs --since 10m pylon
docker compose restart ditto-subnet
```

If `sandbox-docker` exits, check its logs first. It must run privileged so its
nested daemon can build untrusted submissions, but the scorer never mounts or
controls the host Docker socket. If the host reboots, verify both Docker and the
stack rather than adding a second supervisor:

```sh
systemctl is-enabled docker
systemctl is-active docker
docker compose ps
```

## How scoring and weights work

The platform leases at most three live scoring tickets per submission. Three
independent validators publish signed scores, and the platform finalizes the
median. Each ticket pins the dataset seed, dataset hash, and `full` run size, so
validators evaluate the same workload. An expired ticket reopens automatically.

Each validator reads the same public median-aggregated ledger and applies the
deterministic king-of-the-hill fold in `ditto/validator/weights.py`. A challenger
must clear both the relative margin and statistical error band. The champion
gets 90% of weight and the next four distinct miners split 10%; Yuma consensus
combines validators' on-chain vectors. These consensus values are frozen in
code, not configurable through env.

The worker sends its vector to its co-located Pylon identity. Pylon performs UID
resolution, normalization, commit-reveal handling, retries, and the final
`put_weights` extrinsic. One `PYLON_TOKEN` protects both the worker's permit
check and identity writes.

## Optional observability

Production behavior and internal service routes are fixed by Compose. Operators
may configure logging and aggregate telemetry:

| Env | Default | Meaning |
| --- | --- | --- |
| `VALIDATOR_LOG_LEVEL` | `INFO` | Worker log level. |
| `WANDB_MODE` | `online` | `online` publishes aggregate stats to the shared `heyditto/ditto-sn118` project; set `disabled` to opt out. |
| `WANDB_API_KEY` | none | The shared read+write key the Ditto team provides trusted validators confidentially. Required when `WANDB_MODE=online`; never commit it. |
| `WANDB_RUN_NAME` | `validator-<hotkey8>` | Auto-derived per hotkey so validators sharing the project never collide. Leave unset unless overriding. |

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
