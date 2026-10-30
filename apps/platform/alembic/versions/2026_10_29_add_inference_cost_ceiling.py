"""book a list-price ledger cost per grant and stamp a per-run cost ceiling

Revision ID: 4ff0f63c897e
Revises: 4c8e2a6f1d93
Create Date: 2026-10-29

Today a run's inference spend is bounded only by unit walls (chat tokens,
embedding tokens, request counts). Those walls do not compare across models,
so issue #3271 adds one dollar-denominated ceiling per run:

* ``ledger_cost_microusd`` on ``inference_grants`` and ``inference_requests``
  is receipted units times the **grant-stamped** list price. It is never the
  provider bill (``cost_microusd`` stays that), so provider routing, aggregator
  cost reports and cache hits cannot change what a miner is charged.
* ``ledger_prompt_price_per_token`` / ``ledger_completion_price_per_token``
  (USD per token, the unit of the ``route_*`` prices) and
  ``embedding_price_microusd_per_token`` are the stamped list prices. The
  embedding price defaults to the ``0.004`` the relay hard-coded until now, so
  every existing grant settles exactly as before.
* ``cost_ceiling_microusd`` (NULL = no ceiling), ``cost_ceiling_mode``
  (``off`` / ``shadow`` / ``enforce``) and ``cost_ceiling_would_decline`` are
  stamped at mint like the chat budgets, so a settings change never alters a
  live run.

Both tables are hot. Every column goes through ``safe_add_column``: the
defaulted ``NOT NULL`` columns are the metadata-only one-shot shape (a constant
default is stored in ``pg_attribute`` without a rewrite) and the nullable ones
are a bare metadata add. Each runs in its own short transaction, grants before
requests (the application lock order), and no transaction touches both tables.
The CHECK constraints are added ``NOT VALID`` and validated afterwards under
``SHARE UPDATE EXCLUSIVE``, which does not block writers. Every step is
re-runnable.
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import run_with_retry, safe_add_column, safe_drop_column

revision: str = "4ff0f63c897e"
down_revision: str | Sequence[str] | None = "4c8e2a6f1d93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRANTS = "inference_grants"
_REQUESTS = "inference_requests"

_GRANT_COLUMNS: tuple[tuple[str, str, bool, str | None], ...] = (
    # (column, type, not_null, server_default)
    ("ledger_cost_microusd", "BIGINT", True, "0"),
    ("cost_ceiling_microusd", "BIGINT", False, None),
    ("cost_ceiling_mode", "TEXT", True, "'off'"),
    ("cost_ceiling_would_decline", "BIGINT", True, "0"),
    ("ledger_prompt_price_per_token", "DOUBLE PRECISION", False, None),
    ("ledger_completion_price_per_token", "DOUBLE PRECISION", False, None),
    ("embedding_price_microusd_per_token", "DOUBLE PRECISION", True, "0.004"),
)

_GRANT_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_inference_grants_inference_grants_cost_ceiling_mode",
        "cost_ceiling_mode IN ('off', 'shadow', 'enforce')",
    ),
    (
        "ck_inference_grants_inference_grants_cost_ceiling_microusd",
        "cost_ceiling_microusd IS NULL OR cost_ceiling_microusd > 0",
    ),
    (
        "ck_inference_grants_inference_grants_ledger_cost_microusd",
        "ledger_cost_microusd >= 0",
    ),
    (
        "ck_inference_grants_inference_grants_embedding_price",
        "embedding_price_microusd_per_token >= 0",
    ),
)

_REQUEST_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_inference_requests_inference_requests_ledger_cost_microusd",
        "ledger_cost_microusd >= 0",
    ),
)


def _add_checks(table: str, checks: tuple[tuple[str, str], ...]) -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        for name, expression in checks:
            exists = bind.exec_driver_sql(
                f"SELECT 1 FROM pg_constraint WHERE conname = '{name}'"
            ).scalar()
            if not exists:
                run_with_retry(
                    bind,
                    [
                        f"ALTER TABLE {table} ADD CONSTRAINT {name} "
                        f"CHECK ({expression}) NOT VALID"
                    ],
                    f"add {table}.{name}",
                )
            run_with_retry(
                bind,
                [f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"],
                f"validate {table}.{name}",
            )


def _drop_checks(table: str, checks: tuple[tuple[str, str], ...]) -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        for name, _ in checks:
            run_with_retry(
                bind,
                [f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"],
                f"drop {table}.{name}",
            )


def upgrade() -> None:
    for column, type_, not_null, server_default in _GRANT_COLUMNS:
        safe_add_column(
            _GRANTS,
            column,
            type_,
            not_null=not_null,
            server_default=server_default,
        )
    _add_checks(_GRANTS, _GRANT_CHECKS)
    safe_add_column(
        _REQUESTS, "ledger_cost_microusd", "BIGINT", not_null=True, server_default="0"
    )
    _add_checks(_REQUESTS, _REQUEST_CHECKS)


def downgrade() -> None:
    _drop_checks(_GRANTS, _GRANT_CHECKS)
    for column, _, _, _ in reversed(_GRANT_COLUMNS):
        safe_drop_column(_GRANTS, column)
    _drop_checks(_REQUESTS, _REQUEST_CHECKS)
    safe_drop_column(_REQUESTS, "ledger_cost_microusd")
