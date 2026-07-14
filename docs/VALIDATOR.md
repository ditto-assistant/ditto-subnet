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
and health gate from
[`ditto-screener`](https://github.com/ditto-assistant/ditto-screener) before a
submission reaches validators.

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
- `GET https://platform-api.heyditto.ai/api/v1/public/validators` lists the
  hotkey as online with its signed software version and source digest.

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
combines validators' on-chain vectors. That miner vector receives 20% of miner
emission; the other 80% is routed to SN118's owner-associated burn hotkey and
burned by Subtensor. With no eligible miners, 100% is burned. These consensus
values are frozen in code, not configurable through env.

The worker sends its vector to its co-located Pylon identity. Pylon performs UID
resolution, normalization, commit-reveal handling, retries, and the final
`put_weights` extrinsic. One `PYLON_TOKEN` protects both the worker's permit
check and identity writes.

## Optional observability

Add the shared `WANDB_API_KEY` provided by Ditto to `.env` (never commit it), or
set `WANDB_MODE=disabled` to opt out of aggregate telemetry.

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
