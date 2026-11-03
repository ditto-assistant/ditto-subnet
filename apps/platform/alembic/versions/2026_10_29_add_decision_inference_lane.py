"""add the dark decision-model inference lane to grants and requests

Revision ID: 4c8e2a6f1d93
Revises: 9d1e5c7a3f28
Create Date: 2026-10-29

A decision model (TypeSafe Jev) answers structured choice/score questions about
a state and returns labels, probabilities and confidence, never prose. It is a
third hosted lane beside chat and embeddings, with its own pinned model, price
and walls stamped on the grant at mint, exactly as the chat budgets are.

Every new grant column defaults to the "lane not granted" value: a NULL
``decision_model`` and zero budgets. A grant minted while the operator knob is
off is therefore byte-identical to one minted before this revision, and the
relay refuses a decision call against it as ``model_not_permitted``.

``decision_cost_microusd`` is booked at settlement as receipted input tokens x
the grant-stamped list price. It is ledger cost by construction (the provider
bill is never consulted), so the per-run dollar ceiling (#3271) can add it to
its lane sum without a second meter.

Both tables are hot. Each table is altered in its own short transaction (never
two hot tables at once, #481): one ``ALTER TABLE`` adds every grant column as a
non-volatile ``NOT NULL DEFAULT`` (metadata only on PostgreSQL 11+) plus a CHECK
``NOT VALID``; the constraint scans run afterwards under ``SHARE UPDATE
EXCLUSIVE``, which does not block writers. Re-runnable from any point.
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import run_with_retry

revision: str = "4c8e2a6f1d93"
down_revision: str | Sequence[str] | None = "9d1e5c7a3f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GRANT_CHECK = "ck_inference_grants_inference_grants_decision_accounting"
_KIND_CHECK = "ck_inference_requests_inference_requests_kind"

_GRANT_COLUMNS = (
    ("decision_model", "TEXT"),
    ("decision_input_microusd_per_mtok", "BIGINT NOT NULL DEFAULT 0"),
    ("decision_request_budget", "INTEGER NOT NULL DEFAULT 0"),
    ("decision_token_budget", "BIGINT NOT NULL DEFAULT 0"),
    ("decision_request_count", "INTEGER NOT NULL DEFAULT 0"),
    ("decision_tokens", "BIGINT NOT NULL DEFAULT 0"),
    ("decision_cost_microusd", "BIGINT NOT NULL DEFAULT 0"),
    ("decision_active_requests", "INTEGER NOT NULL DEFAULT 0"),
)

_GRANT_CHECK_SQL = (
    "decision_input_microusd_per_mtok >= 0 AND decision_request_budget >= 0 "
    "AND decision_token_budget >= 0 AND decision_request_count >= 0 "
    "AND decision_tokens >= 0 AND decision_cost_microusd >= 0 "
    "AND decision_active_requests >= 0 "
    "AND (decision_model IS NULL OR "
    "(decision_request_budget > 0 AND decision_token_budget > 0))"
)


def upgrade() -> None:
    bind = op.get_bind()
    adds = ", ".join(
        f"ADD COLUMN IF NOT EXISTS {name} {sql}" for name, sql in _GRANT_COLUMNS
    )
    with op.get_context().autocommit_block():
        run_with_retry(
            bind,
            [
                f"ALTER TABLE inference_grants {adds}, "
                f"DROP CONSTRAINT IF EXISTS {_GRANT_CHECK}, "
                f"ADD CONSTRAINT {_GRANT_CHECK} CHECK ({_GRANT_CHECK_SQL}) NOT VALID"
            ],
            "add inference_grants decision lane columns",
        )
        run_with_retry(
            bind,
            [f"ALTER TABLE inference_grants VALIDATE CONSTRAINT {_GRANT_CHECK}"],
            "validate inference_grants decision lane check",
        )
        run_with_retry(
            bind,
            [
                "ALTER TABLE inference_requests "
                f"DROP CONSTRAINT IF EXISTS {_KIND_CHECK}, "
                f"ADD CONSTRAINT {_KIND_CHECK} CHECK "
                "(request_kind IN ('chat', 'embedding', 'decision')) NOT VALID"
            ],
            "widen inference_requests.request_kind",
        )
        run_with_retry(
            bind,
            [f"ALTER TABLE inference_requests VALIDATE CONSTRAINT {_KIND_CHECK}"],
            "validate inference_requests.request_kind",
        )


def downgrade() -> None:
    bind = op.get_bind()
    drops = ", ".join(
        f"DROP COLUMN IF EXISTS {name}" for name, _sql in reversed(_GRANT_COLUMNS)
    )
    with op.get_context().autocommit_block():
        # The old schema has no decision lane: its ledger rows go with it.
        run_with_retry(
            bind,
            ["DELETE FROM inference_requests WHERE request_kind = 'decision'"],
            "drop decision inference requests",
        )
        run_with_retry(
            bind,
            [
                "ALTER TABLE inference_requests "
                f"DROP CONSTRAINT IF EXISTS {_KIND_CHECK}, "
                f"ADD CONSTRAINT {_KIND_CHECK} CHECK "
                "(request_kind IN ('chat', 'embedding'))"
            ],
            "narrow inference_requests.request_kind",
        )
        run_with_retry(
            bind,
            [
                "ALTER TABLE inference_grants "
                f"DROP CONSTRAINT IF EXISTS {_GRANT_CHECK}, "
                f"{drops}"
            ],
            "drop inference_grants decision lane columns",
        )
