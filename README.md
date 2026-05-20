# Ditto Subnet (Bittensor SN118)

Go-based agent memory harness incentive layer. Miners submit a Go harness implementing the required MCP server + agent loop interfaces; validators run each submission in an isolated Docker sandbox, score across correctness / token cost / wall-clock, and the winner takes essentially all emissions.

## Quickstart

```sh
cp .env.example .env
uv sync
make stack-up        # postgres + pylon, blocks until both report healthy
make migrate         # apply alembic migrations
make smoke-pylon     # verify ChainClient against finney via Pylon
make test            # unit tests
```

`make stack-down` stops the services. Postgres state persists in a named docker volume across restarts; `docker compose down -v` for a hard reset.

## Make targets

- `make lint` — `ruff format --check` + `ruff check`
- `make format` — `ruff format` + `ruff check --fix`
- `make typecheck` — `mypy ditto/`
- `make test` — `pytest`
- `make smoke-pylon` — exercise the chain client against the live Pylon
- `make stack-up` / `make stack-down` — bring docker-compose services up / down
- `make migrate` / `make migrate-down` — apply / roll back one alembic revision
- `make migrate-history` / `make migrate-current` — alembic history + current head
