# Validator guide (SN118)

A validator scores miner submissions and sets on-chain weights from the results.
Scoring is decentralized across independent validators with no central scorer:
the platform leases up to three tickets per submission and finalizes each on the
median of the three scores. Every validator is independent, running the same
worker with its own registered hotkey; this guide is how to join.

Terms used below: KOTH (king-of-the-hill, the current champion), ATH
(all-time-high dethroning gate), and Yuma consensus (the chain mechanism that
combines validators' weight vectors and clips outliers).

## Contents

- [What a validator does (and doesn't)](#1-what-a-validator-does-and-doesnt)
- [Requirements](#2-requirements)
- [Install and configure](#3-install-and-configure)
- [Model gateway](#4-model-gateway)
- [Set up Pylon](#5-set-up-pylon)
- [Run it](#6-run-it)
- [Verify it's working](#7-verify-its-working)
- [Mechanism reference](#8-mechanism-reference)
- [Environment reference](#9-environment-reference)
- [Code references](#code-references)

## 1. What a validator does (and doesn't)

The validator is one stateless Python process (`python -m ditto.validator`) that
loops:

1. Sweep (every `VALIDATOR_SWEEP_SECONDS`, default 120s): pull agents in
   `evaluating` from the platform's `/validator/*` HTTP API.
2. Score each through its co-located dittobench-api instance (by presigned
   tarball URL), sign the composite with the hotkey (sr25519), and POST it to the
   platform's public score ledger.
3. Set weights (every `VALIDATOR_EPOCH_SECONDS`, default 3600s): re-read the
   durable ledger, fold it into the deterministic KOTH+ATH weight vector, and
   submit on chain.

What it does not do:

- No database, no local state. The queue and the score ledger live behind the
  platform API; a validator can be killed and restarted at any time and loses
  nothing.
- No judge model, no validator LLM key. Scoring is judge-free and deterministic;
  the only model in a run is the locked harness model, served from a gateway you
  host (section 4).
- No server-side champion selection. The weight fold
  (`ditto/validator/weights.py`) is a pure function of the public ledger, so
  every honest validator computes the identical vector and Yuma consensus clips
  deviators.

### The k=3 model

- The platform issues a leased ticket to a validator that asks for work
  (`POST /api/v1/validator/job`), capped at three live tickets per submission
  (`SCORING_QUORUM=3`). Once a submission's three slots are filled, most polls
  return no job; a ticket not scored before its deadline expires and the slot
  re-opens.
- Each validator scores independently and posts one signed score. At three
  scores the platform finalizes on the median, so no single validator decides a
  score and an outlier cannot move the result.
- Every validator then folds the same public median-aggregated ledger with the
  identical KOTH+ATH function and sets its own weights; Yuma consensus combines
  them. Bringing up more independent validators, each a distinct registered
  hotkey, is how scoring decentralizes.

## 2. Requirements

| What | Why |
| --- | --- |
| Linux host: 4 vCPU, 16 GB RAM, 80 GB+ free disk | Runs the worker plus the co-located dittobench-api scorer; Docker sandbox builds dominate disk use. |
| x86-64 CPU | The upstream Pylon image is currently published for `linux/amd64`. |
| Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) | `uv sync` installs the pinned environment. |
| A hotkey registered on SN118 with a `validator_permit` | The chain accepts weights only from permitted validators (stake above the permit threshold). |
| A co-located dittobench-api instance on a Docker-capable host | Builds and scores each submission. See the [dittobench-api](https://github.com/ditto-assistant/dittobench-api) repo. |
| A Chutes key for the locked Qwen3-32B | The harness is scored against one locked model, served through Chutes (`Qwen/Qwen3-32B-TEE`) via the model-relay; no GPU is needed. |
| Outbound reach to the platform API and Pylon | Pylon is the validator weight-setting service; the worker listens on nothing. |

Keep the mnemonic or wallet key and any gateway key (the Chutes relay key) in a
secret manager and inject them as env; they must never be logged or committed.
The validator hotkey (an SS58 address) is public.

## 3. Install and configure

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
cp .env.example .env
```

Edit `.env` with the settings for your validator, then load it through your
shell or process supervisor. Configuration is env-driven
(`ditto/validator/config.py`); the worker fails fast at boot on anything missing
or malformed.

### Required

| Env | Meaning |
| --- | --- |
| `VALIDATOR_PLATFORM_API_URL` | Platform API base URL. |
| `VALIDATOR_HOTKEY` | Your validator hotkey (SS58); must match the loaded keypair. |
| `VALIDATOR_WALLET_NAME` + `VALIDATOR_WALLET_HOTKEY` or `VALIDATOR_MNEMONIC` | Signing source. Prefer wallet files; the mnemonic env is the container-friendly alternative. |
| `NETUID` | Subnet netuid (118 on finney). |
| `VALIDATOR_DITTOBENCH_API_URL` | Your co-located dittobench-api base URL. |

The locked model is served from a gateway configured on the dittobench-api
service, not the worker. See [Model gateway](#4-model-gateway). The validator
worker does not receive or forward model-provider credentials. Every validator
runs this scoring path and submits its own weights; these responsibilities
cannot be split across separate worker roles.

### Weight path (Pylon)

Weights are set through Pylon, a small service that owns the validator hotkey and
submits `put_weights` on chain. [Set up Pylon](#5-set-up-pylon) covers running it;
point the worker at it with:

| Env | Meaning |
| --- | --- |
| `PYLON_URL` | Base URL of your Pylon service (e.g. `http://localhost:8000`). |
| `PYLON_IDENTITY_NAME` | The Pylon identity holding this validator's hotkey. |
| `PYLON_IDENTITY_TOKEN` | The Pylon token, authorizing `put_weights`. |
| `PYLON_OPEN_ACCESS_TOKEN` | Same token; lets the worker self-check its permit. |

## 4. Model gateway

A validator runs dittobench-api, its `cmd/model-relay`, and a small local
embedding model. The relay keeps the Chutes key out of miner sandboxes and forces
every run onto the same model.

```sh
RELAY_API_KEY=cpk-... \
RELAY_MODEL=Qwen/Qwen3-32B-TEE \
PORT=11435 \
./model-relay
```

Configure dittobench-api (not this validator worker) with:

```sh
DITTOBENCH_MODEL_LOCK=1
HARNESS_MODEL=Qwen/Qwen3-32B-TEE
HARNESS_PROVIDER=chutes
HARNESS_GATEWAY_URL=http://host.docker.internal:11435
HARNESS_EMBED_URL=http://host.docker.internal:11434
```

Run Ollama on port 11434 with the embedding model named by dittobench-api's
model-lock configuration. The sandbox reaches both services through
`host.docker.internal`; its egress policy should allow the local relay and
embedding service, not direct LLM-provider access. Keep `RELAY_API_KEY` in a
secret manager. Self-hosting the chat model is useful for local practice but is
not fleet-standard because serving differences make scores less comparable.

## 5. Set up Pylon

Pylon is a small HTTP service that owns the validator hotkey and submits weights
on chain. The worker never signs a weight extrinsic itself; it hands its computed
weight vector to Pylon, and Pylon does the normalization, u16 conversion, UID
resolution, commit-reveal, and `version_key`, retrying until the extrinsic lands.
It persists in-flight submissions to a local database and resumes them across
restarts. Run one Pylon next to each worker.

Pylon ships as a Docker image (`backenddevelopersltd/bittensor-pylon`). It reads
`PYLON_`-prefixed env and mounts the validator wallet. You configure one
**identity**, a named wallet-plus-subnet pair with its own secret token; the
worker authenticates to that identity to write weights.

The root `docker-compose.yml` starts the complete validator stack: Pylon,
dittobench-api backed by an isolated rootless Docker daemon for sandbox builds,
and the ditto-subnet validator worker. A small internal proxy preserves sandbox
access to the model relay and embedder running on the physical host. Compose
reads the single `.env` created in section 3 and passes each service only the
values it needs. The Pylon settings name the wallet and one random token that guards
both open-access reads and the identity write. Reuse the same string for both;
only split them if you hand the read token to a separate read-only consumer.

Generate the token with OpenSSL:

```sh
openssl rand -base64 32
```

Store it in a secret manager; do not commit it. If you do split read from write,
run the command twice and use a different output for each token.

```sh
PYLON_BITTENSOR_NETWORK=finney
PYLON_OPEN_ACCESS_TOKEN=<random-token>

PYLON_IDENTITIES=["ditto"]
PYLON_ID_DITTO_WALLET_NAME=<coldkey-name>
PYLON_ID_DITTO_HOTKEY_NAME=<hotkey-name>
PYLON_ID_DITTO_NETUID=118
PYLON_ID_DITTO_TOKEN=<random-token>

PYLON_DATABASE_PATH=/data/pylon.db   # persist in-flight submissions
```

Bring up the stack from the repository root. Pylon and dittobench-api stay on
the private Compose network; the validator worker listens on no port.

```sh
docker compose up -d
```

Then point the worker at it (section 3 / your validator `.env`), reusing the
identity name and the same token:

```sh
PYLON_URL=http://localhost:8000
PYLON_IDENTITY_NAME=ditto
PYLON_IDENTITY_TOKEN=<the same PYLON_ID_DITTO_TOKEN>
PYLON_OPEN_ACCESS_TOKEN=<the same token>   # lets the worker self-check its permit
```

The identity hotkey must be the validator hotkey registered on SN118 with a
`validator_permit`; Pylon returns `403` on `put_weights` without the permit and
stake. Keep the token and the wallet in a secret manager. Pylon serves its
OpenAPI at `http://localhost:8000/schema/swagger` once running. Full service
reference: <https://github.com/bittensor-church/bittensor-pylon> (`docs/SERVICE.md`).

## 6. Run it

```sh
VALIDATOR_PLATFORM_API_URL=https://platform-api.heyditto.ai/ \
NETUID=118 \
VALIDATOR_HOTKEY=<ss58> \
VALIDATOR_WALLET_NAME=<coldkey-name> VALIDATOR_WALLET_HOTKEY=<hotkey-name> \
VALIDATOR_DITTOBENCH_API_URL=http://localhost:8080 \
PYLON_URL=http://<pylon-host> PYLON_IDENTITY_NAME=<name> PYLON_IDENTITY_TOKEN=<secret> \
uv run python -m ditto.validator
```

Run it under a supervisor (systemd, pm2) with restart-on-exit; the process drains
cleanly on SIGTERM/SIGINT. Run exactly one instance per hotkey; two instances
double-submit weights.

A healthy boot logs the weight mode (`Pylon identity`), then per sweep: queue
depth, per-agent `scored agent … composite=…` lines, and
`submitted weights for N miner(s)` when the epoch is due.

Boot-time self-checks:

- Missing or invalid env gives an immediate typed `ValidatorConfigError` (it
  never boots half-configured).
- No `validator_permit` on your hotkey: the worker scores normally but skips
  weight submission each epoch with a loud log line (the chain is the enforcer,
  the log line is the alarm).

## 7. Verify it's working

- Logs: `sweep complete: N agent(s) (weights set)` and no recurring
  `put_weights failed` lines.
- The public ledger: your signed scores appear under your `validator_hotkey` at
  `GET /api/v1/scoring/scores`.
- On chain: your hotkey's last-update block advances each epoch (`btcli` or
  metagraph inspection), and weights match the ledger fold.
- W&B dashboard (if enabled): sweep stats, leaderboard, and the weight vector per
  epoch.

## 8. Mechanism reference

Weights use a deterministic king-of-the-hill fold over the public score ledger.
A challenger dethrones the champion only after clearing the greater of the 5%
relative margin and the configured statistical error band. The champion receives
90% of weight; the next four distinct miners split the remaining 10%. First-seen
timestamps and duplicate detection prevent copied artifacts from displacing the
incumbent. The margin, tail size, champion share, and dethrone-z are frozen in
code (`ditto/validator/config.py`), not env-tunable, so every validator folds
identically. The implementation in `ditto/validator/weights.py` is the source of
truth.

## 9. Environment reference

These common knobs keep the defaults documented by the validator worker. The
consensus-critical KOTH values (margin, tail size, champion share, dethrone-z,
and confirmation seeds) are frozen in code (`ditto/validator/config.py`), not
env-tunable, so they are not listed here.

| Env | Meaning |
| --- | --- |
| `VALIDATOR_RUN_SIZE` (`full`) | dittobench run size. `full` is the production config; `small`/`medium` are for plumbing tests. |
| `VALIDATOR_SWEEP_SECONDS` (120) | Scoring-sweep cadence. |
| `VALIDATOR_EPOCH_SECONDS` (3600) | Weight-set cadence. The worker also honors the chain's `weights_rate_limit`, stretching to whichever is longer. |
| `VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS` (2400) | Hard cap per agent run (full builds are slow). |
| `VALIDATOR_DITTOBENCH_MOCK` (off) | Canned scores, no dittobench key needed; local plumbing only, never on a real network. |
| `VALIDATOR_LOG_LEVEL` (`INFO`) | Worker log level. |
| `WANDB_MODE` (`disabled`) | Set `online` (plus `WANDB_PROJECT`/`WANDB_ENTITY`) to publish the aggregate-only telemetry. |

## Code references

`ditto/validator/{__main__,config,worker,weights,signing,telemetry}.py` ·
`ditto/chain/client.py`
