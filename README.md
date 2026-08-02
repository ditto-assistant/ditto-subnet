# Ditto Subnet (Bittensor SN118)

A Bittensor subnet that incentivizes agent memory harnesses. Miners submit a Docker build context
that serves the public harness HTTP contract; the implementation may use Rust, Python, TypeScript,
Go, or any other language. Validators run each screened image in an isolated sandbox and score it
on DittoBench (tool-calling and memory recall).
When eligible miners exist, 100% of miner emission follows the king-of-the-hill ranking; with no
eligible miners, 100% is burned.

This monorepo holds the miner CLI, validator worker, and DittoBench scoring
engine. Other public Ditto stack components currently live in:

- [`ditto-platform`](https://github.com/ditto-assistant/ditto-platform): the public API server, live dashboard, submission coordinator, and score ledger.
- [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness): the Rust reference library used by the starter kit.
- [`dittobench-starter-kit`](https://github.com/ditto-assistant/dittobench-starter-kit): the miner starting point plus the offline practice loop.
- [`services/dittobench-api`](services/dittobench-api): the scoring engine each validator runs.
- [`dittobench-datagen`](https://github.com/ditto-assistant/dittobench-datagen): the dataset generator and judge-free grader.
- [`ditto-screener`](https://github.com/ditto-assistant/ditto-screener): the platform-operated build and health gate plus the shared screening protocol.

## Layout
- `ditto/miner_cli/`: the `ditto` CLI: submit an agent, poll status, pre-flight a tarball.
- `ditto/validator/`: the validator worker (`python -m ditto.validator`): pull agents from the
  platform, score them via dittobench, set weights on chain via Pylon (the
  identity-based weight-setting service).
- `ditto/api_models/`: Pydantic wire shapes shared with the platform (the HTTP contract).
- `ditto/chain/`: Pylon-backed `ChainClient` (used by the validator to set weights).
- `services/dittobench-api/`: the Go scorer used by validators and hosted practice.
- `services/screener-orchestrator/`: Targon-first screener capacity and build control.
- `apps/platform/`: the subnet API, durable queue, dashboard, and control plane.
- `apps/backroom/`: the public-source SN118 operations console.
- `workers/screener/`: the provider-neutral screening worker runtime.
- `packages/ditto-screening-protocol/`: shared screening wire and signing contract.

Submission screening is platform-operated and is not installed or deployed by
this package. Miner and validator clients import the lifecycle contract from
the public `ditto-screening-protocol` package in `ditto-screener`.

## Operator guides

- [Mine on SN118](docs/MINER.md): prepare, verify, submit, and track an agent.
- [Link rotated miner wallets](docs/OWNER-LINKS.md): prove that two hotkeys
  belong to the same operator after a wallet rotation.
- [Validate SN118](docs/VALIDATOR.md): deploy, verify, and operate the complete validator stack.

## Development quickstart
```sh
uv sync
make test          # unit tests
```

## Miner CLI summary
Installed as the `ditto` console script (`pyproject` `[project.scripts]`):
```sh
ditto --network <finney|test|local> [--chain-endpoint ws://…] upload \
  --path <agent.tar.gz> --name <name> --coldkey <coldkey> --hotkey <hotkey> [-y]
ditto status <agent_id>
ditto verify --path <agent.tar.gz>      # pre-flight checks only; no chain/API calls
```
`--network` couples the API URL + subtensor network from a locked table (can't desync);
`--chain-endpoint` overrides only the chain target (e.g. a hosted local subtensor) while keeping the
`--network` API URL. See [MINER.md](docs/MINER.md) for the full workflow.

## Validator quickstart

```sh
cp .env.example .env
# Fill in the wallet names, validator hotkey, Pylon token, and shared W&B key.
# Set WANDB_MODE=disabled instead if you opt out of aggregate telemetry.
./scripts/validator-compose.sh config --quiet
./scripts/validator-compose.sh up -d --build
./scripts/validator-compose.sh ps
```

The root Compose stack runs the worker, Pylon, scorer, and isolated Docker
sandbox from one `.env`. Ticket-scoped chat and embedding inference is served
by the platform; validators carry no provider credential. SN118 and the production scoring and weight mechanism
are locked in code rather than configured by operators. The idle (no eligible miners) burn
vector uses Subtensor's owner-associated burn path; it is not paid to the subnet owner. See
[VALIDATOR.md](docs/VALIDATOR.md) for first deployment, health checks, and upgrades.

The validator reports coarse public system health through its signed heartbeat.
The screener owns its separate reporter. Neither needs a new operator setting
or secret; hostname, IP, paths, container names/images, and env values are not
collected.

## Make targets
- `make lint`: `ruff format --check` + `ruff check`
- `make format`: `ruff format` + `ruff check --fix`
- `make typecheck`: `mypy ditto/`
- `make test`: the default `pytest` suite
