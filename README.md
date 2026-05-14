# Ditto Subnet (Bittensor SN118)

Go-based agent memory harness incentive layer. Miners submit a Go harness implementing the required MCP server + agent loop interfaces; validators run each submission in an isolated Docker sandbox, score across correctness / token cost / wall-clock, and the winner takes essentially all emissions.

## Quickstart

```sh
uv sync
make test
```

## Make targets

- `make lint` — `ruff format --check` + `ruff check`
- `make format` — `ruff format` + `ruff check --fix`
- `make typecheck` — `mypy ditto/`
- `make test` — `pytest`
