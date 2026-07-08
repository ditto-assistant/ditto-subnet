# Ditto Subnet (Bittensor SN118)

Go-based agent memory harness incentive layer. Miners submit a Go harness implementing the required MCP server + agent loop interfaces; validators run each submission in an isolated Docker sandbox, score across correctness / token cost / wall-clock, and the winner takes essentially all emissions.

**Validators:** see [`VALIDATOR_FAQ.md`](VALIDATOR_FAQ.md) for what to have ready before release (compute, keys, registration) and how to do a dry run.

## Quickstart

```sh
cp .env.example .env
uv sync
make stack-up        # postgres + pylon, blocks until both report healthy
make migrate         # apply alembic migrations
make smoke-pylon     # verify ChainClient against finney via Pylon
make test            # unit tests
```

`make api-up` runs the FastAPI server in the foreground on `:8000`. In a separate terminal:

```sh
make api-up          # foreground; Ctrl+C to stop
```

Then back in the first terminal:

```sh
make smoke-api       # curl /health to confirm the API is reachable
```

`make stack-down` stops the services. Postgres state persists in a named docker volume across restarts; `docker compose down -v` for a hard reset.

The API server runs locally (not in compose) for fast iteration. Pylon shifts to host port 8001 so the API can own 8000.

## Make targets

- `make lint` - `ruff format --check` + `ruff check`
- `make format` - `ruff format` + `ruff check --fix`
- `make typecheck` - `mypy ditto/`
- `make test` - run the default `pytest` suite
- `make test-integration` - run integration tests against the live stack
- `make api-up` - run `python -m ditto.api_server` against the local stack
- `make smoke-api` - curl `/health` to confirm the API is reachable
- `make smoke-pylon` - exercise the chain client against the live Pylon
- `make stack-up` / `make stack-down` - bring docker-compose services up / down
- `make migrate` / `make migrate-down` - apply / roll back one alembic revision
- `make migrate-history` / `make migrate-current` - alembic history + current head
