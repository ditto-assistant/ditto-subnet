# Ditto Subnet (Bittensor SN118)

A Bittensor subnet that incentivizes agent memory harnesses. Miners submit a Rust crate that
depends on the `ditto-harness` library and overrides its extension traits; validators run each
submission in an isolated sandbox and score it on DittoBench (tool-calling and memory recall).
Emissions concentrate on the king-of-the-hill champion, with a participation tail.

This repo holds the miner CLI and the validator worker. The platform API server lives in
`ditto-platform` (the private coordinator). The rest of the stack is public:

- [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness): the reference memory harness your crate builds on.
- [`dittobench-starter-kit`](https://github.com/ditto-assistant/dittobench-starter-kit): the miner starting point plus the offline practice loop.
- [`dittobench-api`](https://github.com/ditto-assistant/dittobench-api): the scoring engine each validator runs.
- [`dittobench-datagen`](https://github.com/ditto-assistant/dittobench-datagen): the dataset generator and judge-free grader.

## Layout
- `ditto/miner_cli/`: the `ditto` CLI: submit an agent, poll status, pre-flight a tarball.
- `ditto/validator/`: the validator worker (`python -m ditto.validator`): pull agents from the
  platform, score them via dittobench, set weights on chain via Pylon (the
  identity-based weight-setting service).
- `ditto/api_models/`: Pydantic wire shapes shared with the platform (the HTTP contract).
- `ditto/chain/`: Pylon-backed `ChainClient` (used by the validator to set weights).

## Operator guides

- [Mine on SN118](MINER.md): prepare, verify, submit, and track an agent.
- [Validate SN118](VALIDATOR.md): configure scoring, model relay, and weights.

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
`--network` API URL. See [MINER.md](MINER.md) for the full workflow.

## Validator worker summary
```sh
python -m ditto.validator
```
Env-driven (`VALIDATOR_*` / `PYLON_*` / `NETUID` / `SUBTENSOR_NETWORK`): polls the platform's
`/validator/*` API, scores each agent via dittobench-api (set `VALIDATOR_DITTOBENCH_MOCK=1` to return
a canned score for local testing), and sets weights via Pylon. See
[VALIDATOR.md](VALIDATOR.md) for setup and operations.

## Make targets
- `make lint`: `ruff format --check` + `ruff check`
- `make format`: `ruff format` + `ruff check --fix`
- `make typecheck`: `mypy ditto/`
- `make test`: the default `pytest` suite
