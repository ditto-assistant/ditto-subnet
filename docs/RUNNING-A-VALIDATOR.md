# Running a validator

There is one validator type. The worker (`python -m ditto.validator`) runs two
duties in one process: score agents when the platform leases it a ticket (at
most 3 leases per agent, so scoring rotates across the fleet), and set weights
every interval regardless of whether it scored. The
`VALIDATOR_ENABLE_SCORING` / `VALIDATOR_ENABLE_WEIGHTS` flags can split the
duties for ops or testing, but the fleet runs both on (the default).

## The weights duty

Every validator, every epoch:

1. `GET /api/v1/scoring/scores`: read the best-score-per-miner ledger.
2. Fold it into the weight vector with `compute_weights` (KOTH champion + ATH
   dethroning band). This is a pure deterministic function of the ledger, so
   every validator converges under Yuma consensus.
3. Submit weights on chain (Pylon identity, or the bittensor SDK on localnet).

If the ledger read fails, it leaves the current on-chain weights untouched for the
epoch rather than zeroing anyone; the next epoch recovers from the durable ledger.

Required env (both duties; see the scoring section for the rest):

```
VALIDATOR_PLATFORM_API_URL=https://platform-api.heyditto.ai
VALIDATOR_HOTKEY=<your SS58 hotkey>
VALIDATOR_MNEMONIC=<hotkey mnemonic>        # or VALIDATOR_WALLET_NAME + _HOTKEY
NETUID=118
# Weight sink, Pylon identity (production):
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

### What the fold trusts

The KOTH fold reads the platform's score ledger, which is self-verifying:
every entry is signed by the scoring validator's on-chain hotkey and verified
by the platform at write time, and the deterministic fold re-derives the ATH
winner from those signed scores rather than trusting a platform-computed flag.
Since scoring is judge-free, anyone can additionally re-grade a published
transcript from the public dittobench-datagen module.

## The scoring duty

Every validator polls for tickets each sweep and scores through its co-located
dittobench-api instance.

Additional env on top of the weights duty's:

```
VALIDATOR_DITTOBENCH_API_URL=http://localhost:8080   # your co-located engine
VALIDATOR_RUN_SIZE=full
VALIDATOR_OPENROUTER_KEY=...                 # legacy only: needed when the model lock is off
```

### Hosting the model gateway

Under v2 the scorer runs the harness against one locked open-weight model
served from a gateway; scoring itself is judge-free and deterministic, so no
judge model and no LLM key are needed. The locked model is Qwen3-32B: one
24 GB GPU self-hosted (Ollama/vLLM), or zero GPUs via the model-relay backed by
Chutes' TEE-served catalog. See
[VALIDATOR-MODEL-HOSTING.md](VALIDATOR-MODEL-HOSTING.md) for hardware sizing,
gateway options, artifact pinning, and the env wiring.

## Cadence

- `VALIDATOR_SWEEP_SECONDS` (default 120): how often the worker polls for tickets.
- `VALIDATOR_EPOCH_SECONDS` (default 3600): the minimum weight-set interval. The
  worker also reads the subnet's on-chain `weights_rate_limit` each epoch and
  stretches the effective cadence to whichever is longer, so it never fights the
  chain's rate limiter.

## Observability

Logs (INFO) report each queue sweep, per-agent scores, ledger folds, and weight
submissions. The startup line reports the active roles (`validator roles:
scoring+weights`) and weight mode. Optional wandb telemetry publishes
aggregate-only sweep stats (leaderboard, weights) when enabled.
