# Running a validator

The validator worker (`python -m ditto.validator`) runs one or both halves of the
scoring loop, selected by two role flags. This lets one deployment be the central
scorer, another be an independent weights-only validator, or (the default) both
in one process.

| Role | `VALIDATOR_ENABLE_SCORING` | `VALIDATOR_ENABLE_WEIGHTS` | Runs |
|---|---|---|---|
| Central scorer | `true` | `false` | Pulls the `evaluating` queue, scores each agent via dittobench-api, persists signed scores, re-scores stale champions. Never touches the chain. |
| Independent validator | `false` | `true` | Reads the canonical ledger, folds KOTH+ATH weights, sets them on chain. Never scores or sees the oracle. |
| Combined (default) | `true` | `true` | Both, in one process. The historical single-node setup. |

At least one must be `true`.

## Independent (weights-only) validator

This is the role external operators run. It consumes the platform's
centrally-computed score ledger and sets weights. It needs **no** dittobench-api
URL, **no** OpenRouter key, and never handles benchmark answers.

What it does each epoch:

1. `GET /api/v1/scoring/scores` — read the best-score-per-miner ledger.
2. Fold it into the weight vector with `compute_weights` (KOTH champion + ATH
   dethroning band). This is a pure deterministic function of the ledger, so
   every validator converges under Yuma consensus.
3. Submit weights on chain (Pylon identity, or the bittensor SDK on localnet).

If the ledger read fails, it leaves the current on-chain weights untouched for the
epoch rather than zeroing anyone; the next epoch recovers from the durable ledger.

Required env:

```
VALIDATOR_ENABLE_SCORING=false
VALIDATOR_ENABLE_WEIGHTS=true
VALIDATOR_PLATFORM_API_URL=https://platform-api.heyditto.ai
VALIDATOR_HOTKEY=<your SS58 hotkey>
VALIDATOR_MNEMONIC=<hotkey mnemonic>        # or VALIDATOR_WALLET_NAME + _HOTKEY
NETUID=118
# Weight sink — Pylon identity (production):
PYLON_URL=...
PYLON_IDENTITY_NAME=...
PYLON_IDENTITY_TOKEN=...
PYLON_OPEN_ACCESS_TOKEN=...                  # lets the permit self-check run
# ...or the SDK fallback (localnet): VALIDATOR_USE_SDK_WEIGHTS=true
SUBTENSOR_NETWORK=finney
```

The KOTH/ATH knobs (`VALIDATOR_KOTH_MARGIN`, `_TAIL_SIZE`, `_CHAMPION_SHARE`,
`_DETHRONE_Z`) are **consensus knobs**: every validator must run the same values
or Yuma clips the deviator. Leave them at their defaults unless the team announces
a change.

### What it trusts

An independent validator trusts the platform's score ledger. Scores are computed
by the central scorer, signed with the scorer's on-chain hotkey, and verified by
the platform at write time. The benchmark generator and answer keys never leave
the private scoring service; a validator only ever sees composites, never the
dataset or the oracle.

## Central scorer

Run by the subnet operator as a single instance. It scores the queue and persists
signed scores; it sets no weights.

Required env:

```
VALIDATOR_ENABLE_SCORING=true
VALIDATOR_ENABLE_WEIGHTS=false
VALIDATOR_PLATFORM_API_URL=...
VALIDATOR_DITTOBENCH_API_URL=...             # the private scoring service
VALIDATOR_OPENROUTER_KEY=...                 # forwarded to the run_size pipeline
VALIDATOR_RUN_SIZE=full
VALIDATOR_HOTKEY=<scorer SS58 hotkey>        # signs each score
VALIDATOR_MNEMONIC=...                        # or a wallet
NETUID=118
```

No Pylon identity is required (it sets no weights).

### Hosting the model gateway

Under v2 the scorer runs the harness against one locked open-weight model and
should grade with a self-hosted judge, both served from a local Ollama/vLLM
gateway rather than a hosted API. This is what makes scores comparable and
judging reproducible across the k=3 validators. See
[VALIDATOR-MODEL-HOSTING.md](VALIDATOR-MODEL-HOSTING.md) for the full setup
(gateway install, determinism knobs, and the env wiring).

## Cadence

- `VALIDATOR_SWEEP_SECONDS` (default 120): how often the scorer drains the queue.
- `VALIDATOR_EPOCH_SECONDS` (default 3600): the minimum weight-set interval. The
  worker also reads the subnet's on-chain `weights_rate_limit` each epoch and
  stretches the effective cadence to whichever is longer, so it never fights the
  chain's rate limiter.

## Observability

Logs (INFO) report each queue sweep, per-agent scores, ledger folds, and weight
submissions. The startup line reports the active roles (`validator roles:
scoring+weights`) and weight mode. Optional wandb telemetry publishes
aggregate-only sweep stats (leaderboard, weights) when enabled.
