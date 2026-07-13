# Running a Ditto (SN118) Validator

**As of 2026-07-12.** The k=3 multi-validator design is implemented: the platform
leases up to three tickets per submission and finalizes on the median of the
independent validators' scores (see "The k=3 model" below). Today only the team
validator runs, so this guide is how any independent validator joins. Run the
same stateless worker with your own registered hotkey and the platform shards
work to you.

---

## 1. What a validator does (and doesn't)

The validator is **one stateless Python process** (`python -m ditto.validator`)
that loops forever:

1. **Sweep** (every `VALIDATOR_SWEEP_SECONDS`, default 120s): pull agents in
   `evaluating` from the platform's `/validator/*` HTTP API.
2. **Score** each via the hosted **dittobench-api** (by presigned tarball URL,
   with your own OpenRouter key), sr25519-**sign** the composite, and POST it
   back to the platform's public score ledger.
3. **Set weights** (every `VALIDATOR_EPOCH_SECONDS`, default 3600s): re-read
   the durable ledger, fold it into the deterministic KOTH+ATH weight vector,
   and submit on chain.

What it does **not** do:

- **No database, no local state.** The queue and the score ledger live behind
  the platform API; a validator can be killed and restarted at any time and
  loses nothing. There is nothing to back up.
- **No GPU, no model hosting.** The benchmark itself (docker build, seeded
  cases, deterministic grading) runs in the hosted dittobench service; the
  validator orchestrates over HTTP.
- **No champion selection server-side.** The weight fold
  (`ditto/validator/weights.py`) is a pure function of the public ledger —
  every honest validator computes the identical vector, and Yuma consensus
  clips deviators.

### The k=3 model

Scoring is decentralized across independent validators, no central scorer:

- The platform issues a leased ticket to a validator that asks for work
  (`POST /api/v1/validator/job`), capped at three live tickets per submission
  (`SCORING_QUORUM=3`). Most polls return "no job" once a submission's three
  slots are filled; a ticket that is not scored before its deadline expires and
  the slot re-opens for another validator.
- Each validator scores independently and posts one signed score. When a
  submission has three scores the platform finalizes it on the **median**, so no
  single validator decides a score and an outlier cannot move the result.
- Every validator then folds the same public median-aggregated ledger with the
  identical KOTH+ATH function and sets its own weights; chain Yuma consensus
  combines them. Bringing up more independent validators (each a distinct
  registered hotkey) is exactly how scoring decentralizes. No coordination is
  needed beyond the shared platform and the network-wide mechanism knobs.

## 2. Requirements

| What | Why |
| --- | --- |
| Linux host, 1–2 vCPU, 2 GB RAM | The worker is HTTP + signing only; it is deliberately light. |
| Python 3.11+ / [`uv`](https://docs.astral.sh/uv/) | `uv sync` installs the pinned environment. |
| A **hotkey registered on SN118** with a `validator_permit` | The chain only accepts weights from permitted validators (stake above the permit threshold). |
| The hotkey's signing source (wallet files or mnemonic) | Signs score reports and (SDK path) the `set_weights` extrinsic. **The coldkey is never needed on the box.** |
| An **OpenRouter API key** | BYOK for the LLM-judge portion of each dittobench run. |
| Reachability to the platform API, dittobench-api, and a chain endpoint (Pylon or a subtensor node) | All communication is outbound HTTP/WebSocket; the worker listens on nothing. |

Keep the mnemonic / wallet key and the OpenRouter key in a secret manager and
inject them as env — they must never be logged or committed. The validator
hotkey (an SS58 address) is public.

## 3. Install & configure

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
```

Configuration is entirely env-driven (`ditto/validator/config.py`); the worker
fails fast at boot on anything missing or malformed.

### Required

| Env | Meaning |
| --- | --- |
| `VALIDATOR_PLATFORM_API_URL` | Platform API base URL. |
| `VALIDATOR_HOTKEY` | Your validator hotkey (SS58); must match the loaded keypair. |
| `VALIDATOR_WALLET_NAME` + `VALIDATOR_WALLET_HOTKEY` **or** `VALIDATOR_MNEMONIC` | Signing source. Prefer wallet files; the mnemonic env is the container-friendly alternative. |
| `NETUID` | Subnet netuid (118 on finney; 3 on the dev localnet). |
| `VALIDATOR_DITTOBENCH_API_URL` | Hosted dittobench-api base URL. |
| `VALIDATOR_OPENROUTER_KEY` | Your LLM-judge key (secret). |

### Chain / weight path (pick one)

| Env | Meaning |
| --- | --- |
| `PYLON_URL` + `PYLON_IDENTITY_NAME` + `PYLON_IDENTITY_TOKEN` | **Production path:** weights via Pylon identity `put_weights` (Pylon handles normalization, u16, UID resolution, commit-reveal, version_key). |
| `VALIDATOR_USE_SDK_WEIGHTS=1` + `SUBTENSOR_NETWORK` | **Fallback/localnet path:** weights via `bittensor.Subtensor.set_weights`, hotkey-signed. `SUBTENSOR_NETWORK` takes `finney`, `test`, `local`, or a raw `ws://` endpoint. |

### Common knobs (defaults in parentheses)

| Env | Meaning |
| --- | --- |
| `VALIDATOR_RUN_SIZE` (`full`) | dittobench run size. `full` is the production config; `small`/`medium` are for plumbing tests. |
| `VALIDATOR_SWEEP_SECONDS` (120) | Scoring-sweep cadence (queue-drain latency). |
| `VALIDATOR_EPOCH_SECONDS` (3600) | Weight-set cadence. Align to the target network's tempo / `weights_rate_limit`. |
| `VALIDATOR_KOTH_MARGIN` (0.01) / `VALIDATOR_KOTH_TAIL_SIZE` (4) / `VALIDATOR_KOTH_CHAMPION_SHARE` (0.9) | The consensus-critical mechanism knobs. **Every validator on a network must run identical values** or Yuma clips you. Do not tune unilaterally. |
| `VALIDATOR_WEIGHT_VERSION_KEY` (package spec version) | Mechanism version stamped on SDK-path `set_weights`; must also agree network-wide. |
| `VALIDATOR_REQUIRE_COMMIT_REVEAL` (off) | Cutover guard. When set, the worker logs an error each weight-set if the chain reports commit-reveal **off** (weights would be front-runnable) — it still submits. Set on finney; leave off on the localnet. |
| `VALIDATOR_DITTOBENCH_TIMEOUT_SECONDS` (2400) | Hard cap per agent run (full builds are slow). |
| `VALIDATOR_DITTOBENCH_MOCK` (off) | Canned scores, no dittobench/LLM key needed — local plumbing only, never on a real network. |
| `VALIDATOR_LOG_LEVEL` (`INFO`) | Worker log level. |
| `WANDB_MODE` (`disabled`) | Set `online` (+ `WANDB_PROJECT`/`WANDB_ENTITY`) to publish the public aggregate-only telemetry. |

## 4. Run it

```sh
VALIDATOR_PLATFORM_API_URL=https://<platform-host> \
NETUID=118 \
VALIDATOR_HOTKEY=<ss58> \
VALIDATOR_WALLET_NAME=<coldkey-name> VALIDATOR_WALLET_HOTKEY=<hotkey-name> \
VALIDATOR_DITTOBENCH_API_URL=https://<dittobench-host> \
VALIDATOR_OPENROUTER_KEY=<secret> \
PYLON_URL=http://<pylon-host> PYLON_IDENTITY_NAME=<name> PYLON_IDENTITY_TOKEN=<secret> \
uv run python -m ditto.validator
```

Run it under a supervisor (systemd / pm2) with restart-on-exit; the process
drains cleanly on SIGTERM/SIGINT. **Exactly one instance per hotkey** — two
instances double-submit weights.

A healthy boot logs the weight mode (`Pylon identity` or `bittensor SDK`),
then per sweep: queue depth, per-agent `scored agent … composite=…` lines, and
`submitted weights for N miner(s)` when the epoch is due.

Boot-time self-checks worth knowing:

- Missing/invalid env → immediate typed `ValidatorConfigError` (never boots
  half-configured).
- No `validator_permit` on your hotkey → the worker scores normally but
  **skips weight submission loudly** each epoch (fail-open on flaky reads —
  the chain is the enforcer, the log line is the alarm).

## 5. Verify it's working

- **Logs:** `sweep complete: N agent(s) (weights set)` and no recurring
  `put_weights failed` lines.
- **The public ledger:** your signed scores appear under your
  `validator_hotkey` at `GET /api/v1/scoring/scores` on the platform.
- **On chain:** your hotkey's last-update block advances each epoch
  (`btcli`/metagraph inspection), and weights match the ledger fold.
- **W&B dashboard** (if enabled): sweep stats, leaderboard, and the weight
  vector per epoch.

## 6. Current status & caveats (2026-07-07)

- **k=3 multi-validator is implemented; deployment is the remaining step.** The
  platform leases up to three tickets per submission, stores one signed score
  per `(agent, validator)`, and finalizes on the median (the `/validator/job` /
  `/agent/{id}/score` endpoints are the shipped names). Today
  only the subnet owner's validator runs, so agents currently get one score.
  Bringing up >=3 independent validators, each a distinct SN118 hotkey with a
  `validator_permit`, is what decentralizes scoring; there is no extra
  registration flow beyond that permitted hotkey. See section 7 for a localnet
  three-validator run.
- **Pylon identity-write is not yet provisioned**; the SDK fallback is the
  proven path on the dev chain.
- **Commit-reveal is off on dev** and on for production.
  Under commit-reveal **v3** there is no separate reveal call — `set_weights` and
  Pylon do the timelock commit and the chain auto-reveals. The worker reads the
  `CommitRevealWeightsEnabled` hyperparameter and **logs the mode** each
  weight-set; set `VALIDATOR_REQUIRE_COMMIT_REVEAL=1` on a network where you
  expect commit-reveal on, and it logs loudly (still submitting) if the chain
  reports it off.
- Mechanism knobs (margin/tail/share, version_key) are team-locked values;
  changing them unilaterally makes your weights diverge from consensus.

## 7. Localnet: three validators (prove k=3 consensus)

To exercise the full 3-scores to median to finalize to weights path on the dev
localnet (netuid 3), run three workers with three distinct registered hotkeys:

1. Register three hotkeys on netuid 3, each staked past the `validator_permit`
   threshold (e.g. three wallet hotkeys, or dev keys like `//val1`, `//val2`,
   `//val3`).
2. Copy `scripts/validator.env.example` to `val1.env`, `val2.env`, `val3.env`.
   In each: point `VALIDATOR_PLATFORM_API_URL` / `VALIDATOR_DITTOBENCH_API_URL`
   at the dev services, set that worker's `VALIDATOR_HOTKEY` + signing source,
   `NETUID=3`, and the localnet weight path (`VALIDATOR_USE_SDK_WEIGHTS=1`,
   `SUBTENSOR_NETWORK=<ws://localnet>`). Keep the KOTH knobs identical in all
   three.
3. Start each in its own process:
   `./scripts/run-validator.sh val1.env` (then `val2.env`, `val3.env`).
4. Submit one agent through the miner path and watch: each validator's sweep
   logs a `scored agent ... composite=...` line; the platform issues at most
   three tickets (a fourth poll gets "no job"); at the third score the platform
   finalizes on the median (`GET /api/v1/public/agent/{id}/scores` shows all
   three validators + the median); and each validator's fold resolves the
   champion to the miner's UID on chain.

This is the O-VAL localnet proof for the F-MV milestone. A hotkey without a
`validator_permit` still scores but skips weight submission (loud log), so give
each localnet hotkey enough stake.

### Sources (this repo)

`CLAUDE.md` · `docs/ROAD-TO-PRODUCTION.md` · `docs/incentive-mechanism.md` ·
`ditto/validator/{__main__,config,worker,weights,signing,telemetry}.py` ·
`ditto/chain/client.py`
