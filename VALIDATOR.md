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

| Understand | Set up | Operate and reference |
| --- | --- | --- |
| [What validators do](#1-what-a-validator-does-and-doesnt) | [Requirements](#2-requirements) | [Install and configure](#3-install-and-configure) |
| [Model gateway](#4-model-gateway) | [Run](#5-run-it) | [Verify](#6-verify-its-working) |
| [Three-validator localnet](#7-localnet-three-validators-prove-k3-consensus) | [Mechanism](#8-mechanism-reference) | [Environment reference](#9-environment-reference) |

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
| `NETUID` | Subnet netuid (118 on finney; 3 on the dev localnet). |
| `VALIDATOR_DITTOBENCH_API_URL` | Your co-located dittobench-api base URL. |

The locked model is served from a gateway configured on the dittobench-api
service, not the worker. See [Model gateway](#4-model-gateway). The validator
worker does not need an LLM key.

### Pylon weight setting

| Env | Meaning |
| --- | --- |
| `PYLON_URL` + `PYLON_IDENTITY_NAME` + `PYLON_IDENTITY_TOKEN` | Required validator path: weights via Pylon identity `put_weights`. Pylon improves weight submission by handling normalization, u16 conversion, UID resolution, commit-reveal, and `version_key`. |

Validators use Pylon; direct SDK weight submission is not an operator choice.
`VALIDATOR_USE_SDK_WEIGHTS=1` exists only for the development localnet, where
Pylon identity writes are not available.

## 4. Model gateway

Scoring validators run dittobench-api, its `cmd/model-relay`, and a small local
embedding model. Weights-only validators can skip this section. The relay keeps
the Chutes key out of miner sandboxes and forces every run onto the same model.

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

## 5. Run it

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

A healthy validator boot logs `Pylon identity`, then per sweep: queue depth,
per-agent `scored agent … composite=…` lines, and `submitted weights for N
miner(s)` when the epoch is due. Development-localnet workers instead log the
SDK fallback.

Boot-time self-checks:

- Missing or invalid env gives an immediate typed `ValidatorConfigError` (it
  never boots half-configured).
- No `validator_permit` on your hotkey: the worker scores normally but skips
  weight submission each epoch with a loud log line (the chain is the enforcer,
  the log line is the alarm).

## 6. Verify it's working

- Logs: `sweep complete: N agent(s) (weights set)` and no recurring
  `put_weights failed` lines.
- The public ledger: your signed scores appear under your `validator_hotkey` at
  `GET /api/v1/scoring/scores`.
- On chain: your hotkey's last-update block advances each epoch (`btcli` or
  metagraph inspection), and weights match the ledger fold.
- W&B dashboard (if enabled): sweep stats, leaderboard, and the weight vector per
  epoch.

## 7. Localnet: three validators (prove k=3 consensus)

To exercise the full three-scores to median to finalize to weights path on the
dev localnet (netuid 3), run three workers with three distinct registered
hotkeys:

1. Register three hotkeys on netuid 3, each staked past the `validator_permit`
   threshold (three wallet hotkeys, or dev keys like `//val1`, `//val2`,
   `//val3`).
2. Copy `scripts/validator.env.example` to `val1.env`, `val2.env`, `val3.env`. In
   each: point `VALIDATOR_PLATFORM_API_URL` and `VALIDATOR_DITTOBENCH_API_URL` at
   the dev services, set that worker's `VALIDATOR_HOTKEY` and signing source,
   `NETUID=3`, and the development-only localnet fallback (`VALIDATOR_USE_SDK_WEIGHTS=1`,
   `SUBTENSOR_NETWORK=<ws://localnet>`). Keep the KOTH knobs identical in all
   three.
3. Start each in its own process: `./scripts/run-validator.sh val1.env` (then
   `val2.env`, `val3.env`).
4. Submit one agent through the miner path and watch: each validator's sweep logs
   a `scored agent … composite=…` line; the platform issues at most three tickets
   (a fourth poll gets no job); at the third score the platform finalizes on the
   median (`GET /api/v1/public/agent/{id}/scores` shows all three validators and
   the median); and each validator's fold resolves the champion to the miner's UID
   on chain.

A hotkey without a `validator_permit` still scores but skips weight submission
(loud log), so give each localnet hotkey enough stake.

## 8. Mechanism reference

Weights use a deterministic king-of-the-hill fold over the public score ledger.
A challenger dethrones the champion only after clearing the greater of the 5%
relative margin and the configured statistical error band. The champion receives
90% of weight; the next four distinct miners split the remaining 10%. First-seen
timestamps and duplicate detection prevent copied artifacts from displacing the
incumbent. Keep every `VALIDATOR_KOTH_*` setting identical network-wide so Yuma
consensus converges. The implementation in `ditto/validator/weights.py` is the
source of truth.

## 9. Environment reference

These common knobs retain the defaults documented by the validator worker.
Consensus-critical values must remain identical across the network.

| Env | Meaning |
| --- | --- |
| `VALIDATOR_RUN_SIZE` (`full`) | dittobench run size. `full` is the production config; `small`/`medium` are for plumbing tests. |
| `VALIDATOR_SWEEP_SECONDS` (120) | Scoring-sweep cadence. |
| `VALIDATOR_EPOCH_SECONDS` (3600) | Weight-set cadence. The worker also honors the chain's `weights_rate_limit`, stretching to whichever is longer. |
| `VALIDATOR_KOTH_MARGIN` (0.05) / `VALIDATOR_KOTH_TAIL_SIZE` (4) / `VALIDATOR_KOTH_CHAMPION_SHARE` (0.9) / `VALIDATOR_KOTH_DETHRONE_Z` (1.64) | Consensus-critical mechanism knobs. Every validator on a network must run identical values or Yuma clips you. Do not tune unilaterally. |
| `VALIDATOR_WEIGHT_VERSION_KEY` (package version) | Development-localnet mechanism version stamped on SDK-path `set_weights`; must agree across localnet workers. Production validators use Pylon. |
| `VALIDATOR_REQUIRE_COMMIT_REVEAL` (off) | Cutover guard. When set, the worker logs an error each weight-set if the chain reports commit-reveal off (weights would be front-runnable); it still submits. Set on finney; leave off on the localnet. |
| `VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS` (2400) | Hard cap per agent run (full builds are slow). |
| `VALIDATOR_DITTOBENCH_MOCK` (off) | Canned scores, no dittobench key needed; local plumbing only, never on a real network. |
| `VALIDATOR_LOG_LEVEL` (`INFO`) | Worker log level. |
| `WANDB_MODE` (`disabled`) | Set `online` (plus `WANDB_PROJECT`/`WANDB_ENTITY`) to publish the aggregate-only telemetry. |

## Code references

`ditto/validator/{__main__,config,worker,weights,signing,telemetry}.py` ·
`ditto/chain/client.py`
