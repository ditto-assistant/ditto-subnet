# CLAUDE.md

Guidance for Claude Code (and humans) working in ditto-subnet, the miner-side
CLI and the validator daemon for Bittensor Subnet 118. Read this before making
changes.

## What this repo is

The **client side** of SN118: the miner CLI (`ditto/miner_cli`) that uploads
agents, and the validator worker (`ditto/validator`) that scores them and sets
chain weights. The API server / platform (DB, payment verifier, pricing, S3,
the `/validator/*` and `/upload/*` endpoints) lives in
[`ditto-platform`](https://github.com/ditto-assistant/ditto-platform) (a private
repo); the reference memory harness lives in
[`ditto-harness`](https://github.com/ditto-assistant/ditto-harness).

This repo does not run an API server and has no database. Both the miner CLI and
the validator talk to the platform only over HTTP, and to the chain only via
Pylon and the bittensor SDK. `ditto/api_models/` here is a thin,
hand-maintained copy of the platform's wire shapes; keep it in sync with the
platform's `api_models/` (CI guards this). The platform's OpenAPI schema is the
contract; there is no shared package between the repos.

## Architecture in one paragraph

`ditto/validator` is the validator daemon, started as a standalone process with
`python -m ditto.validator` (`__main__.py` builds config + clients, installs a
SIGTERM/SIGINT drain, and runs `ValidatorWorker.run_forever`). Each sweep
(`worker.py`) pulls agents awaiting evaluation from the platform's `/validator/*`
API (`platform.py`), scores each through its co-located dittobench-api by
presigned tarball URL (`dittobench.py`, `run_size=full`), signs
the score (`signing.py`, sr25519 over `f"{hotkey}:{run_id}"`), reports it back,
then computes a weight vector (`weights.py`) and sets it on chain via
`ditto/chain` (Pylon identity `put_weights`). `ditto/miner_cli` is the miner-side
CLI (`ditto upload`/`status`/`verify`): it bundles + validates a tarball, pays
the eval fee on chain, and uploads to the platform. `ditto/chain` is the shared
Pylon-backed `ChainClient` (open-access reads vs. identity writes).

## Conventions (match the existing code)

- **Pydantic only in `ditto/api_models`** (the wire-shape copy). Everything
  internal (configs, value objects, results) uses `@dataclass(frozen=True)`.
- **Config is env-driven dataclasses** with `parse_*_from_env()` builders and a
  `check_*_config()` validator that fails fast with a typed `*ConfigError`. Never
  boot with a placeholder or a half-set signing source. See
  `validator/config.py` for the pattern (`_require`, `signing_source_present`).
- **The validator is stateless.** No DB, no local persistence of scores. State
  the validator needs (the queue, the score ledger) lives on the platform and is
  fetched over HTTP each sweep. Do not add a `ditto/db` package or import one.
- **Async everywhere**: httpx for HTTP, the async `ChainClient`. One agent
  failing to score is logged and skipped; it must never stall the sweep or block
  weight-setting for the other miners.
- **Secrets never get logged.** The validator hotkey is public (an SS58
  address); the signing mnemonic / wallet key and any gateway key are secrets:
  load them from the environment (Secret Manager in prod) and never log them.

## Commands

```sh
uv sync                              # install deps
make lint typecheck test             # ruff + mypy + pytest (run before every PR)

# run the validator worker (local plumbing, mock the bench, no key needed):
VALIDATOR_PLATFORM_API_URL=http://localhost:8000 NETUID=118 \
  VALIDATOR_DITTOBENCH_MOCK=1 \
  VALIDATOR_WALLET_NAME=<ck> VALIDATOR_WALLET_HOTKEY=<hk> VALIDATOR_HOTKEY=<ss58> \
  PYLON_URL=http://localhost:8001 SUBTENSOR_NETWORK=<ws://…> \
  python -m ditto.validator

# run the miner CLI against a local platform + a chain endpoint:
ditto --network local --chain-endpoint <ws://…> \
  upload --path <agent.tar.gz> --name <name> --coldkey <ck> --hotkey <hk> -y
```

The platform stack (Postgres/MinIO/Pylon/API) that this worker talks to runs
from the `ditto-platform` repo, not here.

## Testing

- `pytest` markers `slow`, `integration`, `localnet`, `e2e` are excluded by
  default. Validator unit tests stub the platform/dittobench/chain clients;
  there is no DB fixture because the validator has no DB.
- Put unit tests next to the package they cover under `ditto/tests/<package>`
  (`ditto/tests/validator`, `ditto/tests/miner_cli`).

## Gotchas

- **`VALIDATOR_DITTOBENCH_MOCK=1`** returns a canned `ScoreReport` and skips the
  real dittobench-api call; use it for local plumbing. When it is off,
  `VALIDATOR_DITTOBENCH_API_URL` is required at boot (fail-fast);
  model-provider credentials belong to the model relay, not the validator worker.
- The worker uses the platform's lease-based **k=3** scoring contract:
  `request_job` leases a `/validator/job` ticket, `/agent/{id}/artifact` fetches
  the submission, `submit_score` posts one signed score to the public ledger
  (`/scoring/scores`), and every validator recomputes **replicated deterministic
  weights** over that ledger. There is no shared wire package, so a request or
  response shape change must land in both repos (see the copy note below).
- `ditto/api_models/validator.py` is a **copy** of the platform's wire models.
  If you change a validator request/response shape, change it in both repos.

## Branching

`main` (release) ← `dev` (integration) ← `name/topic` feature branches. PRs into
`dev`. Do not commit directly to `main`.
