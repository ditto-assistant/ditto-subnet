# CLAUDE.md

Guidance for Claude Code (and humans) working in **ditto-platform** — the API
server for Bittensor Subnet 118. Read this before making changes.

## What this repo is

The platform/API service only. Miner CLI and the validator daemon live in
[`ditto-subnet`](https://github.com/ditto-assistant/ditto-subnet); the reference
memory harness lives in [`ditto-harness`](https://github.com/ditto-assistant/ditto-harness).
This service talks to clients over HTTP; the **OpenAPI schema is the contract**
(there is no shared package between repos).

**Validator boundary (server side lives here):** this repo owns the
validator-*facing* API — the `/validator/*` endpoints (`endpoints/validator.py`),
their wire models (`api_models/validator.py`), the score ledger in `ditto/db`,
and the `4xxx` validator error codes. The validator **worker/process itself does
not live here** — it runs in `ditto-subnet` (`ditto/validator/`), is stateless
(no DB), and reaches this service only over HTTP. Do not add a `ditto/validator/`
package or any weight-setting / dittobench-scoring code to this repo; that is the
subnet's job. The two `api_models/validator.py` copies are kept in sync via the
OpenAPI contract, not a shared import.

## Architecture in one paragraph

`ditto/api_server` is a FastAPI app assembled in `factory.py:create_api_server()`
from env-driven config (`config.py`). Endpoints live under `endpoints/`; shared
concerns (request-id, auth pass-through, error envelope) under `middleware/`.
Three service modules back the upload flow: `payment_verifier/` (verifies the
on-chain payment proof), `pricing/` (CoinGecko TAO/USD oracle + fee math), and
`storage/` (S3/MinIO). Persistence is `ditto/db` (SQLAlchemy 2.0 async + asyncpg,
Alembic migrations). Chain reads go through `ditto/chain` (Pylon +
async-substrate-interface). Wire shapes are `ditto/api_models` (Pydantic).

## Conventions (match the existing code)

- **Pydantic only in `ditto/api_models`.** Everything internal — configs, value
  objects, results — uses `@dataclass(frozen=True)`.
- **`AgentStatus` lives in `ditto/api_models/agent_status.py`** (it's a wire +
  DB value). `ditto/db/models.py` re-imports it, so `from ditto.db.models import
  AgentStatus` still works. Do not redefine the enum in `db`.
- **Config is env-driven dataclasses** with `parse_*_from_env()` builders and a
  `check_config()` validator that runs at boot. Fail fast with a typed
  `*ConfigError`; never boot with a placeholder.
- **Errors map to numeric codes** via `middleware/error_envelope.py`. Domain
  errors are typed exception subclasses; the envelope handler maps them to HTTP
  status + a stable error code. Add new codes in the documented ranges.
- **Async everywhere** — SQLAlchemy `AsyncSession`, aioboto3, httpx. DB mutations
  happen inside one `async with session.begin()` transaction.
- **Migrations own the schema.** `ditto/db/models.py` describes it in Python but
  Alembic under `alembic/versions/` is the source of truth — keep them in sync and
  add a migration for any schema change.
- **One Alembic head, always — rebase before you add a migration.** Alembic
  linears the chain by `down_revision`, *not* by merge date, so two branches
  that each extend the same parent stay divergent however git merges them, and
  `alembic upgrade head` then refuses to run at all (`Multiple head revisions
  are present`) — taking the deploy and every DB test with it. Rebase onto
  current `origin/main` and point `down_revision` at its head before opening a
  PR, renumbering the `YYYY_MM_DD_` filename if main has moved past your date.
  This is enforced, not just advised: `Migration order` resolves the **merge
  result** rather than your branch alone, and every push to `main` re-checks
  each open PR — so a PR that was green when you pushed it goes red the moment
  someone else's migration lands, instead of staying mergeable. Reproduce
  either locally with `python scripts/check_migration_order.py origin/main`.
- **Adding a column to a hot table? Use `safe_add_column`.** `op.add_column`
  holds an `AccessExclusiveLock` until the migration commits, so a plain
  add-then-backfill stalls every writer for the length of the backfill — that is
  how #481 deadlocked the deploy twice. `ditto/db/migration_lock.py` gives you
  `safe_add_column` / `safe_drop_column`, which add metadata-only, backfill in
  committed batches, and pin the constraint in one short window. Hot tables today
  are `inference_requests`, `inference_grants` and `validator_tickets`. Take
  table locks in the application's order — validator_tickets → inference_grants
  → inference_requests — and never touch two hot tables in one transaction.
  `alembic/env.py` applies a `lock_timeout` and a bounded retry underneath, but
  that is a backstop: it does not rescue a badly-shaped migration under sustained
  load, and it assumes your migration is re-runnable.

## Commands

```sh
uv sync                      # install deps
make stack-up                # postgres + pylon + minio (docker)
make migrate                 # alembic upgrade head
make api-up                  # run the API on :8000 (foreground)
make smoke-api               # curl /health
make lint lint-copy typecheck test  # ruff + faircopy + mypy + pytest
```

Run on a host under pm2 with `./scripts/start.sh` (see README). Before opening a
PR, run `make lint`, `make lint-copy`, `make typecheck`, and `make test`. CI
enforces all four; Python checks run on 3.11 and 3.12.

## Testing

- **Every database test runs against a real Postgres.** `ditto/tests/pgharness.py`
  starts an ambient container (`ditto-platform-test-postgres`, port 15433) on
  demand, migrates a template database with the real Alembic chain, and gives
  each xdist worker its own clone. Nothing to set up: `make test` just works.
- **The upload tests run against a real object store**, provisioned the same way
  by `ditto/tests/minioharness.py` (`ditto-platform-test-minio`, port 19000).
- **`uv run pytest` needs no environment.** `ditto/tests/env_defaults.py` seeds
  the variables the config parsers require — `DITTO_UPLOAD_PAYMENT_ADDRESS`,
  `PYLON_OPEN_ACCESS_TOKEN`, `STORAGE_*` — with obviously-fake fixtures, applied
  in `pytest_configure`. `.env` is gitignored and is not copied into worktrees,
  so this is what keeps a fresh clone and every worktree-based agent green.
  Anything already set wins. **Do not move these defaults into
  `ditto/api_server/config.py`**: production must keep failing loudly, since a
  platform that boots on a placeholder receive address is far worse than a red
  test. A newly-required variable without a default fails
  `ditto/tests/test_env_defaults.py`, which names it.
- Use the root `engine` / `session_maker` / `session` fixtures
  (`ditto/tests/conftest.py`). Do **not** build a `create_async_engine(...)`
  inline in a test — that is the copy-paste habit this harness replaced.
- Commits in tests are real commits. There is no rollback-isolation tier, and
  adding one for the concurrency/accounting tests would re-hide the exact bug
  class (#438) the Postgres migration exists to catch.
- Markers `slow`, `localnet`, and `needs_chain` are excluded by default.
  `integration` and `e2e` are **not** — they were, and the consequence was that
  the #438 regression test never ran in CI. `needs_chain` is exactly two tests
  that assert on data only a live subtensor can produce; everything a container
  can provide (Postgres, MinIO) is provisioned in CI instead of excluded. Do not
  widen it.
- There is no SQLite tier any more, and `aiosqlite` is gone from the dev
  dependency group. If a test needs a database, it gets the real one.
- Put unit tests next to the package they cover under `ditto/tests/<package>`.
- `make test-db-reset` forces a template rebuild; `make test-db-clean` reaps
  every harness-owned database.
- **Hundreds of DB failures after touching migrations? Reset the template
  before believing them.** The harness migrates `ditto_test_template` once and
  then clones it, so a template built while the chain was broken keeps failing
  every DB test long after the chain is fixed — and it fails in a way that
  reads as *your* change's fault. During the 2026-07-28 two-head fix the first
  run on a clean worktree came back with 513 failures from exactly this; the
  same worktree was green after `make test-db-reset`. The tell is breadth: a
  real regression fails tests near what you touched, a stale template fails
  everything that opens a database.

## Gotchas

- Pylon is on host port **8001**; the API owns **8000**.
- `/upload/agent` enforces the tarball size cap from the *actual streamed bytes*
  and re-verifies the SHA-256; `/upload/check` trusts the miner-reported size.
- The upload flow is ordered cheap-before-expensive and stores to S3 *before* the
  DB transaction (orphan blobs are cheap; orphan rows break the state machine).
- Some `/upload/*` validations are intentionally deferred (tar manifest, import
  allowlist, schema diff) pending the harness interface spec. Banned-hotkey
  rejection is **no longer deferred** — the `banned_hotkeys` table exists and is
  enforced (hard 403 in `/upload/agent`, reported by `/upload/check`, surfaced by
  `/retrieval/agent-by-hotkey`). What miners submit and what is / isn't enforced
  today is written up in `docs/submission-contract.md`.

## Branching

`main` (release) ← `name/topic` feature branches. Open PRs into `main`. Do not
commit directly to `main`; branch, push, and PR.
