# Ditto Platform

**The API server for Ditto, Bittensor Subnet 118 (SN118).**

Ditto Platform is the central, team-operated service that sits between **miners**
(who submit agent-memory harnesses) and **validators** (who evaluate them and set
weights on chain). It owns miner intake, on-chain payment verification, object
storage for submissions, the evaluation job queue, the score ledger, and the
operational state machine that moves a submission from `uploaded` to `live`.

The source in this repository is open under the [MIT License](LICENSE). The
production service remains centrally operated. Publishing the source does not
publish private miner artifacts, operator review evidence, credentials, or
protected operational data, and it does not grant access to admin endpoints.

> The chain is the settlement layer (weights, stake, payments). This platform is
> the **workflow** layer the chain can't hold — queues, leases, payment
> replay-protection, submission status, and the public score ledger.

---

## Where this fits

```
┌─────────────┐   upload (HTTP)    ┌──────────────────┐   poll / score (HTTP)   ┌─────────────┐
│  miner CLI  │ ─────────────────▶ │  Ditto Platform  │ ◀────────────────────── │ validators  │
│ ditto-subnet│                    │   (this repo)    │                         │ ditto-subnet│
└─────────────┘                    └──────────────────┘                         └──────┬──────┘
                                     │   │   │   │                                      │ put_weights
                                  Postgres │ MinIO/S3                                   ▼
                                       Pylon (subtensor)                          Bittensor chain
```

- **Miner side & validator daemon:** [`ditto-subnet`](https://github.com/ditto-assistant/ditto-subnet)
- **Platform-operated screening worker:** [`workers/screener`](https://github.com/ditto-assistant/ditto-subnet/tree/main/workers/screener)
- **Reference memory harness (what miners fork):** [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness)
- **This repo** is the platform/API only. It is intentionally split out so it can
  be deployed and scaled independently of the miner/validator code.

The miner/validator contract is the **OpenAPI schema** served at `/docs`
(Swagger) and `/openapi.json`. The screener additionally shares only the
dependency-light `ditto-screening-protocol` package, pinned to an exact commit.

---

## API surface

### Built (miner intake + status)

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Liveness + DB/chain readiness + running vs checked-out commit |
| `GET /metrics` | Prometheus metrics |
| `GET /api/v1/upload/eval-pricing` | Quote the upload fee in rao (CoinGecko TAO/USD oracle) |
| `POST /api/v1/upload/check` | Pre-payment validation (signature, registration, size, accidental identical-upload detection) |
| `POST /api/v1/upload/agent` | Verified submission: assign payment/credit → store unique tarball → write `agents` + `evaluation_payments` atomically; an accidental paid identical upload becomes a reusable credit |
| `GET /api/v1/retrieval/agent-by-hotkey` | Look up a miner's latest agent |
| `GET /api/v1/retrieval/agent/{id}/status` | Poll a submission's lifecycle status |

> `/health` and `/metrics` are unprefixed; all other routes are versioned under `/api/v1`.

### Built (validator-facing)

| Method & path | Purpose |
| --- | --- |
| `POST /api/v1/validator/job` | Lease a scoring ticket (seed, dataset_sha256, run_size, deadline) |
| `POST /api/v1/validator/heartbeat` | Submit a signed runtime heartbeat with optional coarse system health |
| `GET /api/v1/public/validators` | Read the public-safe validator fleet view |
| `GET /api/v1/public/screeners` | Read the public-safe platform screener fleet view |
| `GET /api/v1/public/bench/config` | The frozen benchmark setup: locked model, judge-free grading, seed derivation, mirror |
| `GET /api/v1/validator/agent/{id}/artifact` | Presigned download URL for an agent tarball |
| `POST /api/v1/validator/agent/{id}/score` | Submit a signed DittoBench score (→ `scores` table) |

### Built (screener-facing)

Screening is a **platform-operated** pre-evaluation gate: a dedicated host the team
runs (not the validators). It drains `uploaded` agents, `docker build`s and
health-checks each crate in isolation, and promotes passes to `evaluating`,
rejects deterministic submission failures, and records retryable infrastructure
failures as `screening_failed` for another claim. A crate that does not compile
never costs a full benchmark.
It authenticates with a dedicated screener credential (an allowlisted
hotkey plus a bearer token), not a validator permit, so the screener key holds no
stake. The authoritative worker is deployed from the public, MIT-licensed
[`workers/screener`](https://github.com/ditto-assistant/ditto-subnet/tree/main/workers/screener) directory;
it remains platform-operated, and validators do not run it.

| Method & path | Purpose |
| --- | --- |
| `GET /api/v1/screener/queue` | List agents awaiting screening (status `uploaded`), oldest first |
| `POST /api/v1/screener/heartbeat` | Submit a dedicated-auth signed screener health report |
| `GET /api/v1/screener/agent/{id}/artifact` | Presigned download URL for the crate tarball |
| `POST /api/v1/screener/agent/{id}/result` | Signed pass/fail verdict that promotes the agent |

### Planned (scoring + ops)

Weight/score aggregation (`/scoring/*`) and `/admin/*`. See
[`docs/VALIDATOR.md`](https://github.com/ditto-assistant/ditto-subnet/blob/main/docs/VALIDATOR.md)
in `ditto-subnet` for validator scoring design and operations.

---

## Tech stack

- **API:** FastAPI + Uvicorn (Python 3.11+)
- **Wire models:** Pydantic (`ditto/api_models`) — the only place Pydantic is used
- **Database:** PostgreSQL via SQLAlchemy 2.0 async + asyncpg, migrations with Alembic
- **Object storage:** S3-compatible via aioboto3 (MinIO locally)
- **Chain reads:** [Pylon](https://github.com/bittensor-church/bittensor-pylon) +
  `async-substrate-interface`
- **Pricing:** CoinGecko oracle with in-process cache + stale-guard
- **Observability:** Prometheus metrics, structured request-id logging
- **Tooling:** `uv` (deps/venv), `ruff` (lint/format), `mypy`, `pytest`

---

## Quickstart (local development)

### Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python toolchain + venv)
- Node.js 22 and npm (copy lint only)
- Docker + Docker Compose (Postgres, MinIO, Pylon)
- Python 3.11 or 3.12

### 1. Configure

```sh
cp .env.example .env
```

Then edit `.env` and set **`DITTO_UPLOAD_PAYMENT_ADDRESS`** to a real SS58 address
— the server validates it at boot and refuses to start with the placeholder. All
other defaults match the local Docker stack.

### 2. Bring up infra + the API

```sh
uv sync                # install dependencies into .venv
make stack-up          # postgres + pylon + minio (waits until healthy)
make migrate           # apply alembic migrations
make api-up            # run the FastAPI app on :8000 (foreground)
```

In another terminal:

```sh
make smoke-api         # curl /health
open http://localhost:8000/docs   # interactive API docs
```

`make stack-down` stops the Docker services. Postgres state persists in a named
volume across restarts; `docker compose down -v` for a hard reset.

> Pylon runs on host port **8001** so the API can own **8000**.

---

## Running on a host with pm2 (staging / production)

The API is a long-lived process; we run it under [pm2](https://pm2.keymetrics.io/)
on the host (the database and object store stay in Docker). Logs, restarts, and
scripted updates are first-class.

```sh
npm install -g pm2           # one-time, if not present
./scripts/start.sh           # infra up + migrate + start API under pm2
pm2 logs ditto-api           # tail logs
pm2 status                   # process state
./scripts/update.sh          # git pull + uv sync + migrate + pm2 reload + health gate
./scripts/stop.sh            # stop the API process
```

- pm2 config: [`scripts/ecosystem.config.js`](scripts/ecosystem.config.js)
- Logs are written to `./logs/ditto-api.{out,err}.log` and via `pm2 logs`.
- `pm2 startup` + `pm2 save` will resurrect the process across host reboots.
- **Updates are not zero-downtime.** The app runs as a single `fork`-mode pm2
  process, so `pm2 reload` has no second instance to shift traffic onto and
  degrades to a stop/start: expect ~6s of refused connections per deploy
  (measured). Cluster mode would close that gap and is an open operator
  decision, not something the reload command papers over today.
- **`pm2 reload` does not reconcile how a process is launched.** `script`,
  `interpreter`, `interpreter_args`, `exec_mode`, and `cwd` are kept from pm2's
  saved dump even when `ecosystem.config.js` changes them; `args` and env *are*
  reconciled. Changing `script` and reloading therefore runs the OLD program
  with the NEW args, which is how a deploy once left the API in `waiting
  restart` with pid 0 while the site served 502. `scripts/update.sh` now diffs
  the running launch identity against the config
  ([`scripts/pm2_deploy_plan.js`](scripts/pm2_deploy_plan.js)) and does
  `pm2 delete` + `pm2 start` for that app when they differ, so this is handled;
  if you ever bypass `update.sh`, recreate the app rather than reloading it.
- **`update.sh` fails the deploy if the app does not come back.** After
  starting or reloading it polls `/health` on the local port and exits non-zero
  with the tail of `logs/ditto-api.err.log` if the API is not serving within
  `DITTO_HEALTH_TIMEOUT` (default 120s). The one-shot image-cleanup job is
  cron-driven with `autorestart: false`, so `stopped` is its correct state and
  is not treated as a failure.
- **A deploy only passes when the process reports the commit that was checked
  out.** Being checked out and being in effect are different facts. `/health`
  carries the commit resolved at the *process's* boot, and the deploy gate
  compares it against `git rev-parse HEAD`; a 200 from a build older than the
  checkout fails the deploy. `/health` also reports `checked_out_commit` and
  `commit_drift` so the question "is this host running what it has checked
  out?" can be answered at any time, not just during a deploy. Drift is
  reported, never enforced — it does not change the HTTP status, because
  pulling a serving host out of rotation over stale code turns a deploy problem
  into an outage.
- **Divergent Alembic heads stop the deploy in preflight, not mid-sequence.**
  Two migrations that each extend the same parent and merge independently make
  `alembic upgrade head` refuse to run. `update.sh` now asserts a single head
  *before* `uv sync` or the database is touched, using
  `scripts/check_migration_order.py --head` (stdlib only, no venv), and prints
  every head plus the exact `alembic merge` that reconciles them. `heads`
  (plural) was deliberately **not** adopted: applying two unreconciled branches
  in an order nobody reviewed can produce a schema neither branch intended.
- **A deploy that fails before pm2 restarts rolls the checkout back.** The old
  process is still serving in that window, so the checkout is reset to the
  revision `/health` reports and dependencies are re-synced — the host stops
  claiming a deploy that never took effect. After pm2 has been restarted the
  checkout is left alone; going back from there is a deploy of the previous
  revision, not a `git reset`. Every attempt is recorded in
  `logs/last-deploy.json` (result, stage, target, rollback), which is also the
  fallback rollback target when the API cannot answer for itself.

---

## Make targets

| Target | Description |
| --- | --- |
| `make lint` | `ruff format --check` + `ruff check` |
| `make lint-copy` | lint public dashboard copy with Faircopy |
| `make format` | `ruff format` + `ruff check --fix` |
| `make typecheck` | `mypy ditto/` |
| `make test` | unit test suite (`pytest`) |
| `make test-integration` | integration tests against the live stack |
| `make api-up` | run the API in the foreground against the local stack |
| `make smoke-api` | curl `/health` to confirm reachability |
| `make smoke-pylon` | exercise the chain client against live Pylon |
| `make stack-up` / `make stack-down` | bring Docker services up / down |
| `make migrate` / `make migrate-down` | apply / roll back one migration |
| `make migrate-history` / `make migrate-current` | alembic history / current head |

---

## Project layout

```
ditto/
  api_server/          FastAPI app
    endpoints/         health · metrics · upload · retrieval · validator
    middleware/        request-id · auth pass-through · error envelope
    payment_verifier/  on-chain payment proof verification
    pricing/           CoinGecko oracle + upload-fee config
    storage/           S3/MinIO client
    config.py          env-driven ApiServerConfig
    factory.py         create_api_server() + lifespan
    __main__.py        process entry point (argparse + uvicorn)
  api_models/          Pydantic wire shapes (the client contract)
    agent_status.py    canonical AgentStatus lifecycle enum
  chain/               Pylon-backed ChainClient
  db/                  SQLAlchemy models + queries + engine/session factory
  tests/               unit + integration tests
alembic/               database migrations
scripts/               pm2 ecosystem + start/stop/update + smoke_pylon
```

---

## Configuration

All configuration is environment-driven; see [`.env.example`](.env.example) for the
full annotated list. Key groups: **API** (`API_HOST/PORT/LOG_LEVEL`), **Pylon/chain**
(`PYLON_URL`, `PYLON_OPEN_ACCESS_TOKEN`, `NETUID`, `SUBTENSOR_NETWORK`), **Postgres**
(`POSTGRES_*`), **upload/pricing** (`DITTO_UPLOAD_PAYMENT_ADDRESS`,
`DITTO_UPLOAD_FEE_USD`, `DITTO_UPLOAD_FEE_BUFFER`), and **object storage**
(`STORAGE_*`). The server validates config at boot and exits non-zero on a bad value
so a supervisor restarts cleanly.

---

## Testing

```sh
make test                 # the whole suite; nothing to set up first
make test-integration     # narrows to the integration tier
make test-chain           # the two tests that need a live subtensor
```

**`uv run pytest` is green on a fresh clone with no `.env` and nothing
exported.** Postgres and MinIO are ambient containers the harness starts on
demand (`ditto/tests/pgharness.py`, `ditto/tests/minioharness.py`), on test-only
ports so they cannot be confused with the compose stack. The environment
variables the config parsers require — `DITTO_UPLOAD_PAYMENT_ADDRESS`,
`PYLON_OPEN_ACCESS_TOKEN`, `STORAGE_*` — are defaulted to obviously-fake
fixtures by `ditto/tests/env_defaults.py`.

Those defaults are **test-only**, and deliberately so: the server itself still
refuses to boot on a missing or placeholder payment address, because a platform
that silently accepts one is far worse than a red test. Anything already set —
your `.env`, an exported variable, CI's explicit block — always wins. Adding a
newly-required variable without a test default fails
`ditto/tests/test_env_defaults.py`, which names the variable.

`slow`, `localnet`, and `needs_chain` are excluded by default; `integration` and
`e2e` are not. The suite uses all available CPU cores through `pytest-xdist`. CI
runs `ruff`, `mypy`, `faircopy`, and `pytest` on every PR and on `main`.

---

## Branching

`main` (protected, release) ← `dev` (integration) ← feature branches
(`name/topic`, e.g. `dan/api_init`). Open PRs into `dev`; `dev` merges to `main`
via PR.
